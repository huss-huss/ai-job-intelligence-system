"""Scrape the enabled sources inside a time budget, in a rotating order (the daily cron run).

The order comes from the run history: sources never scraped go first, then the one whose last scrape
*attempt* (`runs` rows of type "scrape", whatever their status) is oldest. A source the budget left out
today was attempted earliest of all, so it goes first tomorrow; a source that keeps failing or is slow
moves to the back instead of blocking the others. The progress is the scrape runs themselves, written per
source as it finishes, so a function that is killed mid-run still resumes where it stopped.
"""

import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, Protocol, TypeVar

from sqlalchemy import func, select

from app.models import Run, Source

clock: Callable[[], float] = time.monotonic  # tests replace it with a fake clock


class _HasId(Protocol):
    id: int


S = TypeVar("S", bound=_HasId)


def rotation_order(sources: Sequence[S], last_attempt: dict[int, datetime]) -> list[S]:
    """Never-attempted sources first (by id), then the oldest last attempt first (ties by id)."""
    never = sorted((s for s in sources if s.id not in last_attempt), key=lambda s: s.id)
    seen = sorted((s for s in sources if s.id in last_attempt), key=lambda s: (last_attempt[s.id], s.id))
    return never + seen


async def last_scrape_attempts(db, source_ids: list[int]) -> dict[int, datetime]:
    """{source id: start of its latest scrape run}, one grouped query."""
    if not source_ids:
        return {}
    rows = await db.execute(
        select(Run.source_id, func.max(Run.started_at))
        .where(Run.type == "scrape", Run.source_id.in_(source_ids))
        .group_by(Run.source_id)
    )
    return {source_id: started for source_id, started in rows.all() if started is not None}


async def scrape_within_budget(
    sources: Sequence[Source],
    dispatch: Callable[[int, dict | None], Awaitable[Any]],
    *,
    max_seconds: float,
    started: float,
) -> tuple[list[dict], list[int]]:
    """Dispatch each source in order; none starts once `max_seconds` have passed since `started`.

    A source already running when the budget runs out finishes, so the budget is when the last source may
    *start*. `max_seconds <= 0` means no limit. Returns (results, ids of the sources left for the next run).
    """
    results: list[dict] = []
    for i, source in enumerate(sources):
        if max_seconds > 0 and clock() - started >= max_seconds:
            return results, [s.id for s in sources[i:]]
        try:
            job_id = await dispatch(source.id, source.config)
            results.append({"source_id": source.id, "job_id": job_id})
        except Exception as exc:  # one failing source shouldn't stop the others
            results.append({"source_id": source.id, "error": str(exc)[:300]})
    return results, []
