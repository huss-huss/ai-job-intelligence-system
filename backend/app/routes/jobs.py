"""Jobs list and detail with filtering and pagination."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import delete, select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import JobRaw, JobAnalysis
from app.schemas import JobAnalysisOut, JobOut, Paginated

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("", response_model=Paginated[JobOut])
async def list_jobs(
    db: AsyncSession = Depends(get_db),
    source: int | None = Query(None, description="Filter by source id"),
    min_score: float | None = Query(None, ge=0, le=1),
    q: str | None = Query(None, description="Search in title/company"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> Paginated[JobOut]:
    """List jobs with optional filters and pagination."""
    base = select(JobRaw).options(selectinload(JobRaw.analysis))
    count_stmt = select(func.count()).select_from(JobRaw)
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
        base = base.where(JobRaw.id.in_(subq))
        count_stmt = count_stmt.where(JobRaw.id.in_(subq))

    total = (await db.execute(count_stmt)).scalar() or 0
    base = base.order_by(JobRaw.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(base)
    jobs = result.scalars().all()

    items = []
    for j in jobs:
        out = JobOut(
            id=j.id,
            source_id=j.source_id,
            url=j.url,
            title=j.title,
            company=j.company,
            location=j.location,
            description=j.description,
            posted_at=j.posted_at,
            content_hash=j.content_hash,
            created_at=j.created_at,
            analysis=JobAnalysisOut.model_validate(j.analysis) if j.analysis else None,
        )
        items.append(out)

    pages = (total + page_size - 1) // page_size if total else 0
    return Paginated(items=items, total=total, page=page, page_size=page_size, pages=pages)


@router.get("/{job_id}", response_model=JobOut)
async def get_job(job_id: int, db: AsyncSession = Depends(get_db)) -> JobOut:
    """Get a single job by id."""
    result = await db.execute(
        select(JobRaw).options(selectinload(JobRaw.analysis)).where(JobRaw.id == job_id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
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
        analysis=JobAnalysisOut.model_validate(job.analysis) if job.analysis else None,
    )


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
