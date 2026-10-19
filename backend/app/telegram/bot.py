"""Invite-only Telegram bot: commands and the buttons under each job card.

`handle_update` gets one Update from the webhook.

* Until TELEGRAM_CHAT_ID is set, the only thing it does is reply to /start with the chat id.
* The owner (TELEGRAM_CHAT_ID) and members who joined with an invite code get every command,
  each on their own profile, tracker and feedback. /invite, /users and /remove are owner-only.
* Any other chat is ignored without a reply, except `/start <code>` (an invite link) in a
  private chat, which is checked against the stored code hashes and throttled (app/users.py).

Nothing here logs chat ids, names, codes or tokens.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import tracker, users
from app.models import User
from app.telegram import profile_cmds
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
    ("pause", "Pause your daily digest"),
    ("resume", "Resume your daily digest"),
    ("status", "/status <job id> <status>: e.g. /status 42 interviewing"),
    ("profile", "Your profile: titles, skills, country, salary..."),
    ("skills", "/skills add|remove <skill> [weight 1-3]"),
    ("titles", "/titles add|remove <title>"),
    ("salary", "/salary <minimum yearly USD>"),
    ("country", "/country <name> [ISO code]"),
    ("seniority", "/seniority <levels>, e.g. mid,senior"),
    ("employment", "/employment <types>, e.g. full_time,contract"),
    ("dashboard", "A private link to your statuses on the dashboard"),
    ("help", "What the bot can do"),
]
OWNER_COMMANDS = [
    ("invite", "Create a single-use invite link (48 h)"),
    ("users", "List users"),
    ("remove", "/remove <user id>: remove a member and delete their data"),
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


@dataclass
class Ctx:
    session: Session
    client: TelegramClient
    settings: TelegramSettings
    user: User
    chat_id: str
    now: datetime

    def send(self, text: str, **kw) -> dict:
        return self.client.send_message(self.chat_id, text, **kw)


def digest_size(user: User, settings: TelegramSettings) -> int:
    return user.digest_size if user.digest_size is not None else settings.digest_size


def digest_min_score(user: User, settings: TelegramSettings) -> float:
    return user.digest_min_score if user.digest_min_score is not None else settings.digest_min_score


def help_text(settings: TelegramSettings, user: User | None = None) -> str:
    lines = ["<b>FirstFetch</b> sends your best new job matches once a day, scored against your profile.", ""]
    lines += [f"/{name}: {esc(desc)}" for name, desc in COMMANDS]
    if user is not None and user.role == "owner":
        lines += ["", "<b>Owner</b>"] + [f"/{name}: {esc(desc)}" for name, desc in OWNER_COMMANDS]
    size = digest_size(user, settings) if user else settings.digest_size
    floor = digest_min_score(user, settings) if user else settings.digest_min_score
    lines += [
        "",
        f"Digest: up to {size} new jobs scoring ≥ {floor:.0f}.",
        "Buttons under a job update your tracker; 👎 and ⭐/✅ nudge similar jobs down or up.",
    ]
    return "\n".join(lines)


def is_paused(user: User) -> bool:
    return bool(user.paused)


def set_paused(session: Session, user: User, paused: bool) -> None:
    user.paused = paused
    session.flush()


def send_cards(
    session: Session,
    client: TelegramClient,
    chat_id: str,
    user: User,
    cands: list[tracker.Candidate],
    settings: TelegramSettings,
    now: datetime,
) -> int:
    """One message per job, with buttons. Marks each as notified (for this user) once Telegram accepted it."""
    tz = zone(settings.timezone)
    sent = 0
    for c in cands:
        client.send_message(chat_id, job_card(c, tz), reply_markup=job_keyboard(c.job.id, c.job.url, c.status))
        tracker.mark_notified(session, user.id, c.job.id, now)
        session.commit()  # a later failure must not make this job look unsent
        sent += 1
    return sent


# --------------------------------------------------------------------------- commands


def cmd_top(ctx: Ctx, _args) -> None:
    users.ensure_scores(ctx.session, ctx.user, ctx.now)
    cands = tracker.candidates(ctx.session, ctx.user.id, since=ctx.now - timedelta(days=TOP_MAX_AGE_DAYS))[:TOP_SIZE]
    if not cands:
        ctx.send("No open matches you haven't acted on yet.")
        return
    ctx.send(f"<b>Top {len(cands)} open matches</b> (not yet acted on)")
    send_cards(ctx.session, ctx.client, ctx.chat_id, ctx.user, cands, ctx.settings, ctx.now)


def cmd_today(ctx: Ctx, _args) -> None:
    users.ensure_scores(ctx.session, ctx.user, ctx.now)
    cands = tracker.candidates(ctx.session, ctx.user.id, since=ctx.now - timedelta(days=1))
    if not cands:
        ctx.send("No new open jobs since yesterday.")
        return
    floor = digest_min_score(ctx.user, ctx.settings)
    strong = sum(1 for c in cands if c.points >= floor)
    header = f"<b>{len(cands)} new open job{'s' if len(cands) != 1 else ''} since yesterday</b>"
    header += f" · {strong} scoring ≥ {floor:.0f}"
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
        ctx.send(message)


def cmd_applied(ctx: Ctx, _args) -> None:
    rows = tracker.tracked(ctx.session, ctx.user.id, tracker.POSITIVE)
    if not rows:
        ctx.send("Nothing tracked yet. Tap ⭐ Save or ✅ Applied under a job.")
        return
    tz = zone(ctx.settings.timezone)
    order = {s: i for i, s in enumerate(tracker.STATUSES)}
    rows.sort(key=lambda r: (order.get(r[0].status, 99), -(tracker._aware(r[0].updated_at) or ctx.now).timestamp()))
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
        ctx.send(message)


def cmd_stats(ctx: Ctx, _args) -> None:
    users.ensure_scores(ctx.session, ctx.user, ctx.now)
    floor = digest_min_score(ctx.user, ctx.settings)
    s = tracker.pipeline_stats(ctx.session, ctx.user.id, min_points=floor, now=ctx.now)
    elig = s["eligibility"]
    lines = [
        "<b>Pipeline</b>",
        f"Jobs stored: {s['jobs']} ({s['new_24h']} in the last 24 h)",
        f"Scored for you: {s['analyzed']} · eligible {elig.get('eligible', 0)} · unknown {elig.get('unknown', 0)}"
        f" · not open to you {elig.get('ineligible', 0)}",
        f"Open matches scoring ≥ {floor:.0f}: {s['strong']}",
        f"Sent to you: {s['notified']}",
    ]
    if s["statuses"]:
        lines.append("Tracker: " + " · ".join(f"{STATUS_LABELS[k]} {v}" for k, v in s["statuses"].items()))
    if s["last_run"]:
        run = s["last_run"]
        lines.append(
            f"Last run: {esc(run['type'])} {esc(run['status'])} "
            f"{fmt_time(run['started_at'], zone(ctx.settings.timezone))}"
        )
    lines.append(f"Digest: {'paused' if is_paused(ctx.user) else 'on'}")
    ctx.send("\n".join(lines))


def cmd_pause(ctx: Ctx, _args) -> None:
    set_paused(ctx.session, ctx.user, True)
    ctx.session.commit()
    ctx.send("Daily digest paused. /resume to turn it back on.")


def cmd_resume(ctx: Ctx, _args) -> None:
    set_paused(ctx.session, ctx.user, False)
    ctx.session.commit()
    ctx.send("Daily digest resumed.")


def cmd_status(ctx: Ctx, args) -> None:
    usage = "Usage: /status &lt;job id&gt; &lt;" + "|".join(tracker.STATUSES) + "&gt;"
    if len(args) < 2 or not args[0].lstrip("#").isdigit() or args[1].lower() not in tracker.STATUSES:
        ctx.send(usage)
        return
    job_id, status = int(args[0].lstrip("#")), args[1].lower()
    try:
        tracker.set_status(ctx.session, ctx.user.id, job_id, status, now=ctx.now)
    except tracker.UnknownJob:
        ctx.send(f"No job #{job_id}.")
        return
    ctx.session.commit()
    ctx.send(f"Job #{job_id}: {STATUS_LABELS[status]}")


def cmd_help(ctx: Ctx, _args) -> None:
    ctx.send(help_text(ctx.settings, ctx.user))


def cmd_profile(ctx: Ctx, _args) -> None:
    ctx.send(profile_cmds.summary(ctx.user))


def _edit(name: str):
    def handler(ctx: Ctx, args) -> None:
        ctx.send(profile_cmds.apply_edit(ctx.session, ctx.user, name, args, ctx.now))

    return handler


def cmd_dashboard(ctx: Ctx, args) -> None:
    if args and args[0].lower() == "revoke":
        users.revoke_dashboard_token(ctx.session, ctx.user)
        ctx.session.commit()
        ctx.send("Your dashboard link no longer works. /dashboard makes a new one.")
        return
    base = safe_url(ctx.settings.dashboard_url)
    if not base:
        ctx.send("The dashboard URL isn't configured (TELEGRAM_DASHBOARD_URL); ask the owner.")
        return
    token = users.issue_dashboard_token(ctx.session, ctx.user)
    ctx.session.commit()
    link = f"{base.rstrip('/')}/#token={token}"
    ctx.send(
        "<b>Your private dashboard link</b> (shown once; anyone with it sees your statuses):\n"
        f'<a href="{esc(link)}">Open your dashboard</a>\n<code>{esc(link)}</code>\n\n'
        "A new /dashboard replaces it; /dashboard revoke turns it off."
    )


# --------------------------------------------------------------------------- owner commands


def cmd_invite(ctx: Ctx, _args) -> None:
    code = users.create_invite(ctx.session, ctx.user, ctx.now)
    ctx.session.commit()
    expires = fmt_time(ctx.now + users.INVITE_TTL, zone(ctx.settings.timezone))
    try:
        username = (ctx.client.get_me() or {}).get("username")
    except Exception:  # the link is a convenience; the code works on its own
        username = None
    lines = [f"<b>Invite</b>: single use, expires {expires}.", "Send your friend this link:"]
    if username:
        lines.append(esc(f"https://t.me/{username}?start={code}"))
    lines.append(f"or have them send the bot: <code>/start {esc(code)}</code>")
    ctx.send("\n".join(lines))


def cmd_users(ctx: Ctx, _args) -> None:
    tz = zone(ctx.settings.timezone)
    lines = ["<b>Users</b>"]
    for u in users.list_users(ctx.session):
        seen = fmt_time(u.last_seen, tz) or "never"
        flags = " · paused" if u.paused else ""
        flags += " · no chat yet" if not u.telegram_chat_id else ""
        lines.append(f"<code>#{u.id}</code> {esc(users.label(u), 64)} · {u.role} · last seen {seen}{flags}")
    for message in chunk(lines):
        ctx.send(message)


def cmd_remove(ctx: Ctx, args) -> None:
    if len(args) != 1 or not args[0].lstrip("#").isdigit():
        ctx.send("Usage: /remove &lt;user id&gt; (see /users)")
        return
    user_id = int(args[0].lstrip("#"))
    try:
        removed = users.remove_member(ctx.session, user_id)
    except LookupError:
        ctx.send(f"No user #{user_id}.")
        return
    except users.CannotRemove as exc:
        ctx.send(esc(str(exc)))
        return
    ctx.session.commit()
    ctx.send(
        f"Removed {esc(users.label(removed), 64)} (#{user_id}): their profile, tracker, feedback, talent-network "
        "statuses, scores and dashboard link are deleted. Their chat is ignored from now on."
    )


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
    "profile": cmd_profile,
    "dashboard": cmd_dashboard,
    **{name: _edit(name) for name in profile_cmds.EDITS},
}
OWNER_HANDLERS = {"invite": cmd_invite, "users": cmd_users, "remove": cmd_remove}


# --------------------------------------------------------------------------- updates


def _command(text: str) -> tuple[str, list[str]] | None:
    """'/top@FirstFetchBot  extra' -> ('top', ['extra'])."""
    if not text.startswith("/"):
        return None
    head, *args = text.split()
    return head[1:].split("@", 1)[0].lower(), args


def handle_callback(ctx: Ctx, query: dict) -> str:
    message = query.get("message") or {}
    parsed = parse_callback(query.get("data"))
    if not parsed or parsed[1] not in tracker.STATUSES:
        ctx.client.answer_callback_query(query["id"], "Unknown button")
        return "callback:invalid"
    job_id, status = parsed
    try:
        app = tracker.set_status(ctx.session, ctx.user.id, job_id, status, now=ctx.now)
    except tracker.UnknownJob:
        ctx.client.answer_callback_query(query["id"], "This job is no longer stored")
        return "callback:unknown-job"
    ctx.session.commit()
    ctx.client.answer_callback_query(query["id"], TOAST.get(status, STATUS_LABELS[status]))
    cand = tracker.candidate_for(ctx.session, ctx.user.id, job_id)
    if cand and message.get("message_id"):
        ctx.client.edit_message_text(
            message["chat"]["id"],
            message["message_id"],
            job_card(cand, zone(ctx.settings.timezone), status=app.status, changed_at=app.status_changed_at),
            reply_markup=job_keyboard(job_id, cand.job.url, app.status),
        )
    return f"callback:{status}"


def handle_stranger(
    session: Session, client: TelegramClient, settings: TelegramSettings, chat_id: str, message: dict, now: datetime
) -> str:
    """A chat that isn't a user. Only `/start <code>` gets any answer at all."""
    parsed = _command((message or {}).get("text") or "")
    if not parsed or parsed[0] != "start" or len(parsed[1]) != 1:
        return "ignored:unknown-chat"
    sender = (message or {}).get("from") or {}
    result = users.redeem_invite(session, chat_id, parsed[1][0], sender.get("first_name"), now)
    session.commit()
    if result.reason == "locked":
        return "invite:locked"  # no reply: don't help a brute-force
    if result.reason == "full":
        client.send_message(chat_id, "This bot has no free places right now.")
        return "invite:full"
    if result.user is None:
        client.send_message(chat_id, "This invite code is invalid, already used or expired.")
        return "invite:invalid"
    user = result.user
    users.ensure_scores(session, user, now)
    session.commit()
    client.send_message(
        chat_id,
        "Welcome to <b>FirstFetch</b>. Your profile starts as a copy of the owner's; "
        "make it yours with /profile and the commands below.\n\n" + help_text(settings, user),
    )
    owner = users.get_owner(session)
    if owner is not None and owner.telegram_chat_id:
        try:
            client.send_message(owner.telegram_chat_id, f"{esc(users.label(user), 64)} joined (user #{user.id}).")
        except Exception:
            logger.warning("Could not tell the owner about a new member")
    return "invite:joined"


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

    users.ensure_owner(session, settings.chat_id, now)
    user = users.by_chat(session, chat_id)
    if user is None:
        session.commit()
        if query or chat.get("type", "private") != "private":
            return "ignored:unknown-chat"
        return handle_stranger(session, client, settings, chat_id, message or {}, now)

    user.last_seen = now
    ctx = Ctx(session, client, settings, user, chat_id, now)
    if query:
        return handle_callback(ctx, query)

    parsed = _command((message or {}).get("text") or "")
    if not parsed:
        session.commit()
        return "ignored:not-a-command"
    name, args = parsed
    handler = HANDLERS.get(name) or (OWNER_HANDLERS.get(name) if user.role == "owner" else None)
    if handler is None:
        ctx.send("Unknown command. /help lists what I can do.")
        session.commit()
        return "command:unknown"
    handler(ctx, args)
    session.commit()
    return f"command:{name}"
