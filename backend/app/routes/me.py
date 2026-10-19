"""GET /api/me: who the token belongs to, so the dashboard can say whose private view it shows."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import users
from app.auth import Viewer, require_viewer
from app.db_sync import get_sync_db
from app.models import User

router = APIRouter(prefix="/api", tags=["me"])


class MeOut(BaseModel):
    role: str
    display_name: str


@router.get("/me", response_model=MeOut)
def me(viewer: Viewer = Depends(require_viewer), session: Session = Depends(get_sync_db)) -> MeOut:
    user = session.get(User, viewer.user_id)
    return MeOut(role=viewer.role, display_name=users.label(user) if user else "Owner")
