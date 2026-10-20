"""Dashboard login sessions ("Log in with Telegram", app/web_sessions.py).

The cookie carries a random 256-bit session id; only its SHA-256 is stored here. A row dies with its
user (ON DELETE CASCADE, and app/users.py deletes them explicitly too), when it expires, on logout,
and when the user rotates or revokes their dashboard link.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class WebSession(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # SHA-256 of the User-Agent at login, so a list of sessions can be told apart without storing it.
    user_agent_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
