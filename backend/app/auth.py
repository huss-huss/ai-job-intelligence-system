"""Who is asking: the read side of the privacy lock.

Job data and the public (owner-profile) score are open to everyone, for the portfolio demo.
Everything a person *did* with a job (tracker statuses, notes, feedback, talent-network progress)
is private: it is only returned to a caller that proves who they are.

* ``X-Admin-Token: <ADMIN_TOKEN>`` is the owner.

Tokens are compared in constant time. With no ADMIN_TOKEN configured nobody can read private
data over HTTP (fail closed); the Telegram bot still works, it has its own secret.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.config import settings

ADMIN_HEADER = "x-admin-token"


@dataclass(frozen=True)
class Viewer:
    """An authenticated caller. `user_id` is None until the users table exists (owner only)."""

    role: str  # "owner" | "member"
    user_id: int | None = None

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


def admin_token_ok(supplied: str | None) -> bool:
    expected = settings.admin_token
    return bool(expected) and hmac.compare_digest((supplied or "").encode(), expected.encode())


def viewer_from_request(request: Request) -> Viewer | None:
    if admin_token_ok(request.headers.get(ADMIN_HEADER)):
        return Viewer(role="owner")
    return None


def optional_viewer(request: Request) -> Viewer | None:
    """FastAPI dependency: the caller, or None for the public."""
    return viewer_from_request(request)


def require_viewer(request: Request) -> Viewer:
    """FastAPI dependency for private data: 401 without a valid token."""
    viewer = viewer_from_request(request)
    if viewer is None:
        raise HTTPException(401, "Private data: send X-Admin-Token", headers={"WWW-Authenticate": "Token"})
    return viewer
