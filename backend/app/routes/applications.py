"""Application tracker API. Writes go through the ADMIN_TOKEN middleware like every other write."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import tracker
from app.db_sync import get_sync_db

router = APIRouter(prefix="/api/applications", tags=["applications"])


class ApplicationJob(BaseModel):
    title: str
    company: str | None
    location: str | None
    url: str
    score: float | None  # 0-100


class ApplicationOut(BaseModel):
    job_id: int
    status: str
    notes: str | None
    created_at: datetime | None
    updated_at: datetime | None
    status_changed_at: datetime | None
    applied_at: datetime | None
    notified_at: datetime | None
    job: ApplicationJob


class ApplicationPatch(BaseModel):
    status: str | None = Field(None, description=f"One of: {', '.join(tracker.STATUSES)}")
    notes: str | None = Field(None, max_length=5000)


def _out(app, job, analysis) -> ApplicationOut:
    return ApplicationOut(
        job_id=app.job_id,
        status=app.status,
        notes=app.notes,
        created_at=tracker._aware(app.created_at),
        updated_at=tracker._aware(app.updated_at),
        status_changed_at=tracker._aware(app.status_changed_at),
        applied_at=tracker._aware(app.applied_at),
        notified_at=tracker._aware(app.notified_at),
        job=ApplicationJob(
            title=job.title,
            company=job.company,
            location=job.location,
            url=job.url,
            score=round(tracker.base_points(analysis), 1) if analysis else None,
        ),
    )


def parse_statuses(value: str | None) -> set[str] | None:
    wanted = {v.strip().lower() for v in (value or "").split(",") if v.strip()}
    if not wanted or "any" in wanted:
        return None
    bad = wanted - set(tracker.STATUSES)
    if bad:
        raise HTTPException(422, f"status must be 'any' or a comma-separated subset of {', '.join(tracker.STATUSES)}")
    return wanted


@router.get("", response_model=list[ApplicationOut])
def list_applications(
    status: str | None = Query(None, description="Comma-separated statuses, e.g. 'saved,applied' (default: all)"),
    session: Session = Depends(get_sync_db),
) -> list[ApplicationOut]:
    return [_out(*row) for row in tracker.tracked(session, parse_statuses(status))]


@router.patch("/{job_id}", response_model=ApplicationOut)
def update_application(job_id: int, body: ApplicationPatch, session: Session = Depends(get_sync_db)) -> ApplicationOut:
    if body.status is None and body.notes is None:
        raise HTTPException(422, "Send status and/or notes")
    try:
        tracker.set_status(session, job_id, body.status.lower() if body.status else None, notes=body.notes)
    except tracker.UnknownJob:
        raise HTTPException(404, "Job not found") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    session.commit()
    row = next(r for r in tracker.tracked(session) if r[0].job_id == job_id)
    return _out(*row)
