"""Job source adapters. `fetch_postings(config)` is the entry point used by the worker and CLI."""

from __future__ import annotations

from typing import Any

from worker.sources.adapters import (
    ArbeitnowAdapter,
    GreenhouseAdapter,
    HimalayasAdapter,
    LeverAdapter,
    RemoteOKAdapter,
    RemotiveAdapter,
)
from worker.sources.base import HttpClient, JobPosting, SourceAdapter, SourceError
from worker.sources.html import HtmlAdapter

API_ADAPTERS = {
    a.name: a
    for a in (RemotiveAdapter, RemoteOKAdapter, ArbeitnowAdapter, GreenhouseAdapter, LeverAdapter, HimalayasAdapter)
}
SOURCE_TYPES = sorted([*API_ADAPTERS, HtmlAdapter.name])


def get_adapter(source_type: str, http: HttpClient) -> SourceAdapter:
    if source_type == HtmlAdapter.name:
        return HtmlAdapter()
    try:
        return API_ADAPTERS[source_type](http)
    except KeyError:
        raise SourceError(f"Unknown source type {source_type!r}. Available: {', '.join(SOURCE_TYPES)}") from None


def fetch_postings(config: dict[str, Any], http: HttpClient | None = None) -> list[JobPosting]:
    """Fetch postings for one source config, deduplicated by content hash."""
    source_type = config.get("type")
    if not source_type:
        raise SourceError("Source config needs a 'type'")
    owns_client = http is None
    http = http or HttpClient()
    try:
        postings = get_adapter(source_type, http).fetch(config)
    finally:
        if owns_client:
            http.close()
    seen: set[str] = set()
    unique = []
    for p in postings:
        if p.content_hash not in seen:
            seen.add(p.content_hash)
            unique.append(p)
    limit = config.get("max_results")
    return unique[: int(limit)] if limit else unique


__all__ = ["HttpClient", "JobPosting", "SourceError", "SOURCE_TYPES", "fetch_postings", "get_adapter"]
