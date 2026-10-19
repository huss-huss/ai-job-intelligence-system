"""Jobs list and detail with filtering and pagination.

Without a token every score is the public one (jobs_analysis, from the deployment profile). With a
token (X-User-Token or X-Admin-Token) the score fields are the viewer's own (user_job_scores): the
response has the same shape, with `analysis.score`, `analysis.skills` and `extracted_json.match`,
`.hire`, `.hire_probability` and `.rank` replaced, and `extracted_json.score_scope` set to "user".
A job the viewer has no score row for yet (stored before they joined and older than the 30-day
catch-up) keeps the public score, marked `score_scope: "public"`, and sorts after the viewer's
scored jobs. Sorting and the min_score and eligible filters use the same values that are returned.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import ColumnElement, and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import Viewer, optional_viewer
from app.database import get_db
from app.models import Application, JobAnalysis, JobRaw, UserJobScore
from app.routes.applications import parse_statuses
from app.schemas import JobAnalysisOut, JobOut, Paginated

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

ELIGIBILITY_VALUES = ("true", "false", "unknown")


def parse_eligible(value: str) -> set[str] | None:
    """`any` (or every value) means no filter; otherwise a comma-separated subset of true/false/unknown."""
    wanted = {v.strip().lower() for v in value.split(",") if v.strip()}
    if not wanted or "any" in wanted:
        return None
    bad = wanted - set(ELIGIBILITY_VALUES)
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"eligible must be 'any' or a comma-separated subset of {', '.join(ELIGIBILITY_VALUES)}",
        )
    return None if wanted == set(ELIGIBILITY_VALUES) else wanted


def eligibility_filter(wanted: set[str]) -> ColumnElement[bool]:
    """Condition on JobRaw.id for jobs whose stored match.eligible is in `wanted`.

    The worker stores match.eligible as JSON true/false/null. Jobs with null, a missing key,
    or no analysis at all count as "unknown".
    """
    flag = JobAnalysis.extracted_json[("match", "eligible")].astext  # 'true' | 'false' | NULL
    conditions = []
    for value in ("true", "false"):
        if value in wanted:
            conditions.append(JobRaw.id.in_(select(JobAnalysis.job_id).where(flag == value)))
    if "unknown" in wanted:
        known = select(JobAnalysis.job_id).where(flag.in_(("true", "false")))
        conditions.append(JobRaw.id.not_in(known))
    return or_(*conditions)


def user_eligibility_filter(wanted: set[str]) -> ColumnElement[bool]:
    """Like eligibility_filter, on the viewer's own score row (UserJobScore, outer-joined); jobs without
    one fall back to the public flag."""
    mine = UserJobScore.job_id.is_not(None)
    conditions = []
    for value in ("true", "false"):
        if value in wanted:
            conditions.append(UserJobScore.eligible.is_(value == "true"))
    if "unknown" in wanted:
        conditions.append(and_(mine, UserJobScore.eligible.is_(None)))
    public = eligibility_filter(wanted)
    return or_(*conditions, and_(UserJobScore.job_id.is_(None), public))


def status_filter(wanted: set[str], user_id: int) -> ColumnElement[bool]:
    """Condition on JobRaw.id for jobs whose tracker status *for this user* is in `wanted`; untracked jobs
    count as "new"."""
    mine = Application.user_id == user_id
    conditions = [JobRaw.id.in_(select(Application.job_id).where(mine, Application.status.in_(sorted(wanted))))]
    if "new" in wanted:
        conditions.append(JobRaw.id.not_in(select(Application.job_id).where(mine)))
    return or_(*conditions)


def rank_column() -> ColumnElement[float]:
    """extracted_json.rank (0-100, see worker/hire.py rank_score) as a number."""
    return JobAnalysis.extracted_json["rank"].as_float()


def _join_user_scores(stmt, user_id: int):
    """LEFT JOIN the viewer's score rows: the (user_id, job_id) primary key, one index lookup per job."""
    return stmt.outerjoin(UserJobScore, and_(UserJobScore.job_id == JobRaw.id, UserJobScore.user_id == user_id))


def _analysis_out(job: JobRaw, mine: UserJobScore | None, viewer: Viewer | None) -> JobAnalysisOut | None:
    """The public analysis, or for a viewer the same shape carrying their own score."""
    if job.analysis is None:
        return None
    out = JobAnalysisOut.model_validate(job.analysis)
    if viewer is None:
        return out
    extracted = dict(out.extracted_json or {})
    if mine is None:
        out.extracted_json = {**extracted, "score_scope": "public"}
        return out
    match = {**(mine.match or {}), "score": mine.score, "eligible": mine.eligible}
    extracted.update(match=match, hire_probability=mine.hire_probability, rank=mine.rank, score_scope="user")
    if mine.hire is not None:
        extracted["hire"] = mine.hire
    else:
        extracted.pop("hire", None)
    out.extracted_json = extracted
    out.score = mine.score / 100  # 0-1, like the public score
    out.skills = match.get("matched_skills") or extracted.get("skills") or None
    return out


def _job_out(job: JobRaw, mine: UserJobScore | None = None, viewer: Viewer | None = None) -> JobOut:
    return JobOut(
        id=job.id,
        source_id=job.source_id,
        url=job.url,
        title=job.title,
        company=job.company,
        location=job.location,
        description=job.description,
        posted_at=job.posted_at,
        content_hash=job.content_hash,
        created_at=job.created_at,
        analysis=_analysis_out(job, mine, viewer),
    )


