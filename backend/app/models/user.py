"""Users (owner and invited members), invites, the invite-code throttle and per-user job scores.

The owner is the person running the deployment (TELEGRAM_CHAT_ID, ADMIN_TOKEN). Members join with a
single-use invite code from the owner and get their own profile, tracker, feedback and digest.
Secrets (invite codes, dashboard tokens) are stored as SHA-256 hashes only.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, false
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base

USER_ROLES = ("owner", "member")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # NULL only for the owner before TELEGRAM_CHAT_ID is configured.
    telegram_chat_id: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member", server_default="member")
    # A CandidateProfile (worker/profile.py) as JSON. NULL (owner only) means "the deployment's profile":
    # PROFILE_YAML, else PROFILE_PATH, else profile.example.yaml.
    profile: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Digest overrides; NULL uses TELEGRAM_DIGEST_SIZE / TELEGRAM_DIGEST_MIN_SCORE.
    digest_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    digest_min_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    # SHA-256 of the personal dashboard link token (/dashboard in the bot); NULL when none was issued.
    dashboard_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    profile_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Invite(Base):
    """A single-use invite. Only the SHA-256 of the code is stored; the code itself is shown once."""

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class AuthThrottle(Base):
    """Failed invite-code attempts per key: "chat:<sha256 of the chat id>" or "global"."""

    __tablename__ = "auth_throttle"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserJobScore(Base):
    """One user's score for one job, from their own profile (worker/user_scoring.py).

    jobs_analysis keeps the public score from the deployment profile; this is what the user's digest,
    /top, /today and ranking use.
    """

    __tablename__ = "user_job_scores"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs_raw.id", ondelete="CASCADE"), primary_key=True, index=True)
    score: Mapped[int] = mapped_column(Integer, nullable=False)  # fit, 0-100
    hire_probability: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0-100
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0-100
    eligible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    match: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)  # reasons, breakdown, matched_skills
    hire: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # factors, reasons
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_user_job_scores_user_rank", "user_id", "rank"),)
