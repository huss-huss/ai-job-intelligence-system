"""Job raw and analysis models."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base

if TYPE_CHECKING:
    from app.models.alert import Alert
    from app.models.embedding import Embedding
    from app.models.source import Source


class JobRaw(Base):
    """Raw scraped job listing (before AI analysis)."""

    __tablename__ = "jobs_raw"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True)
    url: Mapped[str] = mapped_column(String(2048), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    company: Mapped[str | None] = mapped_column(String(512), nullable=True)
    location: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # dedupe: hash(url+title+company)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source: Mapped["Source"] = relationship("Source", back_populates="jobs_raw")
    analysis: Mapped["JobAnalysis | None"] = relationship(
        "JobAnalysis", back_populates="job_raw", uselist=False, cascade="all, delete-orphan"
    )
    embeddings: Mapped[list["Embedding"]] = relationship("Embedding", back_populates="job_raw", cascade="all, delete-orphan")
    alerts: Mapped[list["Alert"]] = relationship("Alert", back_populates="job_raw", cascade="all, delete-orphan")

    __table_args__ = (Index("ix_jobs_raw_content_hash_source", "content_hash", "source_id", unique=True),)


class JobAnalysis(Base):
    """AI-extracted structured fields and score for a job."""

    __tablename__ = "jobs_analysis"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs_raw.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    extracted_json: Mapped[dict] = mapped_column(JSONB, nullable=False)  # seniority, skills, salary, etc.
    score: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    seniority: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    salary_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    skills: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    job_raw: Mapped["JobRaw"] = relationship("JobRaw", back_populates="analysis")
