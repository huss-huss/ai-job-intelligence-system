"""Daily Telegram digest, one per user, sent at the end of the daily cron run (no extra cron needed).

For every user with a chat who hasn't paused it, picks up to their digest size (default
TELEGRAM_DIGEST_SIZE) jobs that

* were stored within TELEGRAM_DIGEST_MAX_AGE_DAYS,
* are eligible *for that user's profile* (strict, TELEGRAM_DIGEST_STRICT, the default: confirmed open
  and nothing blocking such as a required language; with it off, unknown eligibility is fine too),
* that user hasn't acted on and was never sent before (their applications.notified_at is empty),
* score at least their minimum (default TELEGRAM_DIGEST_MIN_SCORE) on their own score after their
  own feedback adjustment (app/tracker.py),

and sends a header plus one card per job. A job is marked notified only after Telegram accepted its
message, so a failed send is retried the next day and a sent one never repeats for that user.

Users are isolated: one user's failure (blocked bot, bad data) is recorded and the next user still
gets theirs. The whole run stops after TELEGRAM_DIGEST_MAX_SECONDS so it can't eat the cron budget.
`run_digest` never raises: trouble is logged and recorded in a `runs` row of type "telegram"
(per-user details keyed by internal user id; GET /api/runs shows the public only the totals).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import tracker, users
from app.models import Run, User
from app.telegram.bot import digest_min_score, digest_size, is_paused, send_cards, telegram_filter
from app.telegram.client import TelegramClient, TelegramError
from app.telegram.config import TelegramSettings, telegram_settings
from app.telegram.render import job_card, zone

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_FAILURES = 3  # this user's chat keeps failing: stop instead of burning the cron's time budget


def pick(session: Session, settings: TelegramSettings, now: datetime, user: User) -> list[tracker.Candidate]:
    users.ensure_scores(session, user, now)
    cands = tracker.candidates(
        session,
        user.id,
        since=now - timedelta(days=settings.digest_max_age_days),
        min_points=digest_min_score(user, settings),
        only_unnotified=True,
    )
    return telegram_filter(cands, settings)[: max(0, digest_size(user, settings))]


def send_digest(
    session: Session, client: TelegramClient, settings: TelegramSettings, now: datetime, user: User
) -> dict:
    """One user's digest."""
    stats: dict = {"candidates": 0, "sent": 0, "failed": 0, "errors": []}
    if is_paused(user):
        stats["skipped"] = "paused"
        return stats
    chat_id = user.telegram_chat_id
    floor = digest_min_score(user, settings)
    picks = pick(session, settings, now, user)
    session.commit()
    stats["candidates"] = len(picks)
    if not picks:
        if settings.digest_send_empty:
            client.send_message(chat_id, f"No new strong matches today (score ≥ {floor:.0f}).")
            stats["empty_notice"] = True
        return stats

    client.send_message(
        chat_id,
        f"<b>FirstFetch: {len(picks)} new match{'es' if len(picks) != 1 else ''}</b>"
        f" · score ≥ {floor:.0f} · /top for more",
    )
    failures_in_a_row = 0
    for c in picks:
        try:
            stats["sent"] += send_cards(session, client, chat_id, user, [c], settings, now)
            failures_in_a_row = 0
        except TelegramError as exc:
            session.rollback()
            stats["failed"] += 1
            stats["errors"].append({"job_id": c.job.id, "error": str(exc)[:300]})
            logger.warning("Telegram digest: a card was not sent: %s", exc.error_code)
            failures_in_a_row += 1
            if failures_in_a_row >= MAX_CONSECUTIVE_FAILURES:
                stats["stopped_early"] = True
                break
    return stats


def send_all(
    session: Session,
    client: TelegramClient,
    settings: TelegramSettings,
    now: datetime,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Every active user's digest, each isolated from the others' failures."""
    started = clock()
    users.ensure_owner(session, settings.chat_id, now)
    session.commit()
    recipients = users.digest_users(session)
    total: dict = {"users": len(recipients), "sent": 0, "failed": 0, "per_user": {}}
    for user in recipients:
        if clock() - started > settings.digest_max_seconds:
            total["per_user"][str(user.id)] = {"skipped": "time budget"}
            total["stopped_early"] = True
            continue
        try:
            stats = send_digest(session, client, settings, now, user)
        except Exception as exc:  # this user's header or data failed; the next user still gets theirs
            session.rollback()
            stats = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            total["failed"] += 1
            logger.warning("Telegram digest failed for one user: %s", type(exc).__name__)
        total["sent"] += stats.get("sent", 0)
        total["failed"] += stats.get("failed", 0)
        total["per_user"][str(user.id)] = stats
    return total


def preview(session: Session, settings: TelegramSettings, now: datetime, user: User | None = None) -> list[str]:
    """What the digest would send a user (default: the owner) right now, as plain text (CLI --dry-run)."""
    tz = zone(settings.timezone)
    user = user or users.ensure_owner(session, settings.chat_id, now)
    return [job_card(c, tz) for c in pick(session, settings, now, user)]


def run_digest(
    *,
    settings: TelegramSettings | None = None,
    session_factory: Callable | None = None,
    client_factory: Callable[[TelegramSettings], TelegramClient] | None = None,
    now: datetime | None = None,
) -> dict:
    """Send today's digests and record a Run. Never raises."""
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
                    stats = send_all(session, client, settings, now)
                    stats["api_calls"] = len(client.calls)
                errors = [u for u in stats["per_user"].values() if u.get("error")]
                if errors and len(errors) == len(stats["per_user"]):
                    run.status = "failed"
                    stats["error"] = errors[0]["error"]
                else:
                    run.status = "completed" if not stats.get("failed") else "completed_with_errors"
            except Exception as exc:  # a database error outside any one user's digest
                session.rollback()
                stats = {**stats, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                run.status = "failed"
                logger.warning("Telegram digest failed: %s", type(exc).__name__)
            run.ended_at = tracker.utcnow()
            run.stats = stats
            session.commit()
            stats["run_id"] = run.id
    except Exception as exc:  # couldn't even record the run
        logger.error("Telegram digest could not run: %s", type(exc).__name__)
        stats = {**stats, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
    return stats
