"""Source schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SourceBase(BaseModel):
    """Base source fields."""

    name: str = Field(..., min_length=1, max_length=255)
    base_url: str = Field(..., max_length=2048)
    config: dict[str, Any] | None = None
    enabled: bool = True


class SourceCreate(SourceBase):
    """Create source request."""

    pass


class SourceUpdate(BaseModel):
    """Update source (partial)."""

    name: str | None = Field(None, min_length=1, max_length=255)
    base_url: str | None = Field(None, max_length=2048)
    config: dict[str, Any] | None = None
    enabled: bool | None = None


class SourceOut(SourceBase):
    """Source response."""

    id: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
