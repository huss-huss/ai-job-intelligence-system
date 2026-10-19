"""Talent networks: platforms where one application and one vetting process can lead to many
remote roles. The catalog is static and public; each user's status per platform is stored
(talent_networks, one row per user and platform) and private.

Descriptions say what each platform is, not how likely you are to get in.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import TalentNetworkStatus
from app.models.tracker import TALENT_NETWORK_STATUSES

STATUSES = TALENT_NETWORK_STATUSES

CATALOG: list[dict[str, str]] = [
    {
        "slug": "andela",
        "name": "Andela",
        "url": "https://andela.com",
        "summary": "Global talent marketplace, with deep roots in Africa, that places vetted engineers "
        "in long-term remote roles with companies.",
    },
    {
        "slug": "turing",
        "name": "Turing",
        "url": "https://www.turing.com",
        "summary": "Remote engineering platform: one profile and technical assessment, then matching to "
        "full-time remote roles and AI/LLM training projects.",
    },
    {
        "slug": "toptal",
        "name": "Toptal",
        "url": "https://www.toptal.com",
        "summary": "Freelance network that screens applicants through several interview and test stages "
        "before matching them with client engagements.",
    },
    {
        "slug": "micro1",
        "name": "micro1",
        "url": "https://www.micro1.ai",
        "summary": "Recruiting platform that vets engineers with an AI interviewer, then matches them to "
        "remote roles and contracts.",
    },
    {
        "slug": "mercor",
        "name": "Mercor",
        "url": "https://mercor.com",
        "summary": "Hiring marketplace with an AI-led interview; profiles are matched to contract roles, "
        "many of them AI training and evaluation work.",
    },
    {
        "slug": "arc",
        "name": "Arc.dev",
        "url": "https://arc.dev",
        "summary": "Remote developer job board and marketplace; passing its vetting unlocks freelance and "
        "full-time placements.",
    },
]
SLUGS = {n["slug"] for n in CATALOG}


def list_networks(session: Session, user_id: int) -> list[dict]:
    rows = {
        r.slug: r
        for r in session.execute(select(TalentNetworkStatus).where(TalentNetworkStatus.user_id == user_id)).scalars()
    }
    out = []
    for network in CATALOG:
        row = rows.get(network["slug"])
        out.append(
            {**network, "status": row.status if row else "not_started", "updated_at": row.updated_at if row else None}
        )
    return out


def set_status(session: Session, user_id: int, slug: str, status: str, now: datetime) -> None:
    if slug not in SLUGS:
        raise KeyError(slug)
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    row = session.get(TalentNetworkStatus, (user_id, slug))
    if row is None:
        session.add(TalentNetworkStatus(user_id=user_id, slug=slug, status=status, updated_at=now))
    else:
        row.status, row.updated_at = status, now
    session.flush()
