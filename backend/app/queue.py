"""Hand scrape/analyze jobs to the RQ worker, or run them in-process in inline mode."""

import sys
from pathlib import Path

from redis import Redis
from starlette.concurrency import run_in_threadpool

from app.config import settings

_redis: Redis | None = None


def get_redis() -> Redis:
    """Return Redis connection (sync, for RQ)."""
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


def enqueue_scrape_task(source_id: int, params: dict | None = None) -> str | None:
    """Enqueue a scrape task for the worker. Returns job id or None."""
    from rq import Queue
    queue = Queue("default", connection=get_redis())
    job = queue.enqueue("worker.tasks.run_scrape_task", source_id, params or {}, job_timeout="15m")
    return job.id if job else None


def enqueue_analyze_task(job_ids: list[int]) -> str | None:
    """Enqueue an analyze task for the worker. Returns job id or None."""
    from rq import Queue
    queue = Queue("default", connection=get_redis())
    job = queue.enqueue("worker.tasks.run_analyze_task", job_ids, job_timeout="10m")
    return job.id if job else None


def _worker_tasks():
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)
    from worker import tasks

    return tasks


async def dispatch_scrape(source_id: int, params: dict | None = None) -> str | None:
    """Queue a scrape (redis mode) or run scrape + analysis now (inline mode). Returns a job id."""
    if settings.queue_mode == "inline":
        result = await run_in_threadpool(_worker_tasks().run_scrape_task, source_id, params or {})
        return f"inline:{result.get('run_id')}" if result.get("ok") else None
    return enqueue_scrape_task(source_id, params)


async def dispatch_analyze(job_ids: list[int]) -> str | None:
    if settings.queue_mode == "inline":
        result = await run_in_threadpool(_worker_tasks().run_analyze_task, job_ids)
        return f"inline:{result.get('run_id')}"
    return enqueue_analyze_task(job_ids)


def enqueue_llm_enrich_task() -> str | None:
    from rq import Queue

    queue = Queue("default", connection=get_redis())
    job = queue.enqueue("worker.tasks.run_llm_enrich_task", job_timeout="10m")
    return job.id if job else None


async def dispatch_llm_enrich() -> dict:
    """LLM extraction for the day's best jobs (worker.tasks.run_llm_enrich_task). Never raises.

    Inline mode runs it now, after the day's scrapes, so the digest sees it. Redis mode queues it; jobs
    the worker analyzes after it ran are considered the next day (the window is 36 hours).
    """
    try:
        if settings.queue_mode == "inline":
            return await run_in_threadpool(_worker_tasks().run_llm_enrich_task)
        return {"queued": enqueue_llm_enrich_task()}
    except Exception as exc:  # the scrape results and the digest matter more than AI extraction
        return {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
