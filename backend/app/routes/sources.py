"""Sources CRUD and scrape trigger."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Source
from app.schemas import SourceCreate, SourceOut, SourceUpdate
from app.queue import dispatch_scrape

router = APIRouter(prefix="/api/sources", tags=["sources"])


@router.get("", response_model=list[SourceOut])
async def list_sources(db: AsyncSession = Depends(get_db)) -> list[SourceOut]:
    """List all configured sources."""
    result = await db.execute(select(Source).order_by(Source.id))
    sources = result.scalars().all()
    return [SourceOut.model_validate(s) for s in sources]


@router.post("", response_model=SourceOut, status_code=201)
async def create_source(body: SourceCreate, db: AsyncSession = Depends(get_db)) -> SourceOut:
    """Create a new job source."""
    source = Source(
        name=body.name,
        base_url=body.base_url,
        config=body.config,
        enabled=body.enabled,
    )
    db.add(source)
    await db.flush()
    await db.refresh(source)
    return SourceOut.model_validate(source)


@router.get("/{source_id}", response_model=SourceOut)
async def get_source(source_id: int, db: AsyncSession = Depends(get_db)) -> SourceOut:
    """Get a source by id."""
    result = await db.execute(select(Source).where(Source.id == source_id))
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    return SourceOut.model_validate(source)


@router.patch("/{source_id}", response_model=SourceOut)
async def update_source(
    source_id: int, body: SourceUpdate, db: AsyncSession = Depends(get_db)
) -> SourceOut:
    """Update a source (partial)."""
    result = await db.execute(select(Source).where(Source.id == source_id))
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    if body.name is not None:
        source.name = body.name
    if body.base_url is not None:
        source.base_url = body.base_url
    if body.config is not None:
        source.config = body.config
    if body.enabled is not None:
        source.enabled = body.enabled
    await db.flush()
    await db.refresh(source)
    return SourceOut.model_validate(source)


@router.post("/{source_id}/scrape")
async def trigger_scrape(source_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """Enqueue a scrape task for this source. Returns queue job id."""
    result = await db.execute(select(Source).where(Source.id == source_id))
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    job_id = await dispatch_scrape(source_id, source.config)
    if not job_id:
        raise HTTPException(status_code=503, detail="Queue unavailable")
    return {"job_id": job_id, "message": "Scrape task enqueued"}
