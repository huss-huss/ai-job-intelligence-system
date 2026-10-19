"""Owner-only Telegram bot: commands and the buttons under each job card.

`handle_update` gets one Update from the webhook. It answers only TELEGRAM_CHAT_ID; until that
is set, the only thing it does is reply to /start with the chat id so the owner can configure it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import tracker
from app.telegram.client import TelegramClient
from app.telegram.config import TelegramSettings
from app.telegram.render import (
    STATUS_LABELS,
    chunk,
    esc,
    fmt_time,
    job_card,
    job_keyboard,
    parse_callback,
    safe_url,
    zone,
)

logger = logging.getLogger(__name__)

COMMANDS = [
    ("top", "Best 5 open matches you haven't acted on"),
    ("today", "Jobs stored since yesterday"),
    ("applied", "Jobs you saved or applied to, with status"),
    ("stats", "Pipeline counts"),
    ("pause", "Pause the daily digest"),
    ("resume", "Resume the daily digest"),
    ("status", "/status <job id> <status>: e.g. /status 42 interviewing"),
    ("help", "What the bot can do"),
]
TOP_SIZE = 5
TOP_MAX_AGE_DAYS = 30
TODAY_LIMIT = 20
TOAST = {
    "applied": "Marked as applied",
    "saved": "Saved",
    "not_interested": "Got it: fewer jobs like this",
    "new": "Status cleared",
}
DIGEST_KEY = "telegram"


def help_text(settings: TelegramSettings) -> str:
    lines = ["<b>FirstFetch</b> sends your best new job matches once a day.", ""]
    lines += [f"/{name}: {esc(desc)}" for name, desc in COMMANDS]
    lines += [
        "",
        f"Digest: up to {settings.digest_size} new jobs scoring ≥ {settings.digest_min_score:.0f}.",
        "Buttons under a job update your tracker; 👎 and ⭐/✅ nudge similar jobs down or up.",
    ]
    return "\n".join(lines)


def is_paused(session: Session) -> bool:
    return bool((tracker.get_setting(session, DIGEST_KEY) or {}).get("paused"))


def set_paused(session: Session, paused: bool, now: datetime | None = None) -> None:
    value = dict(tracker.get_setting(session, DIGEST_KEY) or {})
    value["paused"] = paused
    tracker.put_setting(session, DIGEST_KEY, value, now)


def send_cards(
    session: Session,
    client: TelegramClient,
    chat_id: str,
    cands: list[tracker.Candidate],
    settings: TelegramSettings,
    now: datetime,
) -> int:
    """One message per job, with buttons. Marks each as notified once Telegram accepted it."""
    tz = zone(settings.timezone)
    sent = 0
    for c in cands:
        client.send_message(chat_id, job_card(c, tz), reply_markup=job_keyboard(c.job.id, c.job.url, c.status))
        tracker.mark_notified(session, c.job.id, now)
        session.commit()  # a later failure must not make this job look unsent
        sent += 1
    return sent


# --------------------------------------------------------------------------- commands


def cmd_top(session, client, chat_id, settings, now, _args) -> None:
    cands = tracker.candidates(session, since=now - timedelta(days=TOP_MAX_AGE_DAYS))[:TOP_SIZE]
    if not cands:
        client.send_message(chat_id, "No open matches you haven't acted on yet.")
        return
    client.send_message(chat_id, f"<b>Top {len(cands)} open matches</b> (not yet acted on)")
    send_cards(session, client, chat_id, cands, settings, now)


def cmd_today(session, client, chat_id, settings, now, _args) -> None:
    cands = tracker.candidates(session, since=now - timedelta(days=1))
    if not cands:
        client.send_message(chat_id, "No new open jobs since yesterday.")
        return
    strong = sum(1 for c in cands if c.points >= settings.digest_min_score)
    header = f"<b>{len(cands)} new open job{'s' if len(cands) != 1 else ''} since yesterday</b>"
    header += f" · {strong} scoring ≥ {settings.digest_min_score:.0f}"
    lines = [header]
    for c in cands[:TODAY_LIMIT]:
        url = safe_url(c.job.url)
        title = esc(c.job.title, 90)
        title = f'<a href="{esc(url)}">{title}</a>' if url else title
        company = f" · {esc(c.job.company, 50)}" if c.job.company else ""
        lines.append(f"<b>{c.points:.0f}</b> {title}{company} <code>#{c.job.id}</code>")
    if len(cands) > TODAY_LIMIT:
        lines.append(f"…and {len(cands) - TODAY_LIMIT} more on the dashboard.")
    for message in chunk(lines):
        client.send_message(chat_id, message)


def cmd_applied(session, client, chat_id, settings, now, _args) -> None:
    rows = tracker.tracked(session, tracker.POSITIVE)
    if not rows:
        client.send_message(chat_id, "Nothing tracked yet. Tap ⭐ Save or ✅ Applied under a job.")
        return
    tz = zone(settings.timezone)
    order = {s: i for i, s in enumerate(tracker.STATUSES)}
    rows.sort(key=lambda r: (order.get(r[0].status, 99), -(tracker._aware(r[0].updated_at) or now).timestamp()))
    lines = [f"<b>{len(rows)} tracked job{'s' if len(rows) != 1 else ''}</b>"]
    for app, job, _ in rows:
        when = fmt_time(app.applied_at or app.status_changed_at or app.updated_at, tz)
        url = safe_url(job.url)
        title = esc(job.title, 90)
        title = f'<a href="{esc(url)}">{title}</a>' if url else title
        company = f" · {esc(job.company, 50)}" if job.company else ""
        line = f"{STATUS_LABELS.get(app.status, esc(app.status))} {title}{company} <code>#{job.id}</code>"
        if when:
            line += f" · {when}"
        if app.notes:
            line += f"\n    <i>{esc(app.notes, 200)}</i>"
        lines.append(line)
    for message in chunk(lines):
        client.send_message(chat_id, message)


def cmd_stats(session, client, chat_id, settings, now, _args) -> None:
    s = tracker.pipeline_stats(session, min_points=settings.digest_min_score, now=now)
    elig = s["eligibility"]
    lines = [
        "<b>Pipeline</b>",
        f"Jobs stored: {s['jobs']} ({s['new_24h']} in the last 24 h)",
        f"Analyzed: {s['analyzed']} · eligible {elig.get('eligible', 0)} · unknown {elig.get('unknown', 0)}"
        f" · not open to you {elig.get('ineligible', 0)}",
        f"Open matches scoring ≥ {settings.digest_min_score:.0f}: {s['strong']}",
        f"Sent to Telegram: {s['notified']}",
    ]
    if s["statuses"]:
        lines.append("Tracker: " + " · ".join(f"{STATUS_LABELS[k]} {v}" for k, v in s["statuses"].items()))
    if s["last_run"]:
        run = s["last_run"]
        lines.append(
            f"Last run: {esc(run['type'])} {esc(run['status'])} {fmt_time(run['started_at'], zone(settings.timezone))}"
        )
    lines.append(f"Digest: {'paused' if is_paused(session) else 'on'}")
    client.send_message(chat_id, "\n".join(lines))


def cmd_pause(session, client, chat_id, settings, now, _args) -> None:
    set_paused(session, True, now)
    session.commit()
    client.send_message(chat_id, "Daily digest paused. /resume to turn it back on.")


def cmd_resume(session, client, chat_id, settings, now, _args) -> None:
    set_paused(session, False, now)
    session.commit()
    client.send_message(chat_id, "Daily digest resumed.")


def cmd_status(session, client, chat_id, settings, now, args) -> None:
    usage = "Usage: /status &lt;job id&gt; &lt;" + "|".join(tracker.STATUSES) + "&gt;"
    if len(args) < 2 or not args[0].lstrip("#").isdigit() or args[1].lower() not in tracker.STATUSES:
        client.send_message(chat_id, usage)
        return
    job_id, status = int(args[0].lstrip("#")), args[1].lower()
    try:
        tracker.set_status(session, job_id, status, now=now)
    except tracker.UnknownJob:
        client.send_message(chat_id, f"No job #{job_id}.")
        return
    session.commit()
    client.send_message(chat_id, f"Job #{job_id}: {STATUS_LABELS[status]}")


def cmd_help(session, client, chat_id, settings, now, _args) -> None:
    client.send_message(chat_id, help_text(settings))


HANDLERS = {
    "start": cmd_help,
    "help": cmd_help,
    "top": cmd_top,
    "today": cmd_today,
    "applied": cmd_applied,
    "stats": cmd_stats,
    "pause": cmd_pause,
    "resume": cmd_resume,
    "status": cmd_status,
}


# --------------------------------------------------------------------------- updates


def _command(text: str) -> tuple[str, list[str]] | None:
    """'/top@FirstFetchBot  extra' -> ('top', ['extra'])."""
    if not text.startswith("/"):
        return None
    head, *args = text.split()
    return head[1:].split("@", 1)[0].lower(), args


def handle_callback(session, client, settings, query: dict, now: datetime) -> str:
    message = query.get("message") or {}
    parsed = parse_callback(query.get("data"))
    if not parsed or parsed[1] not in tracker.STATUSES:
        client.answer_callback_query(query["id"], "Unknown button")
        return "callback:invalid"
    job_id, status = parsed
    try:
        app = tracker.set_status(session, job_id, status, now=now)
    except tracker.UnknownJob:
        client.answer_callback_query(query["id"], "This job is no longer stored")
        return "callback:unknown-job"
    session.commit()
    client.answer_callback_query(query["id"], TOAST.get(status, STATUS_LABELS[status]))
    cand = tracker.candidate_for(session, job_id)
    if cand and message.get("message_id"):
        client.edit_message_text(
            message["chat"]["id"],
            message["message_id"],
            job_card(cand, zone(settings.timezone), status=app.status, changed_at=app.status_changed_at),
            reply_markup=job_keyboard(job_id, cand.job.url, app.status),
        )
    return f"callback:{status}"


def handle_update(
    session: Session, client: TelegramClient, settings: TelegramSettings, update: dict, now: datetime | None = None
) -> str:
    """Handle one webhook Update. Returns a short label of what happened (for logs and tests)."""
    now = now or tracker.utcnow()
    query = update.get("callback_query")
    message = update.get("message") or update.get("edited_message")
    chat = ((query or {}).get("message") or {}).get("chat") if query else (message or {}).get("chat")
    if not chat or "id" not in chat:
        return "ignored:no-chat"
    chat_id = str(chat["id"])

    if not settings.chat_id:
        # Setup mode: tell the owner their chat id, do nothing else.
        if message and (_command(message.get("text") or "") or ("", []))[0] == "start":
            client.send_message(
                chat_id,
                f"Your chat id is <code>{esc(chat_id)}</code>.\n"
                "Set it as <code>TELEGRAM_CHAT_ID</code> in your deployment and redeploy; "
                "until then this bot answers nothing else.",
            )
            return "setup:chat-id"
        return "ignored:setup"

    if chat_id != str(settings.chat_id).strip():
        logger.info("Telegram update from an unknown chat ignored")
        return "ignored:not-owner"

    if query:
        return handle_callback(session, client, settings, query, now)

    parsed = _command((message or {}).get("text") or "")
    if not parsed:
        return "ignored:not-a-command"
    name, args = parsed
    handler = HANDLERS.get(name)
    if handler is None:
        client.send_message(chat_id, "Unknown command. /help lists what I can do.")
        return "command:unknown"
    handler(session, client, chat_id, settings, now, args)
    return f"command:{name}"
