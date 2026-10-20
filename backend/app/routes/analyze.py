"""Manual analyze trigger (POST /api/analyze)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_owner
from app.database import get_db
from app.models import JobRaw
from app.queue import dispatch_analyze

router = APIRouter(prefix="/api", tags=["analyze"])


class AnalyzeRequest(BaseModel):
    """Optional body: job ids to analyze. If omitted, analyze all jobs."""

    job_ids: list[int] | None = None


@router.post("/analyze", dependencies=[Depends(require_owner)])
async def analyze(
    request: AnalyzeRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Enqueue an analyze task. If job_ids given, analyze those; else analyze all jobs in the DB."""
    if request and request.job_ids is not None and len(request.job_ids) > 0:
        job_ids = request.job_ids
    else:
        result = await db.execute(select(JobRaw.id))
        job_ids = list(result.scalars().all())
    if not job_ids:
        raise HTTPException(status_code=400, detail="No jobs to analyze")
    job_id = await dispatch_analyze(job_ids)
    if not job_id:
        raise HTTPException(status_code=503, detail="Queue unavailable")
    return {
        "job_id": job_id,
        "message": "Analyze task enqueued",
        "job_count": len(job_ids),
    }
