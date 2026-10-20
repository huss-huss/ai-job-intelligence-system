"""Dashboard login: "Log in with Telegram" sessions (app/web_sessions.py).

* ``POST /api/auth/telegram``: the Login Widget payload. A genuine, fresh payload from an invited user
  starts a session (``ff_session`` cookie); anyone else gets the same 403.
* ``POST /api/auth/token-session``: turns a valid ``X-User-Token`` (a /dashboard link) into a session,
  so the link doesn't need to stay in this browser's storage.
* ``POST /api/auth/logout``: ends the cookie's session and clears the cookie.
* ``GET /api/auth/session``: who is looking (any of the three ways to authenticate), or
  ``{"authenticated": false}`` for the public: no 401, so the public page stays quiet.

Every POST here needs ``X-Requested-With: firstfetch`` (CSRF, see app/auth.py): a page elsewhere can't
log a visitor in to someone else's account or out of theirs.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import users, web_sessions
from app.auth import USER_HEADER, Viewer, clear_session_cookie, csrf_ok, optional_viewer, set_session_cookie
from app.db_sync import get_sync_db
from app.models import User
from app.telegram.config import telegram_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])

NO_STORE = {"Cache-Control": "no-store"}
NOT_ALLOWED = "This Telegram account can't log in here. Ask the owner for an invite."


class SessionOut(BaseModel):
    authenticated: bool
    role: str | None = None
    display_name: str | None = None
    via: str | None = None  # "session" | "token" | "admin"


def _require_csrf(request: Request) -> None:
    if not csrf_ok(request):
        raise HTTPException(403, "Send the X-Requested-With: firstfetch header")


def _out(user: User, via: str) -> SessionOut:
    return SessionOut(authenticated=True, role=user.role, display_name=users.label(user), via=via)


def _start(session: Session, response: Response, request: Request, user: User) -> SessionOut:
    web_sessions.purge_expired(session)
    # A login replaces the session this browser had, if any.
    web_sessions.delete_session(session, request.cookies.get(web_sessions.COOKIE_NAME))
    token = web_sessions.create_session(session, user, request.headers.get("user-agent"))
    user.last_seen = web_sessions.utcnow()
    session.commit()
    set_session_cookie(response, token)
    response.headers.update(NO_STORE)
    return _out(user, "session")


@router.post("/telegram", response_model=SessionOut, response_model_exclude_none=True)
def telegram_login(
    request: Request,
    response: Response,
    payload: dict[str, Any] = Body(...),
    session: Session = Depends(get_sync_db),
) -> SessionOut:
    _require_csrf(request)
    if not telegram_settings.bot_token:
        raise HTTPException(503, "Web login isn't configured")
    try:
        telegram_id = web_sessions.verify_login(payload, telegram_settings.bot_token)
    except web_sessions.InvalidLogin:
        raise HTTPException(401, "Invalid or expired Telegram login, try again") from None
    users.ensure_owner(session, telegram_settings.chat_id)  # the owner's row follows TELEGRAM_CHAT_ID
    user = web_sessions.user_for_telegram_id(session, telegram_id)
    if user is None:
        session.commit()
        raise HTTPException(403, NOT_ALLOWED)
    return _start(session, response, request, user)


@router.post("/token-session", response_model=SessionOut, response_model_exclude_none=True)
def token_session(request: Request, response: Response, session: Session = Depends(get_sync_db)) -> SessionOut:
    _require_csrf(request)
    user = users.user_by_dashboard_token(session, request.headers.get(USER_HEADER))
    if user is None:
        raise HTTPException(401, "Send a valid X-User-Token")
    return _start(session, response, request, user)


@router.post("/logout", status_code=204)
def logout(request: Request, session: Session = Depends(get_sync_db)) -> Response:
    _require_csrf(request)
    web_sessions.delete_session(session, request.cookies.get(web_sessions.COOKIE_NAME))
    session.commit()
    response = Response(status_code=204, headers=NO_STORE)
    clear_session_cookie(response)
    return response


@router.get("/session", response_model=SessionOut, response_model_exclude_none=True)
def current_session(
    request: Request,
    response: Response,
    viewer: Viewer | None = Depends(optional_viewer),
    session: Session = Depends(get_sync_db),
) -> SessionOut:
    response.headers.update(NO_STORE)
    user = session.get(User, viewer.user_id) if viewer is not None else None
    if viewer is None or user is None:
        if request.cookies.get(web_sessions.COOKIE_NAME):
            clear_session_cookie(response)  # expired or revoked: stop sending it
        return SessionOut(authenticated=False)
    return _out(user, viewer.via)
