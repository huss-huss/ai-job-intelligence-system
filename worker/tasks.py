"""
RQ job definitions. Run with: PYTHONPATH=backend rq worker
(or from repo root: PYTHONPATH=backend python -m rq worker default)
"""

import logging
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
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

from worker import user_scoring  # noqa: E402
from worker.ai import Extraction, HeuristicExtractor, LLMExtractor, build_caller  # noqa: E402
from worker.budget import SqlUsageStore  # noqa: E402
from worker.config import settings  # noqa: E402
from worker.notion import NotionSync  # noqa: E402
from worker.pipeline import ScoredJob, analyze, count_llm, fair_select, score_posting  # noqa: E402
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


def _analysis_values(scored: ScoredJob) -> dict:
    f, m = scored.extraction.fields, scored.match
    extracted = {
        **f.model_dump(),
        "match": m.to_dict(),
        "hire": scored.hire.to_dict(),
        # 0-100 each; the tracker and Telegram digest read these two keys
        "hire_probability": scored.hire.score,
        "rank": scored.rank,  # geometric mean of match.score and hire_probability
        "method": scored.extraction.method,
    }
    if scored.extraction.model:
        extracted["llm_model"] = scored.extraction.model
    return dict(
        extracted_json=extracted,
        score=m.score / 100,  # stored 0-1; the dashboard renders it as a percentage
        seniority=f.seniority,
        salary_min=scored.posting.salary_min,
        salary_max=scored.posting.salary_max,
        skills=m.matched_skills or f.skills or None,
    )


def _store_analysis(session, job_id: int, scored: ScoredJob) -> None:
    values = _analysis_values(scored)
    analysis = session.execute(select(JobAnalysis).where(JobAnalysis.job_id == job_id)).scalar_one_or_none()
    if analysis:
        for key, value in values.items():
            setattr(analysis, key, value)
    else:
        session.add(JobAnalysis(job_id=job_id, **values))


def run_analyze_task(job_ids: list[int]) -> dict:
    """Extract facts by rules, score against the profile, store analysis, notify on strong matches.

    LLM extraction is a separate step, `run_llm_enrich_task`, run once a day on the day's best jobs.
    """
    profile = _load_profile()
    extractor = HeuristicExtractor()
    notion = (
        NotionSync(settings.notion_token, settings.notion_database_id)
        if settings.notion_token and settings.notion_database_id
        else None
    )

    with get_sync_session() as session:
        run = Run(type="analyze", source_id=None, status="running")
        session.add(run)
        session.flush()
        stats = {"processed": 0, "notified": 0}
        try:
            # Each user's own score for every job (worker/user_scoring.py): the facts are extracted
            # once, then scored per profile, which is rules only.
            users = user_scoring.user_profiles(session)
            stats["users_scored"] = len(users)
            now = datetime.now(UTC)
            user_rows: list[dict] = []
            for jid in job_ids:
                job = session.execute(select(JobRaw).where(JobRaw.id == jid)).scalar_one_or_none()
                if not job:
                    continue
                posting = _to_posting(job)
                previous = session.execute(select(JobAnalysis).where(JobAnalysis.job_id == jid)).scalar_one_or_none()
                if previous is not None and (previous.extracted_json or {}).get("method") == "llm":
                    # Rescoring (POST /api/analyze) keeps the facts the LLM already read: no new call.
                    extraction = user_scoring.stored_extraction(previous, posting)
                    extraction.model = previous.extracted_json.get("llm_model")
                    scored = score_posting(posting, extraction, profile, now=now)
                else:
                    scored = analyze(posting, profile, extractor, now=now)
                _store_analysis(session, jid, scored)
                user_rows += user_scoring.scores_for_users(jid, posting, scored.extraction, scored, users, now)

                stats["processed"] += 1
                m = scored.match
                if m.score >= profile.notify_threshold and m.eligible is not False:
                    stats["notified"] += 1
                    _notify(scored, notion)

            if user_rows:
                user_scoring.replace_scores(session, user_rows)
            run.status = "completed"
            run.ended_at = datetime.now(UTC)
            run.stats = stats
            session.commit()
        except Exception as exc:
            _fail(session, run, exc, **stats)
            raise
        run_id = run.id
    return {"ok": True, "run_id": run_id, **stats}


