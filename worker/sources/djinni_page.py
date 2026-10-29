"""Who may apply to a Djinni job, read from the job page.

Djinni's RSS items carry no location, so every Djinni posting looked like "location not specified"
and the strict Telegram digest dropped all of them. Each job page states it twice:
- JSON-LD JobPosting `applicantLocationRequirements`: an AdministrativeArea with `addressRegion`
  ("Europe") or `addressCountry` ("UA", or a list); absent when the job is open worldwide;
- the sidebar's `<span class="location-text">` above "Countries where we consider candidates"
  ("Ukraine", "Countries of Europe or Ukraine", "Worldwide").
The page also gives the minimum experience (`experienceRequirements.monthsOfExperience`) and the
required languages ("Ukrainian: Native"), which feed the strict digest's blockers.

Pages are fetched like fulltext.py does it: through HttpClient, robots.txt checked, a per-run
page and time budget, and a host that fails twice in a row is skipped for the rest of the run.
A posting whose page can't be read keeps its empty location and is tried again on a later run.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from worker.sources.base import HttpClient, JobPosting, SourceError
from worker.sources.fulltext import (
    _LD_JSON,
    MAX_HOST_FAILURES,
    MAX_PAGE_CHARS,
    PAGE_MAX_RETRIES,
    PAGE_TIMEOUT,
    FetchBudget,
    RobotsCache,
    _default_robots,
    _is_job_posting,
    _walk_ld,
)

logger = logging.getLogger(__name__)

CHECKED = "djinni_page"  # extra key set once a posting's page has been read

_LOCATION_TEXT = re.compile(r'<span[^>]*class="[^"]*\blocation-text\b[^"]*"[^>]*>(.*?)</span>', re.DOTALL)
_CONSIDER = "Countries where we consider candidates"
_LANGUAGES = re.compile(r"Required languages</h2>(.*?)(?:<h2|$)", re.DOTALL)
_DETAIL_ROW = re.compile(r'class="detail-rows__name">(.*?)</span>.*?class="detail-rows__value">(.*?)</span>', re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")

# Region names in addressRegion → the macro-region codes JobPosting.regions uses.
_REGIONS = {"europe": "eu", "worldwide": "global", "africa": "africa", "emea": "emea"}
# Codes Djinni uses in addressCountry (alpha-2 or alpha-3) → (alpha-2, display name).
_COUNTRIES = {
    "UA": ("ua", "Ukraine"), "UKR": ("ua", "Ukraine"),
    "PL": ("pl", "Poland"), "POL": ("pl", "Poland"),
    "PT": ("pt", "Portugal"), "PRT": ("pt", "Portugal"),
    "ES": ("es", "Spain"), "ESP": ("es", "Spain"),
    "IT": ("it", "Italy"), "ITA": ("it", "Italy"),
    "DE": ("de", "Germany"), "DEU": ("de", "Germany"),
    "RO": ("ro", "Romania"), "ROU": ("ro", "Romania"),
    "BG": ("bg", "Bulgaria"), "BGR": ("bg", "Bulgaria"),
    "GE": ("ge", "Georgia"), "GEO": ("ge", "Georgia"),
    "AM": ("am", "Armenia"), "ARM": ("am", "Armenia"),
    "MD": ("md", "Moldova"), "MDA": ("md", "Moldova"),
    "CZ": ("cz", "Czechia"), "CZE": ("cz", "Czechia"),
    "SK": ("sk", "Slovakia"), "SVK": ("sk", "Slovakia"),
    "LT": ("lt", "Lithuania"), "LTU": ("lt", "Lithuania"),
    "LV": ("lv", "Latvia"), "LVA": ("lv", "Latvia"),
    "EE": ("ee", "Estonia"), "EST": ("ee", "Estonia"),
    "CY": ("cy", "Cyprus"), "CYP": ("cy", "Cyprus"),
    "GB": ("gb", "United Kingdom"), "GBR": ("gb", "United Kingdom"),
    "US": ("us", "United States"), "USA": ("us", "United States"),
    "ET": ("et", "Ethiopia"), "ETH": ("et", "Ethiopia"),
}  # fmt: skip


@dataclass(frozen=True)
class DjinniPageOptions:
    max_pages: int = 30
    max_seconds: float = 40.0

    @classmethod
    def from_settings(cls, settings: Any) -> DjinniPageOptions:
        return cls(max_pages=settings.djinni_page_max_pages, max_seconds=settings.djinni_page_max_seconds)


@dataclass
class DjinniPageStats:
    candidates: int = 0
    read: int = 0
    failed: int = 0
    skipped: int = 0  # budget, robots.txt or a failing host

    def as_dict(self) -> dict[str, int]:
        return {"candidates": self.candidates, "read": self.read, "failed": self.failed, "skipped": self.skipped}


@dataclass
class DjinniDetails:
    location: str = ""
    regions: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    experience_years_min: int | None = None
    languages: dict[str, str] = field(default_factory=dict)  # {"Ukrainian": "Native"}


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAGS.sub(" ", fragment)).split())


def _job_posting_ld(page: str) -> dict | None:
    for match in _LD_JSON.finditer(page):
        try:
            data = json.loads(match.group(1).strip())
        except ValueError:
            continue
        for node in _walk_ld(data):
            if _is_job_posting(node):
                return node
    return None


def _requirements(node: dict) -> tuple[list[str], list[str], list[str]]:
    """(regions, alpha-2 countries, display names) from applicantLocationRequirements."""
    reqs = node.get("applicantLocationRequirements")
    regions, countries, names = [], [], []
    for req in reqs if isinstance(reqs, list) else [reqs]:
        if not isinstance(req, dict):
            continue
        address = req.get("address") if isinstance(req.get("address"), dict) else req
        region = address.get("addressRegion") or (req.get("name") if not address.get("addressCountry") else None)
        if isinstance(region, str) and region.strip():
            names.append(region.strip())
            code = _REGIONS.get(region.strip().lower())
            if code:
                regions.append(code)
        raw = address.get("addressCountry")
        codes = raw if isinstance(raw, list) else re.split(r"[,;\s]+", raw) if isinstance(raw, str) else []
        for code in (c.strip().upper() for c in codes if isinstance(c, str) and c.strip()):
            alpha2, name = _COUNTRIES.get(code, (code.lower() if len(code) == 2 else "", code))
            if alpha2:
                countries.append(alpha2)
            names.append(name)
    return regions, countries, names


def parse_djinni_page(page: str) -> DjinniDetails | None:
    """Location, experience and languages from a Djinni job page; None when it isn't one."""
    page = page[:MAX_PAGE_CHARS]
    node = _job_posting_ld(page)
    label = ""
    for match in _LOCATION_TEXT.finditer(page):
        if _CONSIDER in page[match.end() : match.end() + 400]:
            label = _text(match.group(1))
            break
    if node is None and not label:
        return None

    details = DjinniDetails()
    regions, countries, names = _requirements(node or {})
    details.regions, details.countries = regions, countries
    if label:
        details.location = label  # what the page shows, e.g. "Countries of Europe or Ukraine"
    elif names:
        details.location = ", ".join(names)
    if label.lower() == "worldwide" or (node is not None and not names and not label and _CONSIDER in page):
        details.location = details.location or "Worldwide"
    if details.location.lower() == "worldwide" and "global" not in details.regions:
        details.regions.append("global")

    months = ((node or {}).get("experienceRequirements") or {}).get("monthsOfExperience")
    if isinstance(months, (int, float)) and months >= 12:
        details.experience_years_min = int(months // 12)

    langs = _LANGUAGES.search(page)
    if langs:
        for name, level in _DETAIL_ROW.findall(langs.group(1)):
            if _text(name):
                details.languages[_text(name)] = _text(level)
    return details


def apply_details(posting: JobPosting, details: DjinniDetails) -> JobPosting:
    """The posting with the page's facts; existing values win where the page has none."""
    extra = {**posting.extra, CHECKED: True}
    if details.experience_years_min:
        extra["experience_years_min"] = details.experience_years_min
    description = posting.description or ""
    if details.languages:
        extra["required_languages"] = details.languages
        # The blocker rules read the description ("Ukrainian (Native)" → requires Ukrainian).
        line = "Required languages: " + ", ".join(f"{n} ({lvl})" if lvl else n for n, lvl in details.languages.items())
        if line not in description:
            description = f"{description}\n\n{line}".strip()
    return replace(
        posting,
        location=details.location or posting.location,
        regions=details.regions or posting.regions,
        countries=details.countries or posting.countries,
        description=description,
        extra=extra,
    )


def wants(posting: JobPosting) -> bool:
    return (
        posting.source == "djinni"
        and not posting.extra.get(CHECKED)
        and posting.url.startswith("https://djinni.co/jobs/")
    )


def fetch_djinni_details(
    postings: list[JobPosting],
    http: HttpClient,
    options: DjinniPageOptions,
    *,
    robots: RobotsCache | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[JobPosting], DjinniPageStats]:
    """Fill location/experience/languages from each Djinni posting's page, within the budget.

    Returns new postings (same order) and the counts. A page that can't be fetched or parsed leaves
    its posting unchanged (and unmarked, so a later run tries again).
    """
    robots = robots or _default_robots
    stats = DjinniPageStats()
    budget = FetchBudget(options.max_pages, options.max_seconds, clock)
    failures = 0
    last_fetch: float | None = None
    out: list[JobPosting] = []
    for posting in postings:
        if not wants(posting):
            out.append(posting)
            continue
        stats.candidates += 1
        if failures >= MAX_HOST_FAILURES or not budget.allows_page():
            stats.skipped += 1
            out.append(posting)
            continue
        allowed, delay = robots.check(posting.url, http, timeout=min(5.0, budget.remaining_seconds()))
        if not allowed:
            stats.skipped += 1
            out.append(posting)
            continue
        if last_fetch is not None and delay:
            wait = delay - (clock() - last_fetch)
            if wait > 0:
                sleep(wait)
        if not budget.allows_page():
            stats.skipped += 1
            out.append(posting)
            continue
        budget.spend_page()
        last_fetch = clock()
        try:
            timeout = max(1.0, min(PAGE_TIMEOUT, budget.remaining_seconds() / (PAGE_MAX_RETRIES + 1)))
            details = parse_djinni_page(http.get_text(posting.url, timeout=timeout, max_retries=PAGE_MAX_RETRIES))
        except (SourceError, ValueError) as exc:
            logger.info("djinni page %s not read: %s", posting.url, exc)
            details = None
        if details is None:
            failures += 1
            stats.failed += 1
            out.append(posting)
            continue
        failures = 0
        stats.read += 1
        out.append(apply_details(posting, details))
    return out, stats
