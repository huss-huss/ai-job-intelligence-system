"""Daily Telegram digest, sent at the end of the daily cron run (no extra cron needed).

Picks up to TELEGRAM_DIGEST_SIZE jobs that

* were stored within TELEGRAM_DIGEST_MAX_AGE_DAYS,
* are eligible or of unknown eligibility (never "not open to your location"),
* nobody acted on yet and were never sent before (applications.notified_at is empty),
* score at least TELEGRAM_DIGEST_MIN_SCORE after the feedback adjustment (app/tracker.py),

and sends a header plus one card per job. A job is marked notified only after Telegram
accepted its message, so a failed send is retried the next day and a sent one never repeats.

`run_digest` never raises: Telegram or database trouble is logged and recorded in a
`runs` row of type "telegram", and the cron run carries on.
"""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import tracker
from app.models import Run
from app.telegram.bot import is_paused, send_cards
from app.telegram.client import TelegramClient, TelegramError
from app.telegram.config import TelegramSettings, telegram_settings
from app.telegram.render import job_card, zone

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 3  # Telegram is down: stop instead of burning the cron's time budget


def pick(session: Session, settings: TelegramSettings, now: datetime) -> list[tracker.Candidate]:
    return tracker.candidates(
        session,
        since=now - timedelta(days=settings.digest_max_age_days),
        min_points=settings.digest_min_score,
        only_unnotified=True,
    )[: max(0, settings.digest_size)]


def send_digest(session: Session, client: TelegramClient, settings: TelegramSettings, now: datetime) -> dict:
    stats: dict = {"candidates": 0, "sent": 0, "failed": 0, "errors": []}
    if is_paused(session):
        stats["skipped"] = "paused"
        return stats
    picks = pick(session, settings, now)
    stats["candidates"] = len(picks)
    chat_id = settings.chat_id
    if not picks:
        if settings.digest_send_empty:
            client.send_message(chat_id, f"No new strong matches today (score ≥ {settings.digest_min_score:.0f}).")
            stats["empty_notice"] = True
        return stats

    client.send_message(
        chat_id,
        f"<b>FirstFetch: {len(picks)} new match{'es' if len(picks) != 1 else ''}</b>"
        f" · score ≥ {settings.digest_min_score:.0f} · /top for more",
    )
    failures_in_a_row = 0
    for c in picks:
        try:
            stats["sent"] += send_cards(session, client, chat_id, [c], settings, now)
            failures_in_a_row = 0
        except TelegramError as exc:
            session.rollback()
            stats["failed"] += 1
            stats["errors"].append({"job_id": c.job.id, "error": str(exc)[:300]})
            logger.warning("Telegram digest: job %s not sent: %s", c.job.id, exc)
            failures_in_a_row += 1
            if failures_in_a_row >= MAX_CONSECUTIVE_FAILURES:
                stats["stopped_early"] = True
                break
    return stats


def preview(session: Session, settings: TelegramSettings, now: datetime) -> list[str]:
    """What the digest would send right now, as plain text (CLI --dry-run)."""
    tz = zone(settings.timezone)
    return [job_card(c, tz) for c in pick(session, settings, now)]


def run_digest(
    *,
    settings: TelegramSettings | None = None,
    session_factory: Callable | None = None,
    client_factory: Callable[[TelegramSettings], TelegramClient] | None = None,
    now: datetime | None = None,
) -> dict:
    """Send today's digest and record a Run. Never raises."""
    settings = settings or telegram_settings
    if not settings.can_send:
        return {"skipped": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"}
    now = now or tracker.utcnow()
    if session_factory is None:
        from app.db_sync import get_sync_session as session_factory
    client_factory = client_factory or (lambda s: TelegramClient(s.bot_token, api_base=s.api_base))

    stats: dict = {}
    try:
        with session_factory() as session:
            run = Run(type="telegram", source_id=None, status="running", started_at=now)
            session.add(run)
            session.commit()
            try:
                with client_factory(settings) as client:
                    stats = send_digest(session, client, settings, now)
                    stats["api_calls"] = len(client.calls)
                run.status = "completed" if not stats.get("failed") else "completed_with_errors"
            except Exception as exc:  # header/empty notice failed, or a database error
                session.rollback()
                stats = {**stats, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                run.status = "failed"
                logger.warning("Telegram digest failed: %s", stats["error"])
            run.ended_at = tracker.utcnow()
            run.stats = stats
            session.commit()
            stats["run_id"] = run.id
    except Exception as exc:  # couldn't even record the run
        logger.error("Telegram digest could not run: %s\n%s", exc, traceback.format_exc(limit=3))
        stats = {**stats, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    return stats
