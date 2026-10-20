"""Talent networks: the catalog with your own status per platform. Needs a logged-in viewer (app/auth.py)."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import talent, tracker
from app.auth import Viewer, require_viewer
from app.db_sync import get_sync_db

router = APIRouter(prefix="/api/talent-networks", tags=["talent-networks"])


class TalentNetworkOut(BaseModel):
    slug: str
    name: str
    url: str
    summary: str
    status: str | None = None
    updated_at: datetime | None = None


class TalentNetworkPatch(BaseModel):
    status: str


def _out(network: dict) -> TalentNetworkOut:
    return TalentNetworkOut(**{**network, "updated_at": tracker._aware(network["updated_at"])})


@router.get("", response_model=list[TalentNetworkOut], response_model_exclude_none=True)
def list_talent_networks(
    session: Session = Depends(get_sync_db), viewer: Viewer = Depends(require_viewer)
) -> list[TalentNetworkOut]:
    return [_out(n) for n in talent.list_networks(session, viewer.user_id)]


@router.patch("/{slug}", response_model=TalentNetworkOut)
def update_talent_network(
    slug: str,
    body: TalentNetworkPatch,
    session: Session = Depends(get_sync_db),
    viewer: Viewer = Depends(require_viewer),
) -> TalentNetworkOut:
    try:
        talent.set_status(session, viewer.user_id, slug, body.status.lower(), tracker.utcnow())
    except KeyError:
        raise HTTPException(404, "Unknown talent network") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    session.commit()
    return _out(next(n for n in talent.list_networks(session, viewer.user_id) if n["slug"] == slug))
