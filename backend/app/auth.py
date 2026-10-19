"""Who is asking: the read side of the privacy lock.

Job data and the public (owner-profile) score are open to everyone, for the portfolio demo.
Everything a person *did* with a job (tracker statuses, notes, feedback, talent-network progress)
is private: it is only returned to a caller that proves who they are.

* ``X-Admin-Token: <ADMIN_TOKEN>`` is the owner.
* ``X-User-Token: <token>`` is the user whose personal dashboard link (/dashboard in the bot) it
  is. Only its SHA-256 is stored; it opens that user's own tracker and nothing else.

Tokens are compared in constant time. With no ADMIN_TOKEN configured nobody can read private
data over HTTP (fail closed); the Telegram bot still works, it has its own secret.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.db_sync import get_sync_db

ADMIN_HEADER = "x-admin-token"
USER_HEADER = "x-user-token"


@dataclass(frozen=True)
class Viewer:
    """An authenticated caller: whose tracker the request reads and writes."""

    role: str  # "owner" | "member"
    user_id: int

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


def admin_token_ok(supplied: str | None) -> bool:
    expected = settings.admin_token
    return bool(expected) and hmac.compare_digest((supplied or "").encode(), expected.encode())


def viewer_from_request(request: Request, session: Session) -> Viewer | None:
    from app import users
    from app.telegram.config import telegram_settings

    if admin_token_ok(request.headers.get(ADMIN_HEADER)):
        owner = users.ensure_owner(session, telegram_settings.chat_id)
        return Viewer(role="owner", user_id=owner.id)
    token = request.headers.get(USER_HEADER)
    if token:
        user = users.user_by_dashboard_token(session, token)
        if user is not None:
            return Viewer(role=user.role, user_id=user.id)
    return None


def optional_viewer(request: Request, session: Session = Depends(get_sync_db)) -> Viewer | None:
    """FastAPI dependency: the caller, or None for the public. No database access without a token."""
    if not request.headers.get(ADMIN_HEADER) and not request.headers.get(USER_HEADER):
        return None
    return viewer_from_request(request, session)


def require_viewer(viewer: Viewer | None = Depends(optional_viewer)) -> Viewer:
    """FastAPI dependency for private data: 401 without a valid token."""
    if viewer is None:
        raise HTTPException(
            401, "Private data: send X-Admin-Token or your X-User-Token", headers={"WWW-Authenticate": "Token"}
        )
    return viewer
