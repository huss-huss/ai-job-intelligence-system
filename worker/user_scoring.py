"""Per-user scores: every user's fit, hire probability and rank from *their* profile.

Why scores are stored (user_job_scores) instead of computed per request: the expensive part of
analysis is fetching and extracting facts (and LLM calls when configured), and that is done once
per job. Scoring those facts against a profile is pure rules, about a millisecond per job, so the
analysis run scores each new job for every user (a handful) right after the public score, and the
digest, /top and /today just read rows. A profile change rescores that user's recent jobs
(`rescore_user`), and `score_missing` fills in anything without a row (jobs analyzed before the
user joined, or before this table existed).

`jobs_analysis` stays the public score, from the deployment profile.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime

from app.models import JobAnalysis, JobRaw, User, UserJobScore
from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from worker.ai import ExtractedFields, Extraction, HeuristicExtractor
from worker.pipeline import ScoredJob, score_posting
from worker.profile import CandidateProfile

logger = logging.getLogger(__name__)


def profile_for(user: User) -> CandidateProfile:
    """The user's own profile; the owner's NULL profile means the deployment's (PROFILE_YAML ...)."""
    if user.profile is not None:
        return CandidateProfile.model_validate(user.profile)
    from worker.tasks import _load_profile

    return _load_profile()


def stored_extraction(analysis: JobAnalysis | None, posting) -> Extraction:
    """The facts extracted at analysis time (kept in extracted_json); rules if there is no analysis."""
    data = (analysis.extracted_json if analysis else None) or {}
    fields = {k: v for k, v in data.items() if k in ExtractedFields.model_fields}
    if not fields:
        return HeuristicExtractor().extract(posting)
    try:
        return Extraction(ExtractedFields.model_validate(fields), str(data.get("method") or "heuristic"))
    except ValueError:
        return HeuristicExtractor().extract(posting)


def score_values(scored: ScoredJob) -> dict:
    m = scored.match
    return {
        "score": m.score,
        "hire_probability": scored.hire.score,
        "rank": scored.rank,
        "eligible": m.eligible,
        "match": m.to_dict(),
        "hire": scored.hire.to_dict(),
    }


CHUNK = 500  # ids per IN (...) list


def _expire_cached(session: Session) -> None:
    for obj in list(session.identity_map.values()):
        if isinstance(obj, UserJobScore):
            session.expire(obj)


def replace_scores(session: Session, rows: list[dict], *, user_id: int | None = None) -> None:
    """Write score rows (dicts with user_id, job_id and score_values()) in a few statements.

    Old rows for the same jobs (and `user_id`, or every user when None) are deleted first, then the new
    ones go in as one multi-row INSERT per chunk. Row-by-row upserts would cost a database round trip per
    job and user, which is most of the time on a remote serverless database.
    """
    session.flush()
    job_ids = sorted({r["job_id"] for r in rows})
    for i in range(0, len(job_ids), CHUNK):
        stmt = delete(UserJobScore).where(UserJobScore.job_id.in_(job_ids[i : i + CHUNK]))
        if user_id is not None:
            stmt = stmt.where(UserJobScore.user_id == user_id)
        session.execute(stmt.execution_options(synchronize_session=False))
    # Rows with NULLs in different columns can't share a multi-row VALUES batch: group them.
    rows = sorted(rows, key=lambda r: tuple(sorted(k for k, v in r.items() if v is None)))
    for i in range(0, len(rows), CHUNK):
        session.execute(insert(UserJobScore), rows[i : i + CHUNK])
    _expire_cached(session)


def _score_rows(
    session: Session,
    user: User,
    rows: Iterable[tuple[JobRaw, JobAnalysis | None]],
    profile: CandidateProfile,
    now: datetime,
) -> int:
    from worker.tasks import _to_posting

    out = []
    for job, analysis in rows:
        posting = _to_posting(job)
        scored = score_posting(posting, stored_extraction(analysis, posting), profile, now=now)
        out.append({"user_id": user.id, "job_id": job.id, "scored_at": now, **score_values(scored)})
    if out:
        replace_scores(session, out, user_id=user.id)
    return len(out)


def rescore_user(
    session: Session,
    user: User,
    *,
    since: datetime,
    profile: CandidateProfile | None = None,
    now: datetime | None = None,
) -> int:
    """Rescore every analyzed job stored since `since` for one user (after a profile change)."""
    now = now or datetime.now(UTC)
    rows = session.execute(
        select(JobRaw, JobAnalysis).join(JobAnalysis, JobAnalysis.job_id == JobRaw.id).where(JobRaw.created_at >= since)
    ).all()
    return _score_rows(session, user, rows, profile or profile_for(user), now)


def score_missing(
    session: Session,
    user: User,
    *,
    since: datetime,
    profile: CandidateProfile | None = None,
    now: datetime | None = None,
) -> int:
    """Score analyzed jobs stored since `since` that have no row for this user yet. One query when none."""
    now = now or datetime.now(UTC)
    scored_ids = select(UserJobScore.job_id).where(UserJobScore.user_id == user.id)
    rows = session.execute(
        select(JobRaw, JobAnalysis)
        .join(JobAnalysis, JobAnalysis.job_id == JobRaw.id)
        .where(JobRaw.created_at >= since, JobRaw.id.not_in(scored_ids))
    ).all()
    if not rows:
        return 0
    return _score_rows(session, user, rows, profile or profile_for(user), now)


def scores_for_users(
    job_id: int,
    posting,
    extraction: Extraction,
    public: ScoredJob,
    user_profiles: list[tuple[User, CandidateProfile | None]],
    now: datetime,
) -> list[dict]:
    """The analysis run's rows for one job, one per user. A `None` profile scores like the public one."""
    out = []
    for user, profile in user_profiles:
        scored = public if profile is None else score_posting(posting, extraction, profile, now=now)
        out.append({"user_id": user.id, "job_id": job_id, "scored_at": now, **score_values(scored)})
    return out


def user_profiles(session: Session) -> list[tuple[User, CandidateProfile | None]]:
    """Every user with a valid profile. A user without one of their own scores like the public profile."""
    out: list[tuple[User, CandidateProfile | None]] = []
    for user in session.execute(select(User).order_by(User.id)).scalars():
        if user.profile is None:
            out.append((user, None))
            continue
        try:
            out.append((user, CandidateProfile.model_validate(user.profile)))
        except ValueError:
            logger.warning("user %s has an invalid profile; skipped", user.id)
    return out
