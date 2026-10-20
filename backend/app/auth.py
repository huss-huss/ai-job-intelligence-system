"""Who is asking: the read side of the privacy lock.

Job data and the public (owner-profile) score are open to everyone, for the portfolio demo.
Everything a person *did* with a job (tracker statuses, notes, feedback, talent-network progress)
is private: it is only returned to a caller that proves who they are.

* ``X-Admin-Token: <ADMIN_TOKEN>`` is the owner.
* ``X-User-Token: <token>`` is the user whose personal dashboard link (/dashboard in the bot) it
  is. Only its SHA-256 is stored; it opens that user's own tracker and nothing else.
* The ``ff_session`` cookie is a "Log in with Telegram" session (app/web_sessions.py): the same
  rights as that user's X-User-Token. Headers win over the cookie when both are sent.

CSRF: a cookie is sent by the browser on its own, so every cookie-authenticated request other than
GET/HEAD/OPTIONS must also carry ``X-Requested-With: firstfetch``. A page on another origin can't add a
custom header without a CORS preflight, which only the dashboard's origins pass (main.py), and an HTML
form can't add one at all. SameSite=Lax on the cookie is the second layer. The header tokens need no such
check: a browser never attaches them by itself.

Tokens are compared in constant time. With no ADMIN_TOKEN configured nobody can read private
data over HTTP (fail closed); the Telegram bot still works, it has its own secret.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.db_sync import get_sync_db

ADMIN_HEADER = "x-admin-token"
USER_HEADER = "x-user-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class Viewer:
    """An authenticated caller: whose tracker the request reads and writes."""

    role: str  # "owner" | "member"
    user_id: int
    via: str = "token"  # "admin" | "token" | "session"

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


def admin_token_ok(supplied: str | None) -> bool:
    expected = settings.admin_token
    return bool(expected) and hmac.compare_digest((supplied or "").encode(), expected.encode())


def csrf_ok(request: Request) -> bool:
    """A cookie-authenticated request may change something (see the module docstring)."""
    from app.web_sessions import CSRF_HEADER, CSRF_VALUE

    if request.method in SAFE_METHODS:
        return True
    return hmac.compare_digest((request.headers.get(CSRF_HEADER) or "").encode(), CSRF_VALUE.encode())


def set_session_cookie(response: Response, token: str) -> None:
    from app.web_sessions import COOKIE_NAME, SESSION_TTL

    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    from app.web_sessions import COOKIE_NAME

    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")


def viewer_from_request(request: Request, session: Session, response: Response | None = None) -> Viewer | None:
    from app import users, web_sessions
    from app.telegram.config import telegram_settings

    if admin_token_ok(request.headers.get(ADMIN_HEADER)):
        owner = users.ensure_owner(session, telegram_settings.chat_id)
        return Viewer(role="owner", user_id=owner.id, via="admin")
    token = request.headers.get(USER_HEADER)
    if token:
        user = users.user_by_dashboard_token(session, token)
        return Viewer(role=user.role, user_id=user.id) if user is not None else None
    if request.headers.get(ADMIN_HEADER):
        return None  # a wrong admin token is the public, not a fallback to the cookie
    cookie = request.cookies.get(web_sessions.COOKIE_NAME)
    if cookie:
        if not csrf_ok(request):
            raise HTTPException(403, "Cookie-authenticated writes need the X-Requested-With: firstfetch header")
        resolved = web_sessions.resolve_session(session, cookie)
        if resolved is None:
            session.commit()  # an expired row is deleted even though the request then fails
            return None
        if resolved.refreshed:
            session.commit()
            if response is not None:
                set_session_cookie(response, cookie)  # the sliding 30 days start again
        return Viewer(role=resolved.user.role, user_id=resolved.user.id, via="session")
    return None


def has_credentials(request: Request) -> bool:
    from app.web_sessions import COOKIE_NAME

    return bool(
        request.headers.get(ADMIN_HEADER) or request.headers.get(USER_HEADER) or request.cookies.get(COOKIE_NAME)
    )


def optional_viewer(request: Request, response: Response, session: Session = Depends(get_sync_db)) -> Viewer | None:
    """FastAPI dependency: the caller, or None for the public. No database access without credentials."""
    if not has_credentials(request):
        return None
    return viewer_from_request(request, session, response)


def require_viewer(viewer: Viewer | None = Depends(optional_viewer)) -> Viewer:
    """FastAPI dependency for private data: 401 without a valid token."""
    if viewer is None:
        raise HTTPException(
            401,
            "Private data: log in, or send X-Admin-Token or your X-User-Token",
            headers={"WWW-Authenticate": "Token"},
        )
    return viewer
