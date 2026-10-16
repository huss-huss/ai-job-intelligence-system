"""Scrape/analyze runs listing."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Run
from app.schemas import RunOut

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("", response_model=list[RunOut])
async def list_runs(
    db: AsyncSession = Depends(get_db),
    type: str | None = Query(None, description="Filter by type: scrape | analyze"),
    limit: int = Query(50, ge=1, le=200),
) -> list[RunOut]:
    """List recent runs (scrape/analyze) for observability."""
    stmt = select(Run).order_by(Run.started_at.desc()).limit(limit)
    if type:
        stmt = stmt.where(Run.type == type)
    result = await db.execute(stmt)
    runs = result.scalars().all()
    return [RunOut.model_validate(r) for r in runs]


@router.delete("")
async def clear_runs(db: AsyncSession = Depends(get_db)) -> dict:
    """Delete all runs (for cleanup/testing)."""
    result = await db.execute(delete(Run))
    await db.commit()
    return {"message": f"Deleted {result.rowcount} run(s)", "deleted": result.rowcount}
