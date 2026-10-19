"""Scheduled discovery for serverless deployments (Vercel Cron)."""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Source
from app.queue import dispatch_scrape
from app.telegram.config import telegram_settings

router = APIRouter(prefix="/api/cron", tags=["cron"])


@router.get("/discover")
async def discover(authorization: str = Header(""), db: AsyncSession = Depends(get_db)) -> dict:
    """Fetch every enabled source; new jobs are analyzed in the same run (inline) or by the worker."""
    expected = f"Bearer {settings.cron_secret}"
    if not settings.cron_secret or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")
    sources = (await db.execute(select(Source).where(Source.enabled.is_(True)).order_by(Source.id))).scalars().all()
    await db.commit()  # don't hold a connection open while sources are fetched
    results = []
    for source in sources:
        try:
            job_id = await dispatch_scrape(source.id, source.config)
            results.append({"source_id": source.id, "job_id": job_id})
        except Exception as exc:  # one failing source shouldn't stop the others
            results.append({"source_id": source.id, "error": str(exc)[:300]})
    response: dict = {"sources": len(sources), "results": results}
    if telegram_settings.can_send:
        # Daily Telegram digest rides on this run (Vercel Hobby allows one cron a day). It never raises.
        from starlette.concurrency import run_in_threadpool

        from app.telegram.digest import run_digest

        response["telegram"] = await run_in_threadpool(run_digest)
    return response
