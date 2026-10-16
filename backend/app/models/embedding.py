"""pgvector embedding model."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from pgvector.sqlalchemy import Vector

from app.database import Base


class Embedding(Base):
    """Vector embedding for a job (for semantic search)."""

    __tablename__ = "embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs_raw.id", ondelete="CASCADE"), nullable=False, index=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)  # OpenAI ada-002 dimension
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped["datetime"] = mapped_column(DateTime(timezone=True), server_default=func.now())

    job_raw: Mapped["JobRaw"] = relationship("JobRaw", back_populates="embeddings")
