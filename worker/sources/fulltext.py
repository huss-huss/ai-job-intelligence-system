"""Full job descriptions for sources whose API returns an excerpt.

RemoteOK and Himalayas sometimes return a shortened description, which under-counts skill matches.
For postings whose description is shorter than a threshold, this fetches the posting page and
extracts the job text: the JSON-LD JobPosting `description` when the page has one, otherwise the
block holding most of the page's text. The API excerpt is kept on any failure.

Fetching is polite and bounded:
- every request goes through HttpClient (throttled, retried, FirstFetch user agent);
- robots.txt is checked per host (cached) and its Crawl-delay is honored;
- a per-run budget caps pages and seconds, because the serverless cron runs every source in one
  function call with a hard time limit;
- a host that fails twice in a row (e.g. a bot challenge) is skipped for the rest of the run.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from worker.sources.base import HttpClient, JobPosting, SourceError
from worker.text import html_to_text

logger = logging.getLogger(__name__)

ROBOTS_AGENT = "FirstFetch"
ROBOTS_TTL_SECONDS = 6 * 3600
MIN_REQUEST_SECONDS = 2.0  # don't start a page fetch with less time than this left in the budget
PAGE_MAX_RETRIES = 1
PAGE_TIMEOUT = 10.0
MAX_HOST_FAILURES = 2
MAX_PAGE_CHARS = 2_000_000


@dataclass(frozen=True)
class FullTextOptions:
    """Which postings get their page fetched, and the per-run budget."""

    min_chars: int = 1500  # fetch when the plain-text description is shorter than this; 0 disables
    max_pages: int = 10
    max_seconds: float = 20.0
    sources: frozenset[str] = frozenset({"remoteok", "himalayas"})

    @classmethod
    def from_settings(cls, settings: Any) -> FullTextOptions:
        sources = {s.strip().lower() for s in settings.full_description_sources.split(",") if s.strip()}
        return cls(
            min_chars=settings.full_description_min_chars,
            max_pages=settings.full_description_max_pages,
            max_seconds=settings.full_description_max_seconds,
            sources=frozenset(sources),
        )

    def wants(self, posting: JobPosting) -> bool:
        return (
            self.min_chars > 0
            and posting.source in self.sources
            and posting.url.startswith(("http://", "https://"))
            and len(posting.description or "") < self.min_chars
        )


@dataclass
class FullTextStats:
    """Counts recorded in run stats. `candidates` = postings whose description was under the threshold."""

    candidates: int = 0
    fetched: int = 0  # page fetched and its text replaced the excerpt
    failed: int = 0  # fetch error, or the page had no longer text; excerpt kept
    skipped_budget: int = 0
    skipped_robots: int = 0
    skipped_host: int = 0  # host failed MAX_HOST_FAILURES times in a row this run

    @property
    def skipped(self) -> int:
        return self.skipped_budget + self.skipped_robots + self.skipped_host

    def as_dict(self) -> dict[str, int]:
        return {
            "candidates": self.candidates,
            "fetched": self.fetched,
            "failed": self.failed,
            "skipped": self.skipped,
            "skipped_budget": self.skipped_budget,
            "skipped_robots": self.skipped_robots,
            "skipped_host": self.skipped_host,
        }


class FetchBudget:
    """At most `max_pages` page fetches and `max_seconds` of wall time, measured from creation."""

    def __init__(self, max_pages: int, max_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_pages = max_pages
        self.max_seconds = max_seconds
        self.clock = clock
        self.started = clock()
        self.pages = 0

    def remaining_seconds(self) -> float:
        return self.max_seconds - (self.clock() - self.started)

    def allows_page(self) -> bool:
        return self.pages < self.max_pages and self.remaining_seconds() >= MIN_REQUEST_SECONDS

    def spend_page(self) -> None:
        self.pages += 1


@dataclass
class _Robots:
    parser: RobotFileParser | None  # None: allow everything (no robots.txt)
    allow_all: bool
    fetched_at: float


class RobotsCache:
    """robots.txt per origin, cached for ROBOTS_TTL_SECONDS.

    Follows RFC 9309: a 4xx (no robots.txt) allows everything; a 5xx or network error is treated
    as "disallow everything" until the entry expires.
    """

    def __init__(self, agent: str = ROBOTS_AGENT, ttl: float = ROBOTS_TTL_SECONDS, clock=time.monotonic) -> None:
        self.agent = agent
        self.ttl = ttl
        self.clock = clock
        self._entries: dict[str, _Robots] = {}

    def _entry(self, url: str, http: HttpClient, timeout: float) -> _Robots:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc.lower()}"
        entry = self._entries.get(origin)
        if entry and self.clock() - entry.fetched_at < self.ttl:
            return entry
        try:
            text = http.get_text(f"{origin}/robots.txt", accept="text/plain", timeout=timeout, max_retries=0)
        except SourceError as exc:
            if exc.status is not None and 400 <= exc.status < 500:
                entry = _Robots(None, True, self.clock())
            else:
                logger.warning("robots.txt for %s unreachable (%s); not fetching pages from it", origin, exc)
                entry = _Robots(None, False, self.clock())
        else:
            parser = RobotFileParser()
            parser.parse(text.splitlines())
            entry = _Robots(parser, False, self.clock())
        self._entries[origin] = entry
        return entry

    def check(self, url: str, http: HttpClient, timeout: float = 5.0) -> tuple[bool, float]:
        """(allowed, crawl delay in seconds) for fetching `url` with our user agent."""
        entry = self._entry(url, http, timeout)
        if entry.parser is None:
            return entry.allow_all, 0.0
        delay = entry.parser.crawl_delay(self.agent)
        return entry.parser.can_fetch(self.agent, url), float(delay or 0)


_default_robots = RobotsCache()  # shared across runs in a warm process


# --- extraction --------------------------------------------------------------

_LD_JSON = re.compile(
    r"<script[^>]+type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL
)
_CONTAINERS = {"div", "section", "article", "main", "td"}
_BOILERPLATE = {
    "head", "nav", "header", "footer", "aside", "form", "button",
    "script", "style", "noscript", "svg", "template",
}  # fmt: skip
_DESCEND_SHARE = 0.6  # step into a child block holding at least this share of its parent's text


def _walk_ld(node: Any) -> Iterable[dict]:
    if isinstance(node, list):
        for item in node:
            yield from _walk_ld(item)
    elif isinstance(node, dict):
        yield node
        yield from _walk_ld(node.get("@graph"))


def _is_job_posting(node: dict) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(isinstance(k, str) and k.split("/")[-1] == "JobPosting" for k in kinds)


def jsonld_description(page: str) -> str:
    """Plain text of the first JSON-LD JobPosting `description` on the page, or ""."""
    for match in _LD_JSON.finditer(page):
        try:
            data = json.loads(match.group(1).strip())
        except ValueError:
            continue
        for node in _walk_ld(data):
            if _is_job_posting(node) and isinstance(node.get("description"), str):
                text = html_to_text(node["description"])
                if text:
                    return text
    return ""


@dataclass
class _Block:
    tag: str
    start: int
    end: int | None = None
    text: int = 0  # characters of non-link text inside, boilerplate excluded
    children: list[_Block] = field(default_factory=list)
    cuts: list[tuple[int, int]] = field(default_factory=list)  # boilerplate ranges to drop


class _BlockParser(HTMLParser):
    """Builds a tree of container blocks with their text size and source offsets."""

    def __init__(self, page: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page = page
        self._line_starts = [0]
        for m in re.finditer("\n", page):
            self._line_starts.append(m.end())
        self.root = _Block("root", 0, len(page))
        self._stack: list[_Block] = [self.root]
        self._skip: list[tuple[str, int]] = []  # open boilerplate elements and their start offsets
        self._links = 0

    def _offset(self) -> int:
        line, col = self.getpos()
        return self._line_starts[line - 1] + col

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _BOILERPLATE:
            self._skip.append((tag, self._offset()))
        elif tag == "a":
            self._links += 1
        elif tag in _CONTAINERS and not self._skip:
            block = _Block(tag, self._offset())
            self._stack[-1].children.append(block)
            self._stack.append(block)

    def handle_endtag(self, tag: str) -> None:
        if tag in _BOILERPLATE:
            # Close the innermost open element with this tag, and anything left unclosed inside it.
            for i in range(len(self._skip) - 1, -1, -1):
                if self._skip[i][0] == tag:
                    start = self._skip[i][1]
                    del self._skip[i:]
                    if not self._skip:
                        end = self.page.find(">", self._offset()) + 1 or len(self.page)
                        self._stack[-1].cuts.append((start, end))
                    break
        elif tag == "a":
            self._links = max(0, self._links - 1)
        elif tag in _CONTAINERS and not self._skip:
            for i in range(len(self._stack) - 1, 0, -1):
                if self._stack[i].tag == tag:
                    end = self.page.find(">", self._offset()) + 1 or len(self.page)
                    for block in self._stack[i:]:
                        block.end = block.end or end
                    del self._stack[i:]
                    break

    def handle_data(self, data: str) -> None:
        if self._skip or self._links:
            return
        size = len(" ".join(data.split()))
        for block in self._stack:
            block.text += size


def _all_cuts(block: _Block) -> list[tuple[int, int]]:
    cuts = list(block.cuts)
    for child in block.children:
        cuts.extend(_all_cuts(child))
    return cuts


def main_block_text(page: str) -> str:
    """Plain text of the block holding most of the page's text, ignoring nav/header/footer/links.

    Starting from the whole page, step into the child block that holds most of the text while it
    holds at least _DESCEND_SHARE of it; stop where the text is spread across several children.
    """
    parser = _BlockParser(page)
    parser.feed(page)
    parser.close()
    node = parser.root
    while node.children:
        best = max(node.children, key=lambda b: b.text)
        if node.text == 0 or best.text < _DESCEND_SHARE * node.text:
            break
        node = best
    start, end = node.start, node.end if node.end is not None else len(page)
    pieces, pos = [], start
    for cut_start, cut_end in sorted(c for c in _all_cuts(node) if start <= c[0] < end):
        if cut_start >= pos:
            pieces.append(page[pos:cut_start])
            pos = cut_end
    pieces.append(page[pos:end])
    return html_to_text("".join(pieces))


def extract_job_text(page: str) -> str:
    """Main job text from a posting page: JSON-LD JobPosting first, else the largest content block."""
    page = page[:MAX_PAGE_CHARS]
    return jsonld_description(page) or main_block_text(page)


# --- fetching ----------------------------------------------------------------


def fetch_full_descriptions(
    postings: list[JobPosting],
    http: HttpClient,
    options: FullTextOptions,
    *,
    robots: RobotsCache | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[JobPosting], FullTextStats]:
    """Replace short descriptions with the posting page's text, within the budget.

    Returns new postings (same order) and the counts. A posting whose page can't be fetched,
    is disallowed by robots.txt, or yields no longer text keeps its API description.
    """
    robots = robots or _default_robots
    stats = FullTextStats()
    budget = FetchBudget(options.max_pages, options.max_seconds, clock)
    host_failures: dict[str, int] = {}
    last_fetch: dict[str, float] = {}
    out: list[JobPosting] = []
    for posting in postings:
        if not options.wants(posting):
            out.append(posting)
            continue
        stats.candidates += 1
        host = urlsplit(posting.url).netloc.lower()
        if host_failures.get(host, 0) >= MAX_HOST_FAILURES:
            stats.skipped_host += 1
            out.append(posting)
            continue
        if not budget.allows_page():
            stats.skipped_budget += 1
            out.append(posting)
            continue
        allowed, delay = robots.check(posting.url, http, timeout=min(5.0, budget.remaining_seconds()))
        if not allowed:
            stats.skipped_robots += 1
            out.append(posting)
            continue
        if host in last_fetch and delay:
            wait = delay - (clock() - last_fetch[host])
            if wait > 0:
                sleep(wait)
        if not budget.allows_page():  # robots.txt or Crawl-delay used up the time
            stats.skipped_budget += 1
            out.append(posting)
            continue
        budget.spend_page()
        last_fetch[host] = clock()
        try:
            # Split what's left between the attempts so a slow page can't run far past the budget.
            timeout = max(1.0, min(PAGE_TIMEOUT, budget.remaining_seconds() / (PAGE_MAX_RETRIES + 1)))
            page = http.get_text(posting.url, timeout=timeout, max_retries=PAGE_MAX_RETRIES)
            text = extract_job_text(page)
        except (SourceError, ValueError) as exc:
            logger.info("full description for %s not fetched: %s", posting.url, exc)
            text = ""
        if len(text) > len(posting.description or ""):
            host_failures[host] = 0
            stats.fetched += 1
            out.append(replace(posting, description=text))
        else:
            host_failures[host] = host_failures.get(host, 0) + 1
            stats.failed += 1
            out.append(posting)
    return out, stats
