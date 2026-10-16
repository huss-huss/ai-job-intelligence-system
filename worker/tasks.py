"""
RQ job definitions. Run with: PYTHONPATH=backend rq worker
(or from repo root: PYTHONPATH=backend python -m rq worker default)
"""
from pathlib import Path
import sys

# Ensure backend app is on path when worker runs from repo root
_backend = Path(__file__).resolve().parent.parent / "backend"
if _backend.exists() and str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))

from app.db_sync import get_sync_session
from app.models import Source, JobRaw, JobAnalysis, Run
from sqlalchemy import select
from datetime import datetime

from worker.scraper import scrape_source, ScrapedJob
from worker.ai import extract_with_llm, compute_score, ExtractedFields
from worker.config import settings


def _content_hash(url: str, title: str, company: str | None) -> str:
    import hashlib
    return hashlib.sha256(f"{url}|{title}|{company or ''}".encode()).hexdigest()


def run_scrape_task(source_id: int, params: dict) -> dict:
    """Scrape a source, upsert jobs_raw, enqueue analyze tasks."""
    with get_sync_session() as session:
        source = session.execute(select(Source).where(Source.id == source_id)).scalar_one_or_none()
        if not source:
            return {"ok": False, "error": "Source not found"}

        run = Run(type="scrape", source_id=source_id, status="running")
        session.add(run)
        session.flush()

        inserted_ids: list[int] = []
        try:
            jobs = scrape_source(source.base_url, source.config or params)
            for j in jobs:
                ch = _content_hash(j.url, j.title, j.company)
                existing = session.execute(
                    select(JobRaw).where(JobRaw.source_id == source_id, JobRaw.content_hash == ch)
                ).scalar_one_or_none()
                if existing:
                    continue
                raw = JobRaw(
                    source_id=source_id,
                    url=j.url,
                    title=j.title,
                    company=j.company,
                    location=j.location,
                    description=j.description,
                    posted_at=j.posted_at,
                    content_hash=ch,
                )
                session.add(raw)
                session.flush()
                inserted_ids.append(raw.id)

            run.status = "completed"
            run.ended_at = datetime.utcnow()
            run.stats = {"scraped": len(jobs), "inserted": len(inserted_ids)}
            session.commit()

            # Enqueue analyze for new jobs
            if inserted_ids:
                from redis import Redis
                from rq import Queue
                r = Redis.from_url(settings.redis_url, decode_responses=True)
                q = Queue("default", connection=r)
                q.enqueue(run_analyze_task, inserted_ids, job_timeout="10m")
        except Exception as e:
            import traceback
            error_str = str(e)
            error_trace = traceback.format_exc()
            run.status = "failed"
            run.ended_at = datetime.utcnow()
            run.stats = {"error": error_str, "traceback": error_trace[:500]}  # Limit traceback length
            session.commit()  # Ensure error is saved even if we re-raise
            # Log to stderr so it appears in worker logs
            import sys
            print(f"ERROR in scrape_task for source {source_id}: {error_str}", file=sys.stderr)
            print(error_trace, file=sys.stderr)
            raise
        run_id = run.id
    return {"ok": True, "run_id": run_id, "inserted": len(inserted_ids)}


def run_analyze_task(job_ids: list[int]) -> dict:
    """Analyze jobs with LLM, save jobs_analysis, optional alert."""
    with get_sync_session() as session:
        run = Run(type="analyze", source_id=None, status="running")
        session.add(run)
        session.flush()
        run_id = run.id
        processed = 0
        try:
            for jid in job_ids:
                job = session.execute(select(JobRaw).where(JobRaw.id == jid)).scalar_one_or_none()
                if not job:
                    continue
                text = f"{job.title}\n{job.company or ''}\n{job.description or ''}"
                extracted = extract_with_llm(
                    job.description or job.title,
                    settings.openai_api_key,
                    settings.openai_model,
                )
                score = compute_score(extracted, text)
                salary = extracted.salary or {}
                # Upsert analysis
                existing = session.execute(select(JobAnalysis).where(JobAnalysis.job_id == jid)).scalar_one_or_none()
                if existing:
                    existing.extracted_json = extracted.model_dump()
                    existing.score = score
                    existing.seniority = extracted.seniority
                    existing.salary_min = salary.get("min") or None
                    existing.salary_max = salary.get("max") or None
                    existing.skills = extracted.skills or None
                else:
                    session.add(
                        JobAnalysis(
                            job_id=jid,
                            extracted_json=extracted.model_dump(),
                            score=score,
                            seniority=extracted.seniority,
                            salary_min=salary.get("min") or None,
                            salary_max=salary.get("max") or None,
                            skills=extracted.skills or None,
                        )
                    )
                processed += 1
                # Optional: send alert if score >= threshold
                if score >= settings.alert_score_threshold and settings.alert_webhook_url:
                    try:
                        import httpx
                        httpx.post(
                            settings.alert_webhook_url,
                            json={"job_id": jid, "score": score, "title": job.title},
                            timeout=5.0,
                        )
                    except Exception:
                        pass
            run.status = "completed"
            run.ended_at = datetime.utcnow()
            run.stats = {"processed": processed}
            session.commit()
        except Exception as e:
            import traceback
            error_str = str(e)
            error_trace = traceback.format_exc()
            run.status = "failed"
            run.ended_at = datetime.utcnow()
            run.stats = {"error": error_str, "processed": processed, "traceback": error_trace[:500]}
            session.commit()  # Ensure error is saved even if we re-raise
            # Log to stderr so it appears in worker logs
            import sys
            print(f"ERROR in analyze_task: {error_str}", file=sys.stderr)
            print(error_trace, file=sys.stderr)
            raise
    return {"ok": True, "run_id": run_id, "processed": processed}
