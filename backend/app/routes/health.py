"""Health check endpoint."""

from fastapi import APIRouter

from app.schemas import HealthOut

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", response_model=HealthOut)
async def health() -> HealthOut:
    """Liveness/readiness check."""
    return HealthOut()
