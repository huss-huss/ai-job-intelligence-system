"""FreeHire public API (https://freehire.me/openapi.yaml): job search with full descriptions.

Uses `GET /api/v1/agent/jobs/search`, which returns every hit's full description, plus one
optional company lookup (`GET /api/v1/companies?remote_regions=...`) to learn which companies
hire remotely in a region.

API terms worth respecting (from the OpenAPI description):
- No key. The agent search has its own budget of 300 requests/minute, the other reads share
  600/minute. Every response carries X-RateLimit-Limit/Remaining/Reset; HttpClient pauses when
  Remaining runs low and honors 429 Retry-After.
- Send a User-Agent naming the project (HttpClient's default does).
- An unknown parameter is dropped, not refused, and reported in `meta.ignored_params`. A dropped
  filter silently widens the search, so this adapter raises SourceError when that happens.

Config:
    {"type": "freehire",
     "defaults": {"work_mode": "remote", "employment_type": ["full_time", "contract"], "limit": 50},
     "queries": [{"q": "title:backend OR title:python", "regions": ["global", "africa"]}, ...],
     "max_pages": 1,                       # pages per query (limit rows each)
     "company_remote_regions": ["africa"], # look up companies hiring remotely there (optional)
     "company_pages": 6,                   # pages of 100 companies to read for that lookup
     "strict": true,                       # raise when FreeHire ignored a parameter
     "max_seconds": 45}                    # time budget for the whole source; later pages are skipped
FreeHire sometimes answers 522 (origin timeout) with a long Retry-After, so each request retries
at most twice and waits at most RETRY_WAIT_CAP seconds; the company lookup is best-effort.
Query values may be lists; they are sent as repeated parameters (the API's facet grammar).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from worker.salary import normalize_period
from worker.sources.base import HttpClient, JobPosting, SourceError, parse_datetime, to_int, to_number
from worker.sources.normalize import normalize_employment_type, normalize_seniority
from worker.text import fix_mojibake, html_to_text

logger = logging.getLogger(__name__)

BASE_URL = "https://freehire.me/api/v1"
SEARCH_URL = f"{BASE_URL}/agent/jobs/search"
COMPANIES_URL = f"{BASE_URL}/companies"
JOB_PAGE = "https://freehire.me/jobs/{slug}"
MAX_LIMIT = 100
MAX_DEPTH = 10_000  # offset + limit may not exceed this (deeper is a 400)
SEARCH_RETRIES = 2
RETRY_WAIT_CAP = 10.0
DEFAULT_MAX_SECONDS = 45.0  # keeps the daily serverless run (one function call, 300 s) safe

# Keys that configure the adapter rather than being sent to FreeHire.
ADAPTER_KEYS = {
    "type", "name", "queries", "defaults", "max_pages", "max_results", "company_remote_regions",
    "company_pages", "strict", "full_descriptions", "keywords", "max_age_days", "max_seconds",
}  # fmt: skip
DEFAULT_PARAMS: dict[str, Any] = {
    "work_mode": "remote",
    "sort": "posted_at",
    "order": "desc",
    "limit": 50,
    "description_format": "text",
}
REGION_LABELS = {
    "global": "Global",
    "north_america": "North America",
    "latam": "LATAM",
    "eu": "Europe",
    "uk": "UK",
    "mena": "MENA",
    "africa": "Africa",
    "apac": "APAC",
    "cis": "CIS",
}


class IgnoredParamsError(SourceError):
    """FreeHire dropped a parameter it doesn't read; the result is wider than the query asked for."""


def check_ignored_params(meta: dict[str, Any] | None, params: dict[str, Any]) -> None:
    """Raise when `meta.ignored_params` is present (it's absent on a clean request, never [])."""
    ignored = (meta or {}).get("ignored_params")
    if not ignored:
        return
    parts = []
    for entry in ignored:
        name = entry.get("param", "?")
        hint = f" (did you mean {entry['did_you_mean']!r}?)" if entry.get("did_you_mean") else ""
        parts.append(f"{name!r}{hint}")
    raise IgnoredParamsError(f"FreeHire ignored parameter(s) {', '.join(parts)} in query {params}")


def _location(j: dict[str, Any]) -> str | None:
    """The posting's own location string, or a label built from its resolved regions/countries."""
    own = (j.get("location") or "").strip()
    if own:
        return own
    regions = [REGION_LABELS.get(r, r) for r in j.get("regions") or [] if r != "none"]
    countries = [c.upper() for c in j.get("countries") or []]
    label = ", ".join(regions + countries)
    return label or None


