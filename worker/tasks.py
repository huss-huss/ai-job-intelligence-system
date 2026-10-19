"""
RQ job definitions. Run with: PYTHONPATH=backend rq worker
(or from repo root: PYTHONPATH=backend python -m rq worker default)
"""

import logging
import sys
import traceback
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

# Ensure backend app is on path when worker runs from repo root
_backend = Path(__file__).resolve().parent.parent / "backend"
if _backend.exists() and str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

import httpx  # noqa: E402
import yaml  # noqa: E402
from app.db_sync import get_sync_session  # noqa: E402
from app.models import JobAnalysis, JobRaw, Run, Source  # noqa: E402
from sqlalchemy import select  # noqa: E402

from worker.ai import build_extractor  # noqa: E402
from worker.config import settings  # noqa: E402
from worker.notion import NotionSync  # noqa: E402
from worker.pipeline import ScoredJob, analyze  # noqa: E402
from worker.profile import CandidateProfile, load_profile  # noqa: E402
from worker.sources import HttpClient, JobPosting, fetch_postings  # noqa: E402
from worker.sources.base import parse_datetime  # noqa: E402
from worker.sources.fulltext import FullTextOptions, fetch_full_descriptions  # noqa: E402

logger = logging.getLogger(__name__)

META_FIELDS = (
    "source", "external_id", "remote", "tags",
    "salary_min", "salary_max", "salary_currency", "salary_text", "salary_period",
    # hire-probability inputs (worker/hire.py); stored so POST /api/analyze can rescore
    "employment_type", "seniority", "regions", "countries", "applied_count", "view_count",
    "timezone_note", "posting_language", "extra", "source_urls",
)  # fmt: skip


# jobs_raw text columns are VARCHAR(512).
COLUMN_LIMIT = 512


def _fit(value: str | None) -> str | None:
    """Shorten text for a VARCHAR(512) column, marking the cut with an ellipsis."""
    if value is None or len(value) <= COLUMN_LIMIT:
        return value
    return value[: COLUMN_LIMIT - 1].rstrip(" ,;") + "…"


def _meta(p: JobPosting) -> dict:
    """Source facts without a jobs_raw column. Datetimes are stored as ISO strings (JSONB)."""
    meta = {k: v for k, v in asdict(p).items() if k in META_FIELDS and v not in (None, [], {})}
    if p.updated_at:
        meta["updated_at"] = p.updated_at.isoformat()
    # Some boards list every eligible country (800+ chars). The column gets a shortened copy, so
    # keep the full text here: eligibility must see "Ethiopia" even if it falls past the cut.
    if p.location and len(p.location) > COLUMN_LIMIT:
        meta["location_full"] = p.location
    return meta


# Facts that change while a posting stays open; refreshed on every scrape so re-analysis sees them.
VOLATILE_META = ("applied_count", "view_count")


def _refresh_meta(existing: JobRaw, p: JobPosting) -> None:
    """Keep the "updated" date and applicant/view counts of an already stored posting current."""
    meta = dict(existing.meta or {})
    if p.updated_at:
        meta["updated_at"] = p.updated_at.isoformat()
    for key in VOLATILE_META:
        value = getattr(p, key)
        if value is not None:
            meta[key] = value
    if meta != (existing.meta or {}):
        existing.meta = meta


def _full_text_options(config: dict) -> FullTextOptions:
    """FULL_DESCRIPTION_* settings; a source's config can force it on or off with "full_descriptions"."""
    options = FullTextOptions.from_settings(settings)
    flag = config.get("full_descriptions")
    if flag is True:
        return replace(options, sources=options.sources | {config.get("type", "")})
    if flag is False:
        return replace(options, min_chars=0)
    return options


def _source_config(source: Source, params: dict | None) -> dict:
    """Source.config holds the adapter config; older rows only had a base_url to scrape."""
    config = {**(source.config or {}), **(params or {})}
    if "type" not in config:
        config = {**config, "type": "html", "url": source.base_url}
    return config


def _load_profile() -> CandidateProfile:
    if settings.profile_yaml:
        return CandidateProfile.model_validate(yaml.safe_load(settings.profile_yaml) or {})
    example = Path(__file__).resolve().parent.parent / "profile.example.yaml"
    for path in (Path(settings.profile_path), example):
        if path.exists():
            if path == example:
                logger.info("PROFILE_PATH %s not found; using bundled profile.example.yaml", settings.profile_path)
            return load_profile(path)
    return CandidateProfile()


def _to_posting(job: JobRaw) -> JobPosting:
    meta = job.meta or {}
    return JobPosting(
        source=meta.get("source", "unknown"),
        external_id=meta.get("external_id", ""),
        url=job.url,
        title=job.title,
        company=job.company,
        location=meta.get("location_full") or job.location,
        description=job.description,
        posted_at=job.posted_at,
        updated_at=parse_datetime(meta.get("updated_at")),
        remote=meta.get("remote"),
        tags=meta.get("tags") or [],
        salary_min=meta.get("salary_min"),
        salary_max=meta.get("salary_max"),
        salary_currency=meta.get("salary_currency"),
        salary_text=meta.get("salary_text"),
        salary_period=meta.get("salary_period"),
        employment_type=meta.get("employment_type"),
        seniority=meta.get("seniority"),
        regions=meta.get("regions") or [],
        countries=meta.get("countries") or [],
        applied_count=meta.get("applied_count"),
        view_count=meta.get("view_count"),
        timezone_note=meta.get("timezone_note"),
        posting_language=meta.get("posting_language"),
        extra=meta.get("extra") or {},
        source_urls=meta.get("source_urls") or {},
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
            config = _source_config(source, params)
            with HttpClient() as http:
                postings = fetch_postings(config, http=http)
                new: list[JobPosting] = []
                for p in postings:
                    existing = session.execute(
                        select(JobRaw).where(JobRaw.source_id == source_id, JobRaw.content_hash == p.content_hash)
                    ).scalar_one_or_none()
                    if existing:
                        _refresh_meta(existing, p)
                        continue
                    new.append(p)
                # Only new postings: pages of jobs stored on an earlier run aren't fetched again.
                new, full_text = fetch_full_descriptions(new, http, _full_text_options(config))
            for p in new:
                raw = JobRaw(
                    source_id=source_id,
                    url=p.url,
                    title=_fit(p.title),
                    company=_fit(p.company),
                    location=_fit(p.location),
                    description=p.description,
                    posted_at=p.posted_at,
                    content_hash=p.content_hash,
                    meta=_meta(p),
                )
                session.add(raw)
                session.flush()
                inserted_ids.append(raw.id)

            run.status = "completed"
            run.ended_at = datetime.now(UTC)
            run.stats = {
                "fetched": len(postings),
                "inserted": len(inserted_ids),
                "full_descriptions": full_text.as_dict(),
            }
            session.commit()
        except Exception as exc:
            _fail(session, run, exc)
            raise
        run_id = run.id

    if inserted_ids and settings.queue_mode == "inline":
        analysis = run_analyze_task(inserted_ids)
        return {"ok": True, "run_id": run_id, "inserted": len(inserted_ids), "analysis": analysis}
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
                    extracted_json={
                        **f.model_dump(),
                        "match": m.to_dict(),
                        "hire": scored.hire.to_dict(),
                        # 0-100 each; the tracker and Telegram digest read these two keys
                        "hire_probability": scored.hire.score,
                        "rank": scored.rank,  # geometric mean of match.score and hire_probability
                        "method": scored.extraction.method,
                    },
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
