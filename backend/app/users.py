"""Users, invites and per-user secrets.

* The owner is the user with role "owner"; their chat id is TELEGRAM_CHAT_ID (kept in sync).
* Members join with a single-use invite code made by the owner's /invite: 32 URL-safe characters
  from `secrets` (192 bits), stored as SHA-256 only, valid for 48 hours. Redemption is a single
  conditional UPDATE, so two chats racing for one code can't both win.
* Wrong codes are throttled per chat (5 per 15 minutes, then a 1-hour lockout) and globally
  (50 per hour across every chat, then redemption pauses for an hour). The per-chat key is a hash
  of the chat id, so the throttle table stores nothing that identifies anyone.
* Removing a member deletes them and everything they own (tracker, talent networks, scores, resume,
  their dashboard token and login sessions); invites they used and their AI usage rows (token counts and cost only)
  keep no link to them.
* The personal dashboard token (/dashboard) is 32 random bytes, stored as SHA-256 only. Issuing a new
  one or revoking it also ends the user's dashboard login sessions (app/web_sessions.py).

Nothing here logs chat ids, names, codes or tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import (
    Application,
    AuthThrottle,
    Invite,
    LlmUsage,
    LoginLink,
    TalentNetworkStatus,
    User,
    UserJobScore,
    WebSession,
)

if TYPE_CHECKING:
    from worker.profile import CandidateProfile

INVITE_TTL = timedelta(hours=48)
INVITE_BYTES = 24  # 192 bits -> 32 characters of A-Z a-z 0-9 _ -, fits Telegram's /start payload (64)
DASHBOARD_TOKEN_BYTES = 32
CHAT_MAX_FAILURES = 5
CHAT_WINDOW = timedelta(minutes=15)
CHAT_LOCKOUT = timedelta(hours=1)
GLOBAL_MAX_FAILURES = 50
GLOBAL_WINDOW = timedelta(hours=1)
GLOBAL_LOCKOUT = timedelta(hours=1)
MAX_MEMBERS = 20  # an invite-only bot for friends, not a service
CODE_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _worker_on_path() -> None:
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)


# --------------------------------------------------------------------------- users


def get_owner(session: Session) -> User | None:
    return session.execute(select(User).where(User.role == "owner").order_by(User.id).limit(1)).scalar_one_or_none()


def ensure_owner(session: Session, chat_id: str | None, now: datetime | None = None) -> User:
    """The owner row, created if missing; its chat id follows TELEGRAM_CHAT_ID."""
    owner = get_owner(session)
    chat_id = (chat_id or "").strip() or None
    if owner is None:
        owner = User(role="owner", display_name="Owner", telegram_chat_id=None, created_at=now or utcnow())
        session.add(owner)
        session.flush()
    if chat_id and owner.telegram_chat_id != chat_id:
        clash = by_chat(session, chat_id)
        if clash is not None and clash.id != owner.id:
            # The owner's own chat can't also be a member: the member row goes.
            delete_user_data(session, clash.id)
        owner.telegram_chat_id = chat_id
        session.flush()
    return owner


def by_chat(session: Session, chat_id: str) -> User | None:
    return session.execute(select(User).where(User.telegram_chat_id == str(chat_id))).scalar_one_or_none()


def list_users(session: Session) -> list[User]:
    return list(session.execute(select(User).order_by(User.id)).scalars())


def digest_users(session: Session) -> list[User]:
    """Users who get the daily digest: a chat id and not paused. Owner first."""
    return [u for u in list_users(session) if u.telegram_chat_id and not u.paused]


def label(user: User) -> str:
    return user.display_name or f"User {user.id}"


def delete_user_data(session: Session, user_id: int) -> None:
    """Delete a user and everything they own. Explicit deletes, so it doesn't depend on FK cascades."""
    session.execute(delete(Application).where(Application.user_id == user_id))
    session.execute(delete(TalentNetworkStatus).where(TalentNetworkStatus.user_id == user_id))
    session.execute(delete(UserJobScore).where(UserJobScore.user_id == user_id))
    session.execute(delete(WebSession).where(WebSession.user_id == user_id))  # logged out of the dashboard
    session.execute(delete(LoginLink).where(LoginLink.user_id == user_id))
    session.execute(update(Invite).where(Invite.used_by == user_id).values(used_by=None))
    # Spend already made still counts toward the month: the ledger rows (tokens, cost) lose the link.
    session.execute(update(LlmUsage).where(LlmUsage.user_id == user_id).values(user_id=None))
    session.execute(delete(Invite).where(Invite.created_by == user_id))
    session.execute(delete(User).where(User.id == user_id))
    session.flush()


class CannotRemove(ValueError):
    pass


