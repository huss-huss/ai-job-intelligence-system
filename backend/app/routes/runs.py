"""Scrape/analyze runs listing."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Viewer, optional_viewer
from app.database import get_db
from app.models import Run
from app.schemas import RunOut

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("", response_model=list[RunOut])
async def list_runs(
    db: AsyncSession = Depends(get_db),
    type: str | None = Query(None, description="Filter by type: scrape | analyze"),
    limit: int = Query(50, ge=1, le=200),
    viewer: Viewer | None = Depends(optional_viewer),
) -> list[RunOut]:
    """List recent runs (scrape/analyze/telegram) for observability.

    Telegram runs record which jobs went to whom; the public only sees their totals. LLM enrichment
    runs count extractions per user id; only the owner sees that split.
    """
    stmt = select(Run).order_by(Run.started_at.desc()).limit(limit)
    if type:
        stmt = stmt.where(Run.type == type)
    result = await db.execute(stmt)
    runs = [RunOut.model_validate(r) for r in result.scalars().all()]
    for run in runs:
        if viewer is None and run.type == "telegram":
            run.stats = public_telegram_stats(run.stats)
        if (viewer is None or not viewer.is_owner) and run.type == "llm_enrich" and run.stats:
            run.stats = {k: v for k, v in run.stats.items() if k not in OWNER_ONLY_LLM_STATS}
    return runs


PUBLIC_TELEGRAM_STATS = ("users", "sent", "failed")
OWNER_ONLY_LLM_STATS = ("selected_by_user", "enriched_by_user")


def public_telegram_stats(stats: dict | None) -> dict:
    return {k: v for k, v in (stats or {}).items() if k in PUBLIC_TELEGRAM_STATS and isinstance(v, int)}


@router.delete("")
async def clear_runs(db: AsyncSession = Depends(get_db)) -> dict:
    """Delete all runs (for cleanup/testing)."""
    result = await db.execute(delete(Run))
    await db.commit()
    return {"message": f"Deleted {result.rowcount} run(s)", "deleted": result.rowcount}