ENRICH_WINDOW = timedelta(hours=36)  # the day's jobs, with slack for a late or missed run
NO_RETRY = ("error", "invalid_output")  # a job whose LLM call failed this way isn't tried again


def _session_factory():
    from app.db_sync import SyncSessionLocal

    return SyncSessionLocal


def _open_for_llm(session, now: datetime, window: timedelta) -> dict[int, dict]:
    """extracted_json of recent jobs that only have rules extraction and may still be sent to the LLM."""
    rows = session.execute(
        select(JobAnalysis.job_id, JobAnalysis.extracted_json)
        .join(JobRaw, JobRaw.id == JobAnalysis.job_id)
        .where(JobRaw.created_at >= now - window)
    ).all()
    out = {}
    for job_id, data in rows:
        data = data or {}
        if data.get("method") == "llm" or data.get("llm_fallback") in NO_RETRY:
            continue
        out[job_id] = data
    return out


def llm_candidates(session, now: datetime, window: timedelta = ENRICH_WINDOW) -> list[tuple[int, int, int]]:
    """(rank, fit, job id) of recent jobs that only have rules extraction, best first by the public score."""
    out = []
    for job_id, data in _open_for_llm(session, now, window).items():
        fit = int((data.get("match") or {}).get("score") or 0)
        if fit <= 0:
            continue
        out.append((int(data.get("rank") or 0), fit, job_id))
    out.sort(reverse=True)
    return out


def user_llm_candidates(
    session, user_ids: list[int], now: datetime, window: timedelta = ENRICH_WINDOW
) -> dict[int, list[int]]:
    """Each user's open jobs (as in llm_candidates), best first by *their own* rank, then fit. Two queries."""
    from app.models import UserJobScore

    open_ids = _open_for_llm(session, now, window)
    ranked: dict[int, list[tuple[int, int, int]]] = {uid: [] for uid in user_ids}
    if not open_ids or not user_ids:
        return {uid: [] for uid in user_ids}
    rows = session.execute(
        select(UserJobScore.user_id, UserJobScore.job_id, UserJobScore.rank, UserJobScore.score)
        .join(JobRaw, JobRaw.id == UserJobScore.job_id)
        .where(JobRaw.created_at >= now - window, UserJobScore.user_id.in_(user_ids))
    ).all()
    for user_id, job_id, rank, fit in rows:
        if job_id in open_ids and (fit or 0) > 0:
            ranked[user_id].append((rank or 0, fit, job_id))
    return {uid: [jid for _, _, jid in sorted(items, reverse=True)] for uid, items in ranked.items()}


def select_enrich_jobs(session, n: int, now: datetime, stats: dict) -> dict[int, str]:
    """The day's jobs for LLM extraction: `n` (what's left of the daily cap) shared fairly by active users.

    Active users are those with a valid profile whose digest isn't paused. Each one's list is ranked by
    their own scores (filled in first for anyone missing rows, e.g. a member who joined after the jobs were
    analyzed); `fair_select` takes turns, rotating who goes first by day, and extracts a job on two lists
    once. With no active user it is the public top-N, as before. The total is still at most `n`, so cost
    doesn't depend on how many users there are. Returns {job id: whose list it came from} in pick order
    (a user id as a string, or "public"); stats get the candidates and each list's contribution.
    """
    active = [(u, p) for u, p in user_scoring.user_profiles(session) if not u.paused]
    if not active:
        candidates = llm_candidates(session, now)
        stats["candidates"] = len(candidates)
        picked = {job_id: "public" for _, _, job_id in candidates[:n]}
        stats["selected_by_user"] = {"public": len(picked)}
        return picked
    for user, profile in active:
        user_scoring.score_missing(session, user, since=now - ENRICH_WINDOW, profile=profile, now=now)
    ranked = user_llm_candidates(session, [u.id for u, _ in active], now)
    stats["candidates"] = len(set().union(*ranked.values()))
    order, picked_for, deduped = fair_select(ranked, n, start=now.date().toordinal())
    stats["selected_by_user"] = {str(u.id): sum(1 for v in picked_for.values() if v == u.id) for u, _ in active}
    stats["shared_skipped"] = deduped
    return {job_id: str(picked_for[job_id]) for job_id in order}