def remove_member(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise LookupError(user_id)
    if user.role == "owner":
        raise CannotRemove("The owner can't be removed")
    delete_user_data(session, user_id)
    return user


# --------------------------------------------------------------------------- profiles


def profile_model(user: User) -> CandidateProfile:
    _worker_on_path()
    from worker.user_scoring import profile_for

    return profile_for(user)


def save_profile(session: Session, user: User, data: dict[str, Any], now: datetime | None = None) -> CandidateProfile:
    """Validate with CandidateProfile and store. Raises pydantic.ValidationError on bad input."""
    _worker_on_path()
    from worker.profile import CandidateProfile

    profile = CandidateProfile.model_validate(data)
    user.profile = profile.model_dump(mode="json")
    user.profile_updated_at = now or utcnow()
    session.flush()
    return profile


SCORE_WINDOW = timedelta(days=30)  # the longest window anything reads (/top)


def ensure_scores(session: Session, user: User, now: datetime | None = None) -> int:
    """Score recent jobs this user has no row for yet (new member, or rows from before 004). Cheap when none."""
    _worker_on_path()
    from worker.user_scoring import score_missing

    now = now or utcnow()
    return score_missing(session, user, since=now - SCORE_WINDOW, now=now)


# --------------------------------------------------------------------------- invites


def create_invite(session: Session, owner: User, now: datetime | None = None) -> str:
    """A new single-use code. Only its hash is stored; the code is returned once."""
    if owner.role != "owner":
        raise PermissionError("only the owner can invite")
    now = now or utcnow()
    code = secrets.token_urlsafe(INVITE_BYTES)
    session.add(Invite(code_hash=hash_secret(code), created_by=owner.id, created_at=now, expires_at=now + INVITE_TTL))
    session.flush()
    return code


def _throttle_row(session: Session, key: str, now: datetime) -> AuthThrottle:
    row = session.get(AuthThrottle, key)
    if row is None:
        row = AuthThrottle(key=key, failures=0, window_started_at=now)
        session.add(row)
    return row


def _chat_key(chat_id: str) -> str:
    return "chat:" + hash_secret(f"firstfetch-throttle:{chat_id}")


def is_locked(session: Session, chat_id: str, now: datetime | None = None) -> bool:
    now = now or utcnow()
    for key in (_chat_key(chat_id), "global"):
        row = session.get(AuthThrottle, key)
        if row is not None and row.locked_until is not None and _aware(row.locked_until) > now:
            return True
    return False


def _record_failure(session: Session, chat_id: str, now: datetime) -> None:
    for key, limit, window, lockout in (
        (_chat_key(chat_id), CHAT_MAX_FAILURES, CHAT_WINDOW, CHAT_LOCKOUT),
        ("global", GLOBAL_MAX_FAILURES, GLOBAL_WINDOW, GLOBAL_LOCKOUT),
    ):
        row = _throttle_row(session, key, now)
        if now - _aware(row.window_started_at) > window:
            row.failures, row.window_started_at = 0, now
        row.failures += 1
        if row.failures >= limit:
            row.locked_until = now + lockout
            row.failures, row.window_started_at = 0, now
    session.flush()


@dataclass
class Redemption:
    user: User | None
    reason: str  # "joined" | "locked" | "invalid" | "full"


def redeem_invite(
    session: Session, chat_id: str, code: str, display_name: str | None, now: datetime | None = None
) -> Redemption:
    """`/start <code>` from a chat that isn't a user yet."""
    now = now or utcnow()
    chat_id = str(chat_id)
    if is_locked(session, chat_id, now):
        return Redemption(None, "locked")
    if not CODE_RE.fullmatch(code or ""):
        _record_failure(session, chat_id, now)
        return Redemption(None, "invalid")
    digest = hash_secret(code)
    invite = session.execute(select(Invite).where(Invite.code_hash == digest)).scalar_one_or_none()
    usable = (
        invite is not None
        and hmac.compare_digest(invite.code_hash, digest)
        and invite.used_at is None
        and _aware(invite.expires_at) > now
    )
    if not usable:
        _record_failure(session, chat_id, now)
        return Redemption(None, "invalid")
    members = sum(1 for u in list_users(session) if u.role == "member")
    if members >= MAX_MEMBERS:
        return Redemption(None, "full")
    owner = get_owner(session)
    user = User(
        telegram_chat_id=chat_id,
        display_name=(re.sub(r"\s+", " ", display_name or "").strip()[:64] or None),
        role="member",
        created_at=now,
        last_seen=now,
    )
    session.add(user)
    session.flush()
    # Claim the code atomically: a concurrent redemption of the same code updates nothing.
    claimed = session.execute(
        update(Invite)
        .where(Invite.id == invite.id, Invite.used_at.is_(None))
        .values(used_at=now, used_by=user.id)
        .execution_options(synchronize_session=False)
    ).rowcount
    if claimed != 1:
        session.execute(delete(User).where(User.id == user.id))
        session.flush()
        _record_failure(session, chat_id, now)
        return Redemption(None, "invalid")
    # Start from a copy of the owner's profile; from here on it's the member's to edit.
    if owner is not None:
        user.profile = profile_model(owner).model_dump(mode="json")
        user.profile_updated_at = now
    session.flush()
    return Redemption(user, "joined")


# --------------------------------------------------------------------------- dashboard tokens


def issue_dashboard_token(session: Session, user: User) -> str:
    """A new personal dashboard token (the previous one stops working). Shown once.

    Rotating the link also ends every dashboard login session of this user: /dashboard is the
    "log out everywhere" lever if a link or a browser was exposed.
    """
    from app.web_sessions import revoke_user_sessions

    token = secrets.token_urlsafe(DASHBOARD_TOKEN_BYTES)
    user.dashboard_token_hash = hash_secret(token)
    revoke_user_sessions(session, user.id)
    session.flush()
    return token


def revoke_dashboard_token(session: Session, user: User) -> None:
    """Turn the link off; like a rotation, this also ends the user's dashboard login sessions."""
    from app.web_sessions import revoke_user_sessions

    user.dashboard_token_hash = None
    revoke_user_sessions(session, user.id)
    session.flush()


def user_by_dashboard_token(session: Session, token: str | None) -> User | None:
    if not token or len(token) > 128:
        return None
    digest = hash_secret(token)
    user = session.execute(select(User).where(User.dashboard_token_hash == digest)).scalar_one_or_none()
    if user is None or not hmac.compare_digest(user.dashboard_token_hash or "", digest):
        return None
    return user