class FreeHireAdapter:
    name = "freehire"

    def __init__(self, http: HttpClient, clock: Callable[[], float] = time.monotonic) -> None:
        self.http = http
        self._clock = clock
        self._deadline = float("inf")

    def _out_of_time(self, what: str) -> bool:
        if self._clock() < self._deadline:
            return False
        logger.warning("FreeHire time budget spent; skipping %s", what)
        return True

    def fetch(self, config: dict[str, Any]) -> list[JobPosting]:
        strict = config.get("strict", True)
        self._deadline = self._clock() + float(config.get("max_seconds", DEFAULT_MAX_SECONDS))
        postings: list[JobPosting] = []
        for params in self.query_params(config):
            if self._out_of_time(f"query {params.get('q')!r}"):
                break
            postings.extend(self._search(params, int(config.get("max_pages", 1)), strict))
        try:
            hiring = self.companies_hiring_remotely_in(
                list(config.get("company_remote_regions") or []), int(config.get("company_pages", 6)), strict
            )
        except IgnoredParamsError:
            raise
        except SourceError as exc:  # an optional signal: the jobs are still useful without it
            logger.warning("FreeHire company lookup failed, continuing without it: %s", exc)
            hiring = {}
        for p in postings:
            slug = p.extra.get("company_slug")
            if slug and slug in hiring:
                p.extra["company_hires_remote_in"] = sorted(hiring[slug])
        return postings

    @staticmethod
    def query_params(config: dict[str, Any]) -> list[dict[str, Any]]:
        """One param dict per query: DEFAULT_PARAMS < config['defaults'] < top-level keys < the query."""
        top = {k: v for k, v in config.items() if k not in ADAPTER_KEYS}
        base = {**DEFAULT_PARAMS, **(config.get("defaults") or {}), **top}
        queries = config.get("queries") or [{}]
        merged = [{**base, **q} for q in queries]
        for params in merged:
            params["limit"] = min(int(params.get("limit", 50)), MAX_LIMIT)
        return merged

    def _search(self, params: dict[str, Any], max_pages: int, strict: bool) -> list[JobPosting]:
        postings: list[JobPosting] = []
        offset = int(params.get("offset", 0))
        limit = params["limit"]
        for page in range(max(max_pages, 1)):
            if offset + limit > MAX_DEPTH or (page and self._out_of_time(f"page {page + 1}")):
                break
            page_params = {**params, "offset": offset}
            data = self.http.get_json(
                SEARCH_URL, params=page_params, max_retries=SEARCH_RETRIES, max_retry_wait=RETRY_WAIT_CAP
            )
            meta = data.get("meta") or {}
            try:
                check_ignored_params(meta, params)
            except IgnoredParamsError:
                if strict:
                    raise
                logger.error("FreeHire ignored params %s for %s", meta.get("ignored_params"), params)
            rows = data.get("data") or []
            postings.extend(self.parse(j) for j in rows if j.get("public_slug") and j.get("title"))
            offset += len(rows)
            if len(rows) < limit or offset >= int(meta.get("total") or 0):
                break
        return postings

    def companies_hiring_remotely_in(self, regions: list[str], pages: int, strict: bool) -> dict[str, set[str]]:
        """company slug -> regions it hires remotely in, from FreeHire's company index (cheap budget)."""
        hiring: dict[str, set[str]] = {}
        for region in regions:
            for page in range(max(pages, 0)):
                if self._out_of_time(f"company page {page + 1}"):
                    return hiring
                params = {"remote_regions": region, "limit": 100, "offset": page * 100}
                data = self.http.get_json(COMPANIES_URL, params=params, max_retries=1, max_retry_wait=RETRY_WAIT_CAP)
                try:
                    check_ignored_params(data.get("meta"), params)
                except IgnoredParamsError:
                    if strict:
                        raise
                    logger.error("FreeHire ignored params %s for %s", (data.get("meta") or {}), params)
                rows = data.get("data") or []
                for company in rows:
                    if company.get("slug"):
                        hiring.setdefault(company["slug"], set()).add(region)
                if len(rows) < 100:
                    break
        return hiring

    @staticmethod
    def parse(j: dict[str, Any]) -> JobPosting:
        enrichment = j.get("enrichment") or {}
        reality = j.get("reality") or {}
        work_mode = j.get("work_mode")
        # A posting whose source keeps restating its date ("fake freshness") is dated from when
        # FreeHire first saw it instead.
        posted = j.get("created_at") if reality.get("fake_freshness") else (j.get("posted_at") or j.get("created_at"))
        slug = j["public_slug"]
        tags = list(j.get("skills") or [])
        if enrichment.get("category"):
            tags.append(enrichment["category"])
        extra = {
            "origin": j.get("source"),  # the board or ATS FreeHire crawled it from, e.g. "djinni"
            "freehire_url": JOB_PAGE.format(slug=slug),
            "company_slug": j.get("company_slug"),
            "first_seen_at": j.get("created_at"),
            "reality": reality.get("class"),
            "fake_freshness": reality.get("fake_freshness"),
            "repost_count": reality.get("repost_count"),
            "english_level": enrichment.get("english_level"),
            "experience_years_min": enrichment.get("experience_years_min"),
            "visa_sponsorship": enrichment.get("visa_sponsorship"),
            "requires_clearance": j.get("requires_clearance"),
        }
        return JobPosting(
            source="freehire",
            external_id=slug,
            url=j.get("url") or JOB_PAGE.format(slug=slug),
            title=fix_mojibake(j["title"].strip()),
            company=fix_mojibake(j.get("company") or "") or None,
            location=_location(j),
            description=html_to_text(j.get("description")),
            posted_at=parse_datetime(posted),
            remote={"remote": True, "hybrid": False, "onsite": False}.get(work_mode),
            tags=tags,
            salary_min=to_number(enrichment.get("salary_min")),
            salary_max=to_number(enrichment.get("salary_max")),
            salary_currency=enrichment.get("salary_currency"),
            salary_period=normalize_period(enrichment.get("salary_period")) or enrichment.get("salary_period"),
            employment_type=normalize_employment_type(enrichment.get("employment_type")),
            seniority=normalize_seniority(enrichment.get("seniority")),
            regions=[r for r in j.get("regions") or [] if isinstance(r, str)],
            countries=[c.lower() for c in j.get("countries") or [] if isinstance(c, str)],
            applied_count=to_int(j.get("applied_count")),
            view_count=to_int(j.get("view_count")),
            timezone_note=enrichment.get("timezone_note"),
            posting_language=enrichment.get("posting_language"),
            extra={k: v for k, v in extra.items() if v not in (None, "")},
        )