def run_llm_enrich_task(*, now: datetime | None = None, client=None, session_factory=None) -> dict:
    """LLM extraction for the day's top-N jobs by rules rank (N = what's left of today's extraction cap),
    shared fairly by the active users' own rankings (`select_enrich_jobs`).

    The facts are per job, so each enriched job is rescored with the LLM's facts for the public profile and
    every user, whoever's list picked it. Calls run in parallel (LLM_CONCURRENCY); no new call starts
    after LLM_ENRICH_MAX_SECONDS. Every call is checked against the budget first (worker/budget.py).
    Never raises for LLM trouble: a failed call keeps the rules analysis and is counted under `llm_fallbacks`.
    """
    now = now or datetime.now(UTC)
    session_factory = session_factory or _session_factory()
    caller = build_caller(settings, store=SqlUsageStore(session_factory), client=client)
    if caller is None:
        return {"ok": True, "skipped": "AI is off (no OPENAI_API_KEY or LLM_BASE_URL)"}
    extractor = LLMExtractor(caller)
    profile = _load_profile()

    with session_factory() as session:
        run = Run(type="llm_enrich", source_id=None, status="running", started_at=now)
        session.add(run)
        session.commit()
        stats: dict = {"candidates": 0, "selected": 0, "enriched": 0, "llm_calls": 0, "llm_fallbacks": {}}
        try:
            remaining = caller.guard.remaining_extractions()
            picked = select_enrich_jobs(session, remaining, now, stats)
            stats["selected"] = len(picked)
            stats["enriched_by_user"] = dict.fromkeys(stats["selected_by_user"], 0)
            jobs = {j.id: j for j in session.execute(select(JobRaw).where(JobRaw.id.in_(list(picked)))).scalars()}
            postings = {jid: _to_posting(jobs[jid]) for jid in picked if jid in jobs}
            session.commit()  # no transaction stays open during the calls

            started = time.monotonic()

            def extract(job_id: int) -> tuple[int, Extraction | None]:
                if time.monotonic() - started > settings.llm_enrich_max_seconds:
                    return job_id, None
                return job_id, extractor.extract(postings[job_id])

            with ThreadPoolExecutor(max_workers=max(1, settings.llm_concurrency)) as pool:
                results = list(pool.map(extract, list(postings)))

            users = user_scoring.user_profiles(session)
            user_rows: list[dict] = []
            for job_id, extraction in results:
                if extraction is None:
                    stats["llm_fallbacks"]["time_budget"] = stats["llm_fallbacks"].get("time_budget", 0) + 1
                    continue
                count_llm(stats, extraction)
                if extraction.method != "llm":
                    reason = extraction.fallback_reason
                    analysis = session.execute(
                        select(JobAnalysis).where(JobAnalysis.job_id == job_id)
                    ).scalar_one_or_none()
                    if analysis is not None and reason in NO_RETRY:
                        analysis.extracted_json = {**(analysis.extracted_json or {}), "llm_fallback": reason}
                    continue
                posting = postings[job_id]
                scored = score_posting(posting, extraction, profile, now=now)
                _store_analysis(session, job_id, scored)
                user_rows += user_scoring.scores_for_users(job_id, posting, extraction, scored, users, now)
                stats["enriched"] += 1
                stats["enriched_by_user"][picked[job_id]] += 1
            if user_rows:
                user_scoring.replace_scores(session, user_rows)
            fallbacks = sum(stats["llm_fallbacks"].values())
            if fallbacks:
                logger.warning("LLM enrichment: %s of %s jobs kept the rules extraction", fallbacks, len(picked))
            summary = caller.guard.summary()
            stats["month_to_date_usd"] = round(summary.month_cost, 6)
            stats["budget_usd"] = caller.guard.monthly_budget_usd
            stats["model"] = caller.model
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
