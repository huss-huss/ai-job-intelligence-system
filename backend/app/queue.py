"""Redis queue connection and job enqueueing (for worker RQ)."""

from redis import Redis

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
