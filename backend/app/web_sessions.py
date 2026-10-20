"""Log in with Telegram: widget signature check and dashboard sessions.

**The widget.** Telegram's Login Widget (https://core.telegram.org/widgets/login, the classic widget)
hands the page ``id, first_name, last_name, username, photo_url, auth_date, hash``. The data is genuine
when ``hash`` equals ``hex(HMAC_SHA256(data_check_string, SHA256(bot_token)))``, where the data-check
string is every received field except ``hash``, as ``key=value``, sorted by key and joined with ``\\n``.
The comparison is constant-time, and ``auth_date`` older than ``LOGIN_MAX_AGE`` (or in the future) is
refused, so a captured payload can't be replayed later.

**Who may log in.** ``id`` is the Telegram user id. In a private chat with a bot, the chat id *is* the
user id (Bot API: "for private chats, chat id equals user id"), and members only ever join from a private
chat (app/telegram/bot.py ignores strangers elsewhere), so a member is matched by
``users.telegram_chat_id == str(id)``. User ids are positive; a negative id (a group, which only the
owner's TELEGRAM_CHAT_ID could be) never matches, so an owner whose chat is a group can't use web
login (their #token link and ADMIN_TOKEN still work). Anyone else gets the same 403, whatever the reason.

**Sessions.** The cookie value is 32 random bytes (256 bits) from ``secrets``; the database keeps its
SHA-256 only. Sessions last ``SESSION_TTL`` from the last use (sliding), refreshed at most once per
``REFRESH_EVERY`` so a busy page doesn't write on every request. Logout deletes the row; removing a
member, a new /dashboard link and /dashboard revoke delete all of that user's sessions.

Nothing here logs ids, names, hashes or session ids.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import User, WebSession

COOKIE_NAME = "ff_session"
# Cookie-authenticated writes must carry this header (CSRF, see app/auth.py).
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "firstfetch"
SESSION_BYTES = 32  # 256 bits
SESSION_TTL = timedelta(days=30)
REFRESH_EVERY = timedelta(hours=1)
LOGIN_MAX_AGE = timedelta(minutes=10)
LOGIN_CLOCK_SKEW = timedelta(minutes=1)  # auth_date this far in the future is still accepted
MAX_FIELDS = 16
MAX_VALUE_LEN = 512
_HEX = frozenset("0123456789abcdef")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# --------------------------------------------------------------------------- widget signature


class InvalidLogin(ValueError):
    """The payload isn't a genuine, fresh Telegram login. The message is for tests, not for clients."""


def data_check_string(fields: dict[str, Any]) -> str:
    return "\n".join(f"{key}={fields[key]}" for key in sorted(fields) if key != "hash")


def sign(fields: dict[str, Any], bot_token: str) -> str:
    """The hash Telegram computes for these fields (also used by the tests to make known vectors)."""
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    return hmac.new(secret_key, data_check_string(fields).encode(), hashlib.sha256).hexdigest()


def verify_login(payload: dict[str, Any], bot_token: str, now: datetime | None = None) -> int:
    """Check a Login Widget payload; returns the Telegram user id. Raises InvalidLogin."""
    if not bot_token:
        raise InvalidLogin("no bot token")
    if not isinstance(payload, dict) or not 0 < len(payload) <= MAX_FIELDS:
        raise InvalidLogin("shape")
    fields: dict[str, str] = {}
    for key, value in payload.items():
        # Telegram sends strings and integers; anything else (bool, null, objects) isn't from the widget.
        if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, str | int):
            raise InvalidLogin("field type")
        text = str(value)
        if len(key) > 64 or len(text) > MAX_VALUE_LEN or "\n" in key or "\n" in text or "=" in key:
            raise InvalidLogin("field size")
        fields[key] = text
    received = fields.get("hash", "")
    if len(received) != 64 or not set(received) <= _HEX:
        raise InvalidLogin("missing hash")
    if not hmac.compare_digest(sign(fields, bot_token), received):
        raise InvalidLogin("bad signature")
    try:
        auth_date = datetime.fromtimestamp(int(fields.get("auth_date", "")), UTC)
        user_id = int(fields.get("id", ""))
    except (ValueError, OverflowError, OSError):
        raise InvalidLogin("auth_date or id") from None
    now = now or utcnow()
    if auth_date < now - LOGIN_MAX_AGE or auth_date > now + LOGIN_CLOCK_SKEW:
        raise InvalidLogin("stale")
    if user_id <= 0:
        raise InvalidLogin("not a user id")
    return user_id


def user_for_telegram_id(session: Session, telegram_id: int) -> User | None:
    """The invited user whose private chat is this Telegram account, or None."""
    from app import users

    if telegram_id <= 0:
        return None
    return users.by_chat(session, str(telegram_id))


# --------------------------------------------------------------------------- sessions


def create_session(session: Session, user: User, user_agent: str | None = None, now: datetime | None = None) -> str:
    """A new session for this user. Returns the cookie value (shown once; only its hash is stored)."""
    now = now or utcnow()
    token = secrets.token_urlsafe(SESSION_BYTES)
    session.add(
        WebSession(
            token_hash=_sha256(token),
            user_id=user.id,
            created_at=now,
            last_seen=now,
            expires_at=now + SESSION_TTL,
            user_agent_hash=_sha256(user_agent) if user_agent else None,
        )
    )
    session.flush()
    return token


@dataclass
class Resolved:
    user: User
    refreshed: bool  # the expiry moved; the cookie should be sent again with the new Max-Age


def resolve_session(session: Session, token: str | None, now: datetime | None = None) -> Resolved | None:
    """The user a session cookie belongs to, sliding its expiry; None if unknown or expired."""
    if not token or len(token) > 128:
        return None
    now = now or utcnow()
    digest = _sha256(token)
    row = session.execute(select(WebSession).where(WebSession.token_hash == digest)).scalar_one_or_none()
    if row is None or not hmac.compare_digest(row.token_hash, digest):
        return None
    if _aware(row.expires_at) <= now:
        session.delete(row)
        session.flush()
        return None
    user = session.get(User, row.user_id)
    if user is None:
        return None
    refreshed = now - _aware(row.last_seen) >= REFRESH_EVERY
    if refreshed:
        row.last_seen = now
        row.expires_at = now + SESSION_TTL
        session.flush()
    return Resolved(user, refreshed)


def delete_session(session: Session, token: str | None) -> bool:
    if not token or len(token) > 128:
        return False
    deleted = session.execute(delete(WebSession).where(WebSession.token_hash == _sha256(token))).rowcount
    session.flush()
    return bool(deleted)


def revoke_user_sessions(session: Session, user_id: int) -> int:
    """Log this user out everywhere."""
    deleted = session.execute(delete(WebSession).where(WebSession.user_id == user_id)).rowcount
    session.flush()
    return deleted or 0


def purge_expired(session: Session, now: datetime | None = None) -> int:
    deleted = session.execute(delete(WebSession).where(WebSession.expires_at <= (now or utcnow()))).rowcount
    session.flush()
    return deleted or 0
