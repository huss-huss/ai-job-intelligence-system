"""Job and analysis schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class JobRawIn(BaseModel):
    """Raw job input (e.g. from scraper)."""

    url: str = Field(..., max_length=2048)
    title: str = Field(..., max_length=512)
    company: str | None = None
    location: str | None = None
    description: str | None = None
    posted_at: datetime | None = None


class JobAnalysisOut(BaseModel):
    """Analysis output for a job."""

    seniority: str | None
    skills: list[str] | None
    salary_min: float | None
    salary_max: float | None
    extracted_json: dict[str, Any]
    score: float
    created_at: datetime

    model_config = {"from_attributes": True}


class JobOut(BaseModel):
    """Job list/detail response (raw + optional analysis)."""

    id: int
    source_id: int
    url: str
    title: str
    company: str | None
    location: str | None
    description: str | None
    posted_at: datetime | None
    content_hash: str
    created_at: datetime
    analysis: JobAnalysisOut | None = None

    model_config = {"from_attributes": True}


class JobListParams(BaseModel):
    """Query params for GET /jobs."""

    status: str | None = None  # e.g. analyzed
    source: int | None = None
    min_score: float | None = None
    q: str | None = None
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)
