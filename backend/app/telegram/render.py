"""Telegram HTML for job cards and lists.

Telegram's HTML mode needs <, > and & escaped in text and attribute values. Every field is
clipped *before* it is escaped, so a cut can never split an entity like &amp;, and a single
job card stays far below the 4096-character message limit.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app.telegram.client import MAX_CALLBACK_DATA_BYTES, MAX_MESSAGE_CHARS
from app.tracker import Candidate, match_info

STATUS_LABELS = {
    "new": "New",
    "saved": "⭐ Saved",
    "applied": "✅ Applied",
    "interviewing": "🗣 Interviewing",
    "offer": "🎉 Offer",
    "rejected": "✖️ Rejected",
    "not_interested": "👎 Not for me",
}
BUTTONS = (("applied", "✅ Applied"), ("saved", "⭐ Save"), ("not_interested", "👎 Not for me"))

# Reasons written by worker/matching.py location_eligibility(); the eligibility line already says this.
_LOCATION_REASON = re.compile(
    r"^(On-site/hybrid|Description restricts|Location not specified|Remote, but eligible"
    r"|Open to applicants|Restricted to)"
)


def esc(text: object, limit: int | None = None) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if limit is not None and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return html.escape(s, quote=True)


def zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except Exception:  # no tz database on the host: Addis Ababa has no DST, so a fixed offset is exact
        return timezone(timedelta(hours=3)) if name == "Africa/Addis_Ababa" else UTC


def fmt_time(dt: datetime | None, tz: tzinfo) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(tz).strftime("%d %b %H:%M")


def safe_url(url: str | None) -> str | None:
    """Only absolute http(s) links: Telegram rejects the whole message for a bad button URL."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    return url.strip() if parts.scheme in ("http", "https") and parts.netloc and len(url) <= 2048 else None


def top_reasons(reasons: list[str], n: int = 2) -> list[str]:
    """Risks first (they cost the most), then the rest in scoring order; location has its own line."""
    risks = [r for r in reasons if r.startswith("Risk:")]
    rest = [r for r in reasons if not r.startswith("Risk:") and not _LOCATION_REASON.match(r)]
    return [*risks, *rest][:n]


def eligibility_text(eligible: bool | None) -> str:
    return {True: "✅ Eligible", False: "⛔ Not open to your location", None: "❔ Eligibility unknown"}[eligible]


def job_card(c: Candidate, tz: tzinfo, *, status: str | None = None, changed_at: datetime | None = None) -> str:
    job, m = c.job, match_info(c.analysis)
    status = status or c.status
    lines = [f"<b>{esc(job.title, 200)}</b>"]
    where = " · ".join(esc(x, 100) for x in (job.company, job.location) if x)
    if where:
        lines.append(where)
    score = f"Score <b>{c.points:.0f}</b>"
    if c.adjustment.points:
        score += f" ({c.base:.0f} {'+' if c.adjustment.points > 0 else '−'} {abs(c.adjustment.points):.0f} feedback)"
    if c.hire_probability is not None:
        score += f" · Hire probability {c.hire_probability:.0%}"
    lines.append(f"{score} · {eligibility_text(c.eligible)}")
    for reason in top_reasons(list(m.get("reasons") or [])):
        lines.append(f"• {esc(reason, 160)}")
    for reason in c.adjustment.reasons[:2]:
        lines.append(f"↳ {esc(reason, 160)}")
    url = safe_url(job.url)
    if url:
        lines.append(f'<a href="{html.escape(url, quote=True)}">Apply</a>')
    if status != "new":
        when = fmt_time(changed_at, tz)
        lines.append(f"<i>Status: {STATUS_LABELS.get(status, esc(status))}{f' · {when}' if when else ''}</i>")
    text = "\n".join(lines)
    assert len(text) <= MAX_MESSAGE_CHARS
    return text


def callback_data(job_id: int, status: str) -> str:
    data = f"s:{job_id}:{status}"
    assert len(data.encode()) <= MAX_CALLBACK_DATA_BYTES
    return data


def parse_callback(data: str | None) -> tuple[int, str] | None:
    match = re.fullmatch(r"s:(\d{1,12}):([a-z_]{1,20})", data or "")
    return (int(match.group(1)), match.group(2)) if match else None


def cover_callback_data(job_id: int) -> str:
    data = f"c:{job_id}"
    assert len(data.encode()) <= MAX_CALLBACK_DATA_BYTES
    return data


def parse_cover_callback(data: str | None) -> int | None:
    match = re.fullmatch(r"c:(\d{1,12})", data or "")
    return int(match.group(1)) if match else None


def job_keyboard(job_id: int, url: str | None, status: str, *, cover: bool = False) -> dict:
    """Status buttons; then Open, ✍️ Cover letter (when AI is on) and Undo."""
    row = [
        {"text": f"{label} ✓" if status == value else label, "callback_data": callback_data(job_id, value)}
        for value, label in BUTTONS
    ]
    rows = [row]
    second = []
    if safe_url(url):
        second.append({"text": "🔗 Open", "url": safe_url(url)})
    if cover:
        second.append({"text": "✍️ Cover letter", "callback_data": cover_callback_data(job_id)})
    if status != "new":
        second.append({"text": "↩️ Undo", "callback_data": callback_data(job_id, "new")})
    if second:
        rows.append(second)
    return {"inline_keyboard": rows}


def chunk(blocks: Iterable[str], limit: int = MAX_MESSAGE_CHARS, sep: str = "\n") -> list[str]:
    """Pack whole blocks (e.g. one line per job) into as few messages as fit under `limit`.

    Blocks are already-escaped HTML, so they are never split; a block that is too long on its
    own (callers clip fields, so this shouldn't happen) is dropped rather than cut mid-tag.
    """
    messages: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > limit:
            continue
        candidate = f"{current}{sep}{block}" if current else block
        if len(candidate) > limit:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages
