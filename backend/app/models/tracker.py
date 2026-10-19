"""Application tracker, talent-network status and small key/value settings.

These tables are separate from the scraping tables on purpose: the tracker is the owner's
own data (what they did with a job), so a re-scrape or re-analysis never touches it.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base

if TYPE_CHECKING:
    from app.models.job import JobRaw

# Order matters: it's the order the dashboard and /applied show them in.
APPLICATION_STATUSES = ("new", "saved", "applied", "interviewing", "offer", "rejected", "not_interested")
TALENT_NETWORK_STATUSES = ("not_started", "applied", "vetted", "active")


class Application(Base):
    """What the owner did with one job. A row is created when the job is notified or acted on."""

    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs_raw.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new", server_default="new", index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set once the job went out in a Telegram digest or /top; the digest never sends it again.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    job_raw: Mapped["JobRaw"] = relationship("JobRaw")


class TalentNetworkStatus(Base):
    """The owner's progress with one talent network (the catalog itself lives in app/talent.py)."""

    __tablename__ = "talent_networks"

    slug: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="not_started", server_default="not_started")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AppSetting(Base):
    """Small runtime settings changed from the bot or dashboard, e.g. {"telegram": {"paused": true}}."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
