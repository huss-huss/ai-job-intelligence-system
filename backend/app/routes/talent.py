"""Talent networks: static catalog plus your status per platform (writes need the admin token)."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import talent, tracker
from app.db_sync import get_sync_db

router = APIRouter(prefix="/api/talent-networks", tags=["talent-networks"])


class TalentNetworkOut(BaseModel):
    slug: str
    name: str
    url: str
    summary: str
    status: str
    updated_at: datetime | None


class TalentNetworkPatch(BaseModel):
    status: str


@router.get("", response_model=list[TalentNetworkOut])
def list_talent_networks(session: Session = Depends(get_sync_db)) -> list[TalentNetworkOut]:
    return [
        TalentNetworkOut(**{**n, "updated_at": tracker._aware(n["updated_at"])}) for n in talent.list_networks(session)
    ]


@router.patch("/{slug}", response_model=TalentNetworkOut)
def update_talent_network(
    slug: str, body: TalentNetworkPatch, session: Session = Depends(get_sync_db)
) -> TalentNetworkOut:
    try:
        talent.set_status(session, slug, body.status.lower(), tracker.utcnow())
    except KeyError:
        raise HTTPException(404, "Unknown talent network") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    session.commit()
    network = next(n for n in talent.list_networks(session) if n["slug"] == slug)
    return TalentNetworkOut(**{**network, "updated_at": tracker._aware(network["updated_at"])})