@router.get("", response_model=Paginated[JobOut])
async def list_jobs(
    db: AsyncSession = Depends(get_db),
    source: int | None = Query(None, description="Filter by source id"),
    min_score: float | None = Query(None, ge=0, le=1),
    q: str | None = Query(None, description="Search in title/company"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort: str = Query(
        "score",
        pattern="^(rank|score|recent)$",
        description="rank: fit and hire probability combined (extracted_json.rank); score: best fit first; "
        "recent: newest. With a token: the viewer's own rank and fit",
    ),
    eligible: str = Query(
        "any",
        description="Location eligibility: 'any' (default), or a comma-separated subset of "
        "true, false, unknown, e.g. 'true,unknown' hides jobs not open to your location",
    ),
    status: str | None = Query(
        None,
        description="Tracker status: comma-separated subset of new, saved, applied, interviewing, offer, "
        "rejected, not_interested ('new' includes untracked jobs). Private: needs a token",
    ),
    viewer: Viewer | None = Depends(optional_viewer),
) -> Paginated[JobOut]:
    """List jobs with optional filters and pagination. Never includes tracker data.

    With a token the scores, sorting and score/eligibility filters are the viewer's own (module docstring).
    """
    statuses = parse_statuses(status)
    if statuses is not None and viewer is None:
        # Filtering by status would reveal which jobs someone applied to.
        raise HTTPException(status_code=401, detail="The status filter is private: send a token")
    wanted = parse_eligible(eligible)
    personal = viewer is not None
    base = select(JobRaw).options(selectinload(JobRaw.analysis))
    count_stmt = select(func.count()).select_from(JobRaw)
    if personal:
        base = _join_user_scores(base.add_columns(UserJobScore), viewer.user_id)
        count_stmt = _join_user_scores(count_stmt, viewer.user_id)
    if source is not None:
        base = base.where(JobRaw.source_id == source)
        count_stmt = count_stmt.where(JobRaw.source_id == source)
    if q:
        q_pattern = f"%{q}%"
        base = base.where(
            or_(JobRaw.title.ilike(q_pattern), JobRaw.company.ilike(q_pattern))
        )
        count_stmt = count_stmt.where(
            or_(JobRaw.title.ilike(q_pattern), JobRaw.company.ilike(q_pattern))
        )

    # Subquery for min_score: only job_ids that have analysis with score >= min_score
    if min_score is not None:
        subq = select(JobAnalysis.job_id).where(JobAnalysis.score >= min_score)
        condition = JobRaw.id.in_(subq)
        if personal:  # the viewer's fit is stored 0-100
            condition = or_(
                UserJobScore.score >= min_score * 100, and_(UserJobScore.job_id.is_(None), condition)
            )
        base = base.where(condition)
        count_stmt = count_stmt.where(condition)

    if wanted is not None:
        condition = user_eligibility_filter(wanted) if personal else eligibility_filter(wanted)
        base = base.where(condition)
        count_stmt = count_stmt.where(condition)

    if statuses is not None:
        condition = status_filter(statuses, viewer.user_id)
        base = base.where(condition)
        count_stmt = count_stmt.where(condition)

    total = (await db.execute(count_stmt)).scalar() or 0
    # The viewer's own values first; jobs without a row for them follow, by the public values.
    mine_rank = (UserJobScore.rank.desc().nulls_last(), UserJobScore.score.desc().nulls_last()) if personal else ()
    mine_score = (UserJobScore.score.desc().nulls_last(),) if personal else ()
    if sort == "rank":
        # Rows analyzed before hire probability existed have no rank; they sort last until re-analyzed.
        base = base.outerjoin(JobAnalysis, JobAnalysis.job_id == JobRaw.id).order_by(
            *mine_rank,
            rank_column().desc().nulls_last(),
            JobAnalysis.score.desc().nulls_last(),
            JobRaw.created_at.desc(),
        )
    elif sort == "score":
        base = base.outerjoin(JobAnalysis, JobAnalysis.job_id == JobRaw.id).order_by(
            *mine_score, JobAnalysis.score.desc().nulls_last(), JobRaw.created_at.desc()
        )
    else:
        base = base.order_by(JobRaw.created_at.desc())
    base = base.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(base)
    if personal:
        items = [_job_out(job, mine, viewer) for job, mine in result.all()]
    else:
        items = [_job_out(job) for job in result.scalars().all()]

    pages = (total + page_size - 1) // page_size if total else 0
    return Paginated(items=items, total=total, page=page, page_size=page_size, pages=pages)


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: int, db: AsyncSession = Depends(get_db), viewer: Viewer | None = Depends(optional_viewer)
) -> JobOut:
    """Get a single job by id; with a token, scored for the viewer like the list."""
    result = await db.execute(
        select(JobRaw).options(selectinload(JobRaw.analysis)).where(JobRaw.id == job_id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    mine = None
    if viewer is not None:
        mine = (
            await db.execute(
                select(UserJobScore).where(UserJobScore.user_id == viewer.user_id, UserJobScore.job_id == job_id)
            )
        ).scalar_one_or_none()
    return _job_out(job, mine, viewer)


@router.delete("")
async def clear_jobs(db: AsyncSession = Depends(get_db)) -> dict:
    """Delete all jobs (raw + analysis) for cleanup/testing."""
    # Delete analyses first (due to foreign key)
    deleted_analysis = await db.execute(delete(JobAnalysis))
    # Then delete raw jobs
    deleted_raw = await db.execute(delete(JobRaw))
    await db.commit()
    return {
        "message": f"Deleted {deleted_raw.rowcount} job(s) and {deleted_analysis.rowcount} analysis record(s)",
        "deleted_jobs": deleted_raw.rowcount,
        "deleted_analyses": deleted_analysis.rowcount,
    }
