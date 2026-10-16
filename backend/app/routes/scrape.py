"""Manual scrape trigger (POST /api/scrape)."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.database import get_db
from app.models import Source
from app.queue import enqueue_scrape_task
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api", tags=["scrape"])


class ScrapeRequest(BaseModel):
    """Optional body to trigger scrape for specific source or all."""

    source_id: int | None = None  # If omitted, scrape all enabled sources


@router.post("/scrape")
async def scrape(request: ScrapeRequest | None = None, db: AsyncSession = Depends(get_db)) -> dict:
    """Enqueue scrape task(s). If source_id given, scrape that source; else scrape all enabled."""
    if request and request.source_id is not None:
        result = await db.execute(select(Source).where(Source.id == request.source_id, Source.enabled.is_(True)))
        source = result.scalar_one_or_none()
        if not source:
            raise HTTPException(status_code=404, detail="Source not found or disabled")
        job_id = enqueue_scrape_task(source.id, source.config)
        if not job_id:
            raise HTTPException(status_code=503, detail="Queue unavailable")
        return {"job_id": job_id, "source_id": source.id, "message": "Scrape task enqueued"}

    result = await db.execute(select(Source).where(Source.enabled.is_(True)))
    sources = result.scalars().all()
    if not sources:
        raise HTTPException(status_code=400, detail="No enabled sources to scrape")
    job_ids = []
    for s in sources:
        jid = enqueue_scrape_task(s.id, s.config)
        if jid:
            job_ids.append({"source_id": s.id, "job_id": jid})
    return {"jobs": job_ids, "message": f"Enqueued {len(job_ids)} scrape task(s)"}
