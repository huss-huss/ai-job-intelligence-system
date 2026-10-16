"""Common response schemas."""

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class HealthOut(BaseModel):
    """Health check response."""

    status: str = "ok"
    service: str = "api"


class Paginated(BaseModel, Generic[T]):
    """Paginated list wrapper."""

    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int
