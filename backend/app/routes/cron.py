"""Scheduled discovery for serverless deployments (Vercel Cron)."""

import hmac
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import discovery
from app.config import settings
from app.database import get_db
from app.models import Run, Source
from app.queue import dispatch_llm_enrich, dispatch_scrape
from app.telegram.config import telegram_settings

router = APIRouter(prefix="/api/cron", tags=["cron"])


@router.get("/discover")
async def discover(authorization: str = Header(""), db: AsyncSession = Depends(get_db)) -> dict:
    """Fetch the enabled sources within DISCOVER_MAX_SECONDS, least recently attempted first; new jobs are
    analyzed in the same run (inline) or by the worker. Sources left out go first on the next run."""
    expected = f"Bearer {settings.cron_secret}"
    if not settings.cron_secret or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")
    started = discovery.clock()
    sources = (await db.execute(select(Source).where(Source.enabled.is_(True)).order_by(Source.id))).scalars().all()
    run = None
    if sources:
        sources = discovery.rotation_order(sources, await discovery.last_scrape_attempts(db, [s.id for s in sources]))
        run = Run(type="discover", source_id=None, status="running", stats={"order": [s.id for s in sources]})
        db.add(run)
    await db.commit()  # don't hold a connection open while sources are fetched
    results, skipped = await discovery.scrape_within_budget(
        sources, dispatch_scrape, max_seconds=settings.discover_max_seconds, started=started
    )
    response: dict = {"sources": len(sources), "results": results}
    if skipped:
        response["skipped"] = skipped
    if run is not None:
        # Recorded before enrichment and the digest, so it survives even if those run out of time.
        run.status = "completed"
        run.ended_at = datetime.now(UTC)
        run.stats = {
            "order": [s.id for s in sources],
            "processed": [r["source_id"] for r in results],
            "failed": [r["source_id"] for r in results if "error" in r],
            "skipped": skipped,
            "elapsed_seconds": round(discovery.clock() - started, 1),
            "max_seconds": settings.discover_max_seconds,
        }
        await db.commit()
    if settings.ai_enabled:
        # The day's top jobs by rules rank get LLM extraction, within the budget (worker/budget.py).
        response["llm_enrich"] = await dispatch_llm_enrich()
    if telegram_settings.can_send:
        # Daily Telegram digest rides on this run (Vercel Hobby allows one cron a day). It never raises.
        from starlette.concurrency import run_in_threadpool

        from app.telegram.digest import run_digest

        response["telegram"] = await run_in_threadpool(run_digest)
    return response
