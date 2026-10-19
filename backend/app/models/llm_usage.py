"""LLM usage ledger: one row per OpenAI call (or reservation for one), for the local budget guard.

Rows hold token counts and cost only, never prompts, resumes or generated text. A row starts as
"reserved" with the worst-case estimate before the call, then becomes "recorded" with the real usage
from the response, is deleted if the API rejected the request (not billed), or stays "uncertain"
(estimate kept) when a timeout leaves it unknown whether the call was billed.
"""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base

LLM_FEATURES = ("extract", "cover_letter", "eval")
LLM_USAGE_STATUSES = ("reserved", "recorded", "uncertain")


class LlmUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    day: Mapped[date] = mapped_column(Date, nullable=False)  # UTC
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Who asked (cover letters); NULL for pipeline extraction and for members who were removed.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    feature: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    reasoning_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved", server_default="reserved")

    __table_args__ = (
        Index("ix_llm_usage_day_feature", "day", "feature"),
        Index("ix_llm_usage_user_day", "user_id", "day"),
    )
