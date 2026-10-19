"""Shared types and HTTP plumbing for job source adapters."""

from __future__ import annotations

import hashlib
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "FirstFetch/0.2 (+https://github.com/huss-huss/ai-job-intelligence-system)"
# 520-524 are Cloudflare's "origin unreachable/timed out" pages, which clear up on a retry.
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
# When an API reports X-RateLimit-Remaining at or below this, wait for X-RateLimit-Reset first.
RATE_LIMIT_LOW_WATER = 2
MAX_RATE_LIMIT_WAIT = 60.0


@dataclass
class JobPosting:
    """A job listing normalized across sources."""

    source: str
    external_id: str
    url: str
    title: str
    company: str | None = None
    location: str | None = None
    description: str | None = None
    posted_at: datetime | None = None  # when the posting first went live
    updated_at: datetime | None = None  # last edit, for sources that report one (Greenhouse)
    remote: bool | None = None
    tags: list[str] = field(default_factory=list)
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_text: str | None = None
    salary_period: str | None = None  # "hour", "day", "week", "month" or "year"; None means annual
    # Facts some sources state (FreeHire, Jobicy, We Work Remotely); used by worker.hire.
    employment_type: str | None = None  # "full_time", "part_time", "contract", "internship"
    seniority: str | None = None  # intern, junior, mid, senior, lead, director, as the source states it
    regions: list[str] = field(default_factory=list)  # macro-regions, e.g. ["global"], ["africa", "eu"]
    countries: list[str] = field(default_factory=list)  # ISO 3166-1 alpha-2, lowercase
    applied_count: int | None = None
    view_count: int | None = None
    timezone_note: str | None = None
    posting_language: str | None = None  # ISO 639-1
    extra: dict[str, Any] = field(default_factory=dict)  # source-specific facts, kept in jobs_raw.meta
    # Every board this job was seen on (source -> URL), filled by cross-source dedupe.
    source_urls: dict[str, str] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """Stable dedupe key. Uses the source's own ID when it has one."""
        if self.external_id:
            raw = f"{self.source}|{self.external_id}"
        else:
            raw = f"{self.url}|{self.title}|{self.company or ''}"
        return hashlib.sha256(raw.encode()).hexdigest()


class SourceError(RuntimeError):
    """Raised when a source can't be fetched after retries. `status` is the final HTTP status, if any."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SourceAdapter(Protocol):
    """Fetches and normalizes postings from one job source."""

    name: str

    def fetch(self, config: dict[str, Any]) -> list[JobPosting]: ...


class HttpClient:
    """httpx wrapper with retries, exponential backoff, and a minimum interval between requests.

    Public job APIs are free and ask callers not to hammer them, so every adapter goes
    through this client instead of calling httpx directly.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 20.0,
        max_retries: int = 3,
        max_redirects: int = 5,  # a redirect loop (some sites' bot walls) fails fast instead of after 20 hops
        backoff_base: float = 1.0,
        min_interval: float = 1.0,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
            max_redirects=max_redirects,
            transport=transport,
        )
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.min_interval = min_interval
        self._sleep = sleep
        self._last_request = 0.0
        self._rate_limit_wait = 0.0  # seconds to wait before the next request, from X-RateLimit-*

    def get_json(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        max_retries: int | None = None,
        max_retry_wait: float | None = None,
    ) -> Any:
        """GET JSON. `max_retries`/`max_retry_wait` bound this call (e.g. a Retry-After: 300 on a 522)."""
        return self._request(
            "GET", url, params=params, max_retries=max_retries, max_retry_wait=max_retry_wait
        ).json()

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return self._request(method, url, params=params, json=json, headers=headers).json()

    def get_text(
        self,
        url: str,
        *,
        accept: str = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        timeout: float | None = None,
        max_retries: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> str:
        """GET a page as text. `timeout` and `max_retries` override the client defaults for this call."""
        return self._request(
            "GET", url, params=params, headers={"Accept": accept}, timeout=timeout, max_retries=max_retries
        ).text

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        max_retry_wait: float | None = None,
    ) -> httpx.Response:
        retries = self.max_retries if max_retries is None else max_retries
        extra = {} if timeout is None else {"timeout": timeout}
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            self._throttle()
            try:
                resp = self._client.request(method, url, params=params, json=json, headers=headers, **extra)
                self._read_rate_limit(resp)
                if resp.status_code in RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                resp.raise_for_status()
                return resp
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                if status is not None and status not in RETRYABLE_STATUS:
                    detail = exc.response.text[:300]
                    raise SourceError(f"{method} {url} failed with HTTP {status}: {detail}", status) from exc
                last_error = exc
                if attempt < retries:
                    delay = self._retry_delay(exc, attempt)
                    if max_retry_wait is not None:
                        delay = min(delay, max_retry_wait)
                    logger.warning("%s %s failed (%s), retry %d in %.1fs", method, url, exc, attempt + 1, delay)
                    self._sleep(delay)
                    self._rate_limit_wait = 0.0  # Retry-After already covered the window
            except httpx.RequestError as exc:  # e.g. a redirect loop: retrying won't help
                raise SourceError(f"{method} {url} failed: {exc}") from exc
        status = last_error.response.status_code if isinstance(last_error, httpx.HTTPStatusError) else None
        raise SourceError(f"{method} {url} failed after {retries + 1} attempts: {last_error}", status)

    def _retry_delay(self, exc: Exception, attempt: int) -> float:
        if isinstance(exc, httpx.HTTPStatusError):
            retry_after = exc.response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                return min(float(retry_after), 60.0)
        return self.backoff_base * (2**attempt) + random.uniform(0, 0.25)

    def _read_rate_limit(self, resp: httpx.Response) -> None:
        """Pace against X-RateLimit-Remaining/Reset before the API refuses (FreeHire sends them on success).

        A response without the headers means "unknown", not "unlimited": the normal throttle applies.
        """
        remaining, reset = resp.headers.get("X-RateLimit-Remaining"), resp.headers.get("X-RateLimit-Reset")
        if remaining is None or reset is None:
            return
        try:
            remaining_n, reset_s = int(remaining), float(reset)
        except ValueError:
            return
        if remaining_n <= RATE_LIMIT_LOW_WATER:
            self._rate_limit_wait = min(max(reset_s, 0.0), MAX_RATE_LIMIT_WAIT)
            logger.info("%s: %d requests left in the window, pausing %.0fs", resp.url.host, remaining_n, reset_s)

    def _throttle(self) -> None:
        if self._rate_limit_wait > 0:
            self._sleep(self._rate_limit_wait)
            self._rate_limit_wait = 0.0
            self._last_request = time.monotonic()
            return
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = time.monotonic()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def parse_datetime(value: Any) -> datetime | None:
    """Parse ISO strings and unix timestamps (seconds or milliseconds) into aware datetimes."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(ts, tz=UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def parse_rfc822(value: str | None) -> datetime | None:
    """RSS pubDate ("Mon, 19 Oct 2026 14:19:05 +0300") as an aware datetime."""
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return parse_datetime(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_number(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None
