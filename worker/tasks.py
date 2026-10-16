"""
RQ job definitions. Run with: PYTHONPATH=backend rq worker
(or from repo root: PYTHONPATH=backend python -m rq worker default)
"""

import logging
import sys
import traceback
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

# Ensure backend app is on path when worker runs from repo root
_backend = Path(__file__).resolve().parent.parent / "backend"
if _backend.exists() and str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

import httpx  # noqa: E402
from app.db_sync import get_sync_session  # noqa: E402
from app.models import JobAnalysis, JobRaw, Run, Source  # noqa: E402
from sqlalchemy import select  # noqa: E402

from worker.ai import build_extractor  # noqa: E402
from worker.config import settings  # noqa: E402
from worker.notion import NotionSync  # noqa: E402
from worker.pipeline import ScoredJob, analyze  # noqa: E402
from worker.profile import CandidateProfile, load_profile  # noqa: E402
from worker.sources import JobPosting, fetch_postings  # noqa: E402

logger = logging.getLogger(__name__)

META_FIELDS = ("source", "external_id", "remote", "tags", "salary_min", "salary_max", "salary_currency", "salary_text")


def _source_config(source: Source, params: dict | None) -> dict:
    """Source.config holds the adapter config; older rows only had a base_url to scrape."""
    config = {**(source.config or {}), **(params or {})}
    if "type" not in config:
        config = {**config, "type": "html", "url": source.base_url}
    return config


def _load_profile() -> CandidateProfile:
    path = Path(settings.profile_path)
    if not path.exists():
        logger.warning("Profile %s not found, scoring with defaults (copy profile.example.yaml)", path)
        return CandidateProfile()
    return load_profile(path)


def _to_posting(job: JobRaw) -> JobPosting:
    meta = job.meta or {}
    return JobPosting(
        source=meta.get("source", "unknown"),
        external_id=meta.get("external_id", ""),
        url=job.url,
        title=job.title,
        company=job.company,
        location=job.location,
        description=job.description,
        posted_at=job.posted_at,
        remote=meta.get("remote"),
        tags=meta.get("tags") or [],
        salary_min=meta.get("salary_min"),
        salary_max=meta.get("salary_max"),
        salary_currency=meta.get("salary_currency"),
        salary_text=meta.get("salary_text"),
    )


def _fail(session, run: Run, exc: Exception, **stats) -> None:
    run.status = "failed"
    run.ended_at = datetime.now(UTC)
    run.stats = {**stats, "error": str(exc), "traceback": traceback.format_exc()[:500]}
    session.commit()  # keep the failure record even though we re-raise
    logger.exception("task failed for run %s", run.id)


def run_scrape_task(source_id: int, params: dict | None = None) -> dict:
    """Fetch a source, insert new jobs_raw rows, enqueue analysis for them."""
    with get_sync_session() as session:
        source = session.execute(select(Source).where(Source.id == source_id)).scalar_one_or_none()
        if not source:
            return {"ok": False, "error": "Source not found"}

        run = Run(type="scrape", source_id=source_id, status="running")
        session.add(run)
        session.flush()

        inserted_ids: list[int] = []
        try:
            postings = fetch_postings(_source_config(source, params))
            for p in postings:
                exists = session.execute(
                    select(JobRaw.id).where(JobRaw.source_id == source_id, JobRaw.content_hash == p.content_hash)
                ).scalar_one_or_none()
                if exists:
                    continue
                raw = JobRaw(
                    source_id=source_id,
                    url=p.url,
                    title=p.title[:512],
                    company=p.company,
                    location=p.location,
                    description=p.description,
                    posted_at=p.posted_at,
                    content_hash=p.content_hash,
                    meta={k: v for k, v in asdict(p).items() if k in META_FIELDS},
                )
                session.add(raw)
                session.flush()
                inserted_ids.append(raw.id)

            run.status = "completed"
            run.ended_at = datetime.now(UTC)
            run.stats = {"fetched": len(postings), "inserted": len(inserted_ids)}
            session.commit()
        except Exception as exc:
            _fail(session, run, exc)
            raise
        run_id = run.id

    if inserted_ids:
        from redis import Redis
        from rq import Queue

        Queue("default", connection=Redis.from_url(settings.redis_url)).enqueue(
            run_analyze_task, inserted_ids, job_timeout="15m"
        )
    return {"ok": True, "run_id": run_id, "inserted": len(inserted_ids)}


def run_analyze_task(job_ids: list[int]) -> dict:
    """Extract facts, score against the profile, store analysis, notify on strong matches."""
    profile = _load_profile()
    extractor = build_extractor(settings.openai_api_key, settings.openai_model, settings.llm_base_url)
    notion = (
        NotionSync(settings.notion_token, settings.notion_database_id)
        if settings.notion_token and settings.notion_database_id
        else None
    )

    with get_sync_session() as session:
        run = Run(type="analyze", source_id=None, status="running")
        session.add(run)
        session.flush()
        stats = {"processed": 0, "llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "notified": 0}
        try:
            for jid in job_ids:
                job = session.execute(select(JobRaw).where(JobRaw.id == jid)).scalar_one_or_none()
                if not job:
                    continue
                scored = analyze(_to_posting(job), profile, extractor)
                f, m = scored.extraction.fields, scored.match
                values = dict(
                    extracted_json={**f.model_dump(), "match": m.to_dict(), "method": scored.extraction.method},
                    score=m.score / 100,  # stored 0-1; the dashboard renders it as a percentage
                    seniority=f.seniority,
                    salary_min=scored.posting.salary_min,
                    salary_max=scored.posting.salary_max,
                    skills=m.matched_skills or f.skills or None,
                )
                analysis = session.execute(select(JobAnalysis).where(JobAnalysis.job_id == jid)).scalar_one_or_none()
                if analysis:
                    for key, value in values.items():
                        setattr(analysis, key, value)
                else:
                    session.add(JobAnalysis(job_id=jid, **values))

                stats["processed"] += 1
                if scored.extraction.method == "llm":
                    stats["llm_calls"] += 1
                    stats["input_tokens"] += scored.extraction.input_tokens
                    stats["output_tokens"] += scored.extraction.output_tokens

                if m.score >= profile.notify_threshold and m.eligible is not False:
                    stats["notified"] += 1
                    _notify(scored, notion)

            run.status = "completed"
            run.ended_at = datetime.now(UTC)
            run.stats = stats
            session.commit()
        except Exception as exc:
            _fail(session, run, exc, **stats)
            raise
        run_id = run.id
    return {"ok": True, "run_id": run_id, **stats}


def _notify(scored: ScoredJob, notion: NotionSync | None) -> None:
    """Notifications are best-effort: a Notion or webhook outage must not fail the analysis run."""
    if notion:
        try:
            notion.upsert(scored)
        except Exception:
            logger.exception("Notion sync failed for %s", scored.posting.url)
    if settings.alert_webhook_url:
        try:
            httpx.post(
                settings.alert_webhook_url,
                json={
                    "title": scored.posting.title,
                    "company": scored.posting.company,
                    "url": scored.posting.url,
                    "score": scored.score,
                    "reasons": scored.match.reasons,
                },
                timeout=5.0,
            )
        except httpx.HTTPError:
            logger.warning("Webhook alert failed for %s", scored.posting.url)
