"""Application tracker and feedback learning.

Everything here takes a *sync* SQLAlchemy Session so the same code serves the API routes
(FastAPI runs sync routes in a thread pool), the Telegram webhook and the cron digest.

Feedback learning is deliberately small and explainable. When ranking jobs for the digest
and /top, each candidate is compared with the jobs you already reacted to:

* "Similar" means the two jobs share at least 2 keywords and the keyword sets overlap by at
  least 50% (Jaccard). Keywords are the job's matched skills plus the role words of its title
  ("backend", "platform", "data"...; seniority and filler words like "senior" or "engineer"
  are ignored).
* Each similar job you marked 👎 Not for me costs 10 points, and a 👎 at the same company
  costs another 10. Each similar job you saved or applied to adds 5, and a liked job at the
  same company adds 5.
* The total is clamped to [-20, +10] points on the 0-100 score, so feedback can reorder the
  digest and push weak matches under the threshold, but it never buries a strong match
  outright or lifts a poor one to the top.
* Every adjustment comes with a reason ("Similar to jobs you saved: python, fastapi"), shown
  in the Telegram message next to the score.

The stored analysis score is never changed; the adjustment only applies when picking and
ordering what to send you.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Application, AppSetting, JobAnalysis, JobRaw, Run
from app.models.tracker import APPLICATION_STATUSES

STATUSES = APPLICATION_STATUSES
# Statuses that mean "I liked this kind of job": you saved it or got as far as applying.
POSITIVE = frozenset({"saved", "applied", "interviewing", "offer", "rejected"})
NEGATIVE = frozenset({"not_interested"})
# Statuses that set applied_at the first time they're reached.
APPLIED_OR_LATER = frozenset({"applied", "interviewing", "offer", "rejected"})

SIMILAR_PENALTY = 10
COMPANY_PENALTY = 10
SIMILAR_BOOST = 5
COMPANY_BOOST = 5
MAX_PENALTY = 20
MAX_BOOST = 10
MIN_SHARED_KEYWORDS = 2
MIN_JACCARD = 0.5

_TITLE_STOPWORDS = frozenset(
    """a an and or the of for to in at with on by from as is senior sr junior jr lead staff principal
    mid level entry intern ii iii iv i remote hybrid onsite engineer engineering engineers developer
    developers dev specialist team member full part time contract contractor freelance m f d w x
    us usa eu uk emea latam apac anywhere worldwide global""".split()
)
_COMPANY_SUFFIXES = re.compile(r"\b(inc|llc|ltd|gmbh|corp|co|company|limited|plc|sa|bv)\b\.?")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat them as UTC like Postgres timestamptz."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# --------------------------------------------------------------------------- scores


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def base_points(analysis: JobAnalysis) -> float:
    """0-100 ranking value: the analysis `rank` when the scorer provides one, else the match score."""
    rank = _number(getattr(analysis, "rank", None))
    if rank is None:
        rank = _number((analysis.extracted_json or {}).get("rank"))
    if rank is not None:
        return rank * 100 if rank <= 1 else rank
    return (analysis.score or 0) * 100


def hire_probability(analysis: JobAnalysis) -> float | None:
    """0-1 estimate when the scorer provides one."""
    value = _number(getattr(analysis, "hire_probability", None))
    if value is None:
        value = _number((analysis.extracted_json or {}).get("hire_probability"))
    if value is None:
        return None
    return value / 100 if value > 1 else value


def match_info(analysis: JobAnalysis | None) -> dict:
    return ((analysis.extracted_json or {}).get("match") or {}) if analysis else {}


# --------------------------------------------------------------------------- feedback


def company_key(company: str | None) -> str:
    name = _COMPANY_SUFFIXES.sub("", (company or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", name).strip()


def title_keywords(title: str) -> set[str]:
    words = re.findall(r"[a-z][a-z0-9+#]*", title.lower())
    return {w for w in words if w not in _TITLE_STOPWORDS and len(w) > 1}


@dataclass(frozen=True)
class JobFeatures:
    job_id: int
    title: str
    company: str  # normalized, "" when unknown
    keywords: frozenset[str]


def features(job: JobRaw, analysis: JobAnalysis | None) -> JobFeatures:
    m = match_info(analysis)
    skills = (analysis.skills if analysis else None) or m.get("matched_skills") or []
    if not skills and analysis:
        skills = (analysis.extracted_json or {}).get("skills") or []
    keywords = {str(s).lower() for s in skills} | title_keywords(job.title)
    return JobFeatures(job.id, job.title, company_key(job.company), frozenset(keywords))


def similar(a: JobFeatures, b: JobFeatures) -> set[str] | None:
    """Shared keywords when the two jobs count as similar, else None."""
    shared = a.keywords & b.keywords
    union = a.keywords | b.keywords
    if len(shared) >= MIN_SHARED_KEYWORDS and union and len(shared) / len(union) >= MIN_JACCARD:
        return shared
    return None


@dataclass
class Adjustment:
    points: float = 0
    reasons: list[str] = field(default_factory=list)


def _sample(words: set[str], n: int = 3) -> str:
    return ", ".join(sorted(words)[:n])


def feedback_adjustment(job: JobFeatures, liked: list[JobFeatures], disliked: list[JobFeatures]) -> Adjustment:
    """Bounded score adjustment from your 👎 / ⭐ / ✅ history, with the reasons for it."""
    penalty, boost, reasons = 0.0, 0.0, []

    others = [d for d in disliked if d.job_id != job.job_id]
    if job.company and any(d.company == job.company for d in others):
        penalty += COMPANY_PENALTY
        reasons.append("Same company as a job you marked not for you")
    hits = [(d, s) for d in others if (s := similar(job, d))]
    if hits:
        penalty += SIMILAR_PENALTY * len(hits)
        reasons.append(f"Similar to jobs you marked not for you ({_sample(set().union(*(s for _, s in hits)))})")

    others = [lk for lk in liked if lk.job_id != job.job_id]
    if job.company and any(lk.company == job.company for lk in others):
        boost += COMPANY_BOOST
        reasons.append("Same company as a job you saved or applied to")
    hits = [(lk, s) for lk in others if (s := similar(job, lk))]
    if hits:
        boost += SIMILAR_BOOST * len(hits)
        reasons.append(f"Similar to jobs you saved or applied to ({_sample(set().union(*(s for _, s in hits)))})")

    return Adjustment(points=min(boost, MAX_BOOST) - min(penalty, MAX_PENALTY), reasons=reasons)


def feedback_sets(session: Session) -> tuple[list[JobFeatures], list[JobFeatures]]:
    rows = session.execute(
        select(JobRaw, JobAnalysis, Application.status)
        .join(Application, Application.job_id == JobRaw.id)
        .outerjoin(JobAnalysis, JobAnalysis.job_id == JobRaw.id)
        .where(Application.status.in_(POSITIVE | NEGATIVE))
    ).all()
    liked = [features(j, a) for j, a, s in rows if s in POSITIVE]
    disliked = [features(j, a) for j, a, s in rows if s in NEGATIVE]
    return liked, disliked


# --------------------------------------------------------------------------- candidates


@dataclass
class Candidate:
    job: JobRaw
    analysis: JobAnalysis
    application: Application | None
    base: float
    adjustment: Adjustment

    @property
    def points(self) -> float:
        return max(0.0, min(100.0, self.base + self.adjustment.points))

    @property
    def status(self) -> str:
        return self.application.status if self.application else "new"

    @property
    def eligible(self) -> bool | None:
        value = match_info(self.analysis).get("eligible")
        return value if isinstance(value, bool) else None

    @property
    def hire_probability(self) -> float | None:
        return hire_probability(self.analysis)


def candidates(
    session: Session,
    *,
    since: datetime | None = None,
    min_points: float | None = None,
    only_unnotified: bool = False,
    include_ineligible: bool = False,
) -> list[Candidate]:
    """Analyzed jobs nobody acted on yet (status new or untracked), best first after feedback.

    Filtering on eligibility happens in Python: it reads the JSON match, and the volume is a
    few hundred rows at most.
    """
    stmt = (
        select(JobRaw, JobAnalysis, Application)
        .join(JobAnalysis, JobAnalysis.job_id == JobRaw.id)
        .outerjoin(Application, Application.job_id == JobRaw.id)
        .where((Application.id.is_(None)) | (Application.status == "new"))
    )
    if since is not None:
        stmt = stmt.where(JobRaw.created_at >= since)
    if only_unnotified:
        stmt = stmt.where((Application.id.is_(None)) | (Application.notified_at.is_(None)))
    liked, disliked = feedback_sets(session)
    out: list[Candidate] = []
    for job, analysis, app in session.execute(stmt).all():
        c = Candidate(job, analysis, app, base_points(analysis), Adjustment())
        if not include_ineligible and c.eligible is False:
            continue
        c.adjustment = feedback_adjustment(features(job, analysis), liked, disliked)
        if min_points is not None and c.points < min_points:
            continue
        out.append(c)
    out.sort(key=lambda c: (-c.points, -(_aware(c.job.created_at) or utcnow()).timestamp(), c.job.id))
    return out


def candidate_for(session: Session, job_id: int) -> Candidate | None:
    """One job as a Candidate (any status), e.g. to redraw its Telegram card after a button tap."""
    row = session.execute(
        select(JobRaw, JobAnalysis, Application)
        .outerjoin(JobAnalysis, JobAnalysis.job_id == JobRaw.id)
        .outerjoin(Application, Application.job_id == JobRaw.id)
        .where(JobRaw.id == job_id)
    ).first()
    if row is None:
        return None
    job, analysis, app = row
    if analysis is None:
        analysis = JobAnalysis(job_id=job.id, extracted_json={}, score=0)
    liked, disliked = feedback_sets(session)
    return Candidate(
        job, analysis, app, base_points(analysis), feedback_adjustment(features(job, analysis), liked, disliked)
    )


# --------------------------------------------------------------------------- status changes


class UnknownJob(LookupError):
    pass


def get_application(session: Session, job_id: int) -> Application | None:
    return session.execute(select(Application).where(Application.job_id == job_id)).scalar_one_or_none()


def ensure_application(session: Session, job_id: int, now: datetime | None = None) -> Application:
    app = get_application(session, job_id)
    if app is None:
        if session.get(JobRaw, job_id) is None:
            raise UnknownJob(job_id)
        now = now or utcnow()
        app = Application(job_id=job_id, status="new", created_at=now, updated_at=now)
        session.add(app)
        session.flush()
    return app


def set_status(
    session: Session,
    job_id: int,
    status: str | None = None,
    *,
    notes: str | None = None,
    now: datetime | None = None,
) -> Application:
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    now = now or utcnow()
    app = ensure_application(session, job_id, now)
    if status is not None and status != app.status:
        app.status = status
        app.status_changed_at = now
        if status in APPLIED_OR_LATER and app.applied_at is None:
            app.applied_at = now
    if notes is not None:
        app.notes = notes or None
    app.updated_at = now
    session.flush()
    return app


def mark_notified(session: Session, job_id: int, now: datetime | None = None) -> Application:
    now = now or utcnow()
    app = ensure_application(session, job_id, now)
    if app.notified_at is None:
        app.notified_at = now
    session.flush()
    return app


def tracked(session: Session, statuses: set[str] | None = None) -> list[tuple[Application, JobRaw, JobAnalysis | None]]:
    stmt = (
        select(Application, JobRaw, JobAnalysis)
        .join(JobRaw, JobRaw.id == Application.job_id)
        .outerjoin(JobAnalysis, JobAnalysis.job_id == JobRaw.id)
        .order_by(Application.updated_at.desc(), Application.id.desc())
    )
    if statuses:
        stmt = stmt.where(Application.status.in_(statuses))
    return [tuple(r) for r in session.execute(stmt).all()]


# --------------------------------------------------------------------------- settings and stats


def get_setting(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(AppSetting, key)
    return row.value if row is not None else default


def put_setting(session: Session, key: str, value: Any, now: datetime | None = None) -> None:
    row = session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value=value, updated_at=now or utcnow()))
    else:
        row.value = value
        row.updated_at = now or utcnow()
    session.flush()


def pipeline_stats(session: Session, *, min_points: float, now: datetime | None = None) -> dict:
    now = now or utcnow()
    jobs = session.execute(select(func.count()).select_from(JobRaw)).scalar() or 0
    new_24h = (
        session.execute(
            select(func.count()).select_from(JobRaw).where(JobRaw.created_at >= now - timedelta(days=1))
        ).scalar()
        or 0
    )
    eligibility: Counter[str] = Counter()
    strong = 0
    for analysis in session.execute(select(JobAnalysis)).scalars():
        flag = match_info(analysis).get("eligible")
        eligibility[{True: "eligible", False: "ineligible"}.get(flag, "unknown")] += 1
        if flag is not False and base_points(analysis) >= min_points:
            strong += 1
    statuses = dict(
        session.execute(select(Application.status, func.count()).group_by(Application.status)).tuples().all()
    )
    notified = (
        session.execute(
            select(func.count()).select_from(Application).where(Application.notified_at.is_not(None))
        ).scalar()
        or 0
    )
    last_run = session.execute(select(Run).order_by(Run.started_at.desc(), Run.id.desc()).limit(1)).scalar()
    return {
        "jobs": jobs,
        "new_24h": new_24h,
        "analyzed": sum(eligibility.values()),
        "eligibility": dict(eligibility),
        "strong": strong,
        "notified": notified,
        "statuses": {s: statuses.get(s, 0) for s in STATUSES if statuses.get(s)},
        "last_run": {"type": last_run.type, "status": last_run.status, "started_at": _aware(last_run.started_at)}
        if last_run
        else None,
    }
