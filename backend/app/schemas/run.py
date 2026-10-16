"""Run (scrape/analyze) schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class RunOut(BaseModel):
    """Run response."""

    id: int
    type: str
    source_id: int | None
    status: str
    started_at: datetime
    ended_at: datetime | None
    stats: dict[str, Any] | None

    model_config = {"from_attributes": True}
