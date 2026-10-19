"""Discovery pipeline: fetch → extract facts → score. Shared by the RQ worker and the CLI."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from worker.ai import Extraction, Extractor, HeuristicExtractor, LLMExtractor
from worker.hire import HireResult, estimate_hire_probability, rank_score
from worker.matching import MatchResult, location_eligibility, score_job
from worker.profile import CandidateProfile
from worker.sources import HttpClient, JobPosting, SourceError, fetch_postings
from worker.sources.fulltext import FullTextOptions, FullTextStats, fetch_full_descriptions

logger = logging.getLogger(__name__)

# Only spend LLM tokens on jobs that already look promising from cheap rules.
LLM_PRESCORE_THRESHOLD = 35


_COMPANY_SUFFIXES = re.compile(r"\b(inc|llc|ltd|gmbh|corp|co|company|limited|ab|bv|sa|srl|oy)\b\.?")
# Query parameters that only track where a click came from; dropped when comparing URLs.
_TRACKING_PARAMS = re.compile(r"^(utm_\w+|ref|referrer|source|src|gh_src|lever-source|trk)$", re.IGNORECASE)
# Boards whose terms ask for a link back to the listing: when one of them is in a merged group, its
# URL stays the job's URL.
LINK_BACK_SOURCES = ("weworkremotely", "jobicy", "remoteok", "remotive", "arbeitnow", "workingnomads", "djinni")
# Sources that re-publish other boards' postings. Their location can come from a third-party copy
# ("Roskilde, Denmark" for a job the original board lists as "Anywhere in the World"), so a
# board that posted the job directly wins on location.
AGGREGATOR_SOURCES = ("freehire",)


def _norm(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.lower()).strip()


def canonical_url(url: str) -> str:
    """host + path, lowercased, without "www.", trailing slash or tracking parameters."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAMS.match(k)))
    return f"{host}{parts.path.rstrip('/').lower()}" + (f"?{query}" if query else "")


def cross_source_key(posting: JobPosting) -> str:
    """Same job reposted on several boards: normalized title + company."""
    company = _COMPANY_SUFFIXES.sub("", (posting.company or "").lower())
    return f"{_norm(posting.title)}|{_norm(company)}"


def _title_keys(posting: JobPosting) -> list[str]:
    """Title+company keys. "Acme: Backend Engineer" (We Work Remotely's style, which other boards copy
    verbatim into the title) also yields the key for title "Backend Engineer" at "Acme"."""
    keys = [cross_source_key(posting)] if posting.company else []
    prefix, sep, rest = posting.title.partition(": ")
    if sep and rest:
        keys.append(cross_source_key(replace(posting, title=rest, company=prefix)))
    return keys


def _richness(p: JobPosting) -> tuple[int, int]:
    """(structured facts the posting states, description length): higher is richer."""
    facts = [
        p.company, p.location, p.posted_at, p.salary_max or p.salary_min, p.employment_type, p.seniority,
        p.applied_count is not None, p.view_count is not None, p.regions or p.countries, p.timezone_note,
    ]  # fmt: skip
    return sum(1 for f in facts if f), len(p.description or "")


def merge_postings(group: list[JobPosting]) -> JobPosting:
    """One posting for a job seen on several boards.

    The richest record wins; fields it lacks are filled from the others (richest first), the
    longest description is kept, tags are combined, and every board's URL is kept in
    `source_urls`. The job URL stays on a link-back board when one is in the group, and the
    location comes from a board that posted the job directly rather than from an aggregator.
    """
    ranked = sorted(group, key=_richness, reverse=True)
    best = ranked[0]
    if len(group) == 1:
        return best if best.source_urls else replace(best, source_urls={best.source: best.url})
    updates: dict[str, Any] = {}
    for name in ("company", "location", "posted_at", "employment_type", "seniority", "applied_count",
                 "view_count", "timezone_note", "posting_language", "remote"):  # fmt: skip
        if getattr(best, name) is None:
            donor = next((getattr(p, name) for p in ranked[1:] if getattr(p, name) is not None), None)
            if donor is not None:
                updates[name] = donor
    if best.salary_max is None and best.salary_min is None:
        donor = next((p for p in ranked[1:] if p.salary_max or p.salary_min), None)
        if donor:
            updates.update(salary_min=donor.salary_min, salary_max=donor.salary_max,
                           salary_currency=donor.salary_currency, salary_period=donor.salary_period,
                           salary_text=donor.salary_text)  # fmt: skip
    direct = next((p for p in ranked if p.source not in AGGREGATOR_SOURCES and p.location), None)
    if best.source in AGGREGATOR_SOURCES and direct and direct.location != best.location:
        updates.update(location=direct.location, remote=direct.remote if direct.remote is not None else best.remote)
    for name in ("regions", "countries"):
        if not getattr(best, name):
            donor = next((getattr(p, name) for p in ranked[1:] if getattr(p, name)), [])
            if donor:
                updates[name] = donor
    longest = max(group, key=lambda p: len(p.description or ""))
    if len(longest.description or "") > len(best.description or ""):
        updates["description"] = longest.description
    updates["tags"] = list(dict.fromkeys(t for p in ranked for t in p.tags))
    urls: dict[str, str] = {}
    for p in ranked:
        for source, url in (p.source_urls or {p.source: p.url}).items():
            urls.setdefault(source, url)
    updates["source_urls"] = urls
    link_back = next((p for p in ranked if p.source in LINK_BACK_SOURCES), None)
    if best.source not in LINK_BACK_SOURCES and link_back:
        updates["url"] = link_back.url
    updates["extra"] = {**{k: v for p in reversed(ranked) for k, v in p.extra.items()}, "merged_from": sorted(urls)}
    return replace(best, **updates)


def dedupe_across_sources(postings: list[JobPosting]) -> list[JobPosting]:
    """Collapse the same job seen on several boards into one merged posting (see merge_postings).

    Two postings are the same job when their canonical URLs match (FreeHire links Djinni and We
    Work Remotely jobs to the board's own URL), or when title + company match and they come from
    different sources (one board listing a title twice is usually two openings, e.g. two cities).
    """
    parent = list(range(len(postings)))
    sources: list[set[str]] = [{p.source} for p in postings]

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int, *, same_source_ok: bool) -> None:
        a, b = find(i), find(j)
        if a == b or (not same_source_ok and sources[a] & sources[b]):
            return
        parent[b] = a
        sources[a] |= sources[b]

    first_by_url: dict[str, int] = {}
    first_by_title: dict[str, list[int]] = {}
    for i, p in enumerate(postings):
        for url in {p.url, *p.source_urls.values()}:
            if url:
                key = canonical_url(url)
                if key in first_by_url:
                    union(first_by_url[key], i, same_source_ok=True)
                else:
                    first_by_url[key] = i
    for i, p in enumerate(postings):
        for key in _title_keys(p):
            for j in first_by_title.setdefault(key, []):
                union(j, i, same_source_ok=False)
            first_by_title[key].append(i)

    groups: dict[int, list[JobPosting]] = {}
    for i, p in enumerate(postings):
        groups.setdefault(find(i), []).append(p)
    return [merge_postings(group) for group in groups.values()]


@dataclass
class ScoredJob:
    posting: JobPosting
    extraction: Extraction
    match: MatchResult
    hire: HireResult = field(default_factory=lambda: HireResult(score=0))

    @property
    def score(self) -> int:
        return self.match.score

    @property
    def rank(self) -> int:
        """Geometric mean of fit and hire probability (worker.hire.rank_score)."""
        return rank_score(self.match.score, self.hire.score)


@dataclass
class DiscoveryReport:
    jobs: list[ScoredJob] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    fetched: int = 0
    per_source: dict[str, int] = field(default_factory=dict)  # source label -> postings fetched
    source_seconds: dict[str, float] = field(default_factory=dict)  # source label -> fetch time
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    full_descriptions: FullTextStats = field(default_factory=FullTextStats)


def enrich(posting: JobPosting, extraction: Extraction) -> JobPosting:
    """Fill gaps in the posting with extracted facts, never overwrite what the source said."""
    f = extraction.fields
    updates: dict[str, Any] = {}
    if not posting.location and f.location_restriction:
        updates["location"] = f.location_restriction
    if posting.remote is None and f.remote is not None:
        updates["remote"] = f.remote
    if posting.salary_max is None and f.salary.max:
        updates.update(
            salary_min=f.salary.min,
            salary_max=f.salary.max,
            salary_currency=f.salary.currency,
            salary_period=f.salary.period,
        )
    return replace(posting, **updates) if updates else posting


def analyze(
    posting: JobPosting,
    profile: CandidateProfile,
    extractor: Extractor,
    *,
    now: datetime | None = None,
) -> ScoredJob:
    rules = HeuristicExtractor()
    extraction = rules.extract(posting)
    if isinstance(extractor, LLMExtractor):
        prelim = score_job(posting, profile, seniority=extraction.fields.seniority, now=now)
        if prelim.score >= LLM_PRESCORE_THRESHOLD:
            extraction = extractor.extract(posting)
    elif not isinstance(extractor, HeuristicExtractor):
        extraction = extractor.extract(posting)

    return score_posting(posting, extraction, profile, now=now)


def score_posting(
    posting: JobPosting,
    extraction: Extraction,
    profile: CandidateProfile,
    *,
    now: datetime | None = None,
) -> ScoredJob:
    """Fit and hire probability of one posting for one profile, from facts already extracted.

    Cheap (rules only, no I/O), so the same extraction can be scored for every user's profile.
    """
    enriched = enrich(posting, extraction)
    # A level the source states (FreeHire, Jobicy) beats one guessed from the title.
    seniority = _fit_level(enriched.seniority) or extraction.fields.seniority
    match = score_job(
        enriched,
        profile,
        seniority=seniority,
        risk_flags=list(extraction.fields.risk_flags),
        now=now,
    )
    eligible, location_reason = location_eligibility(enriched, profile)
    hire = estimate_hire_probability(
        enriched, profile, eligible=eligible, location_reason=location_reason, seniority=seniority, now=now
    )
    return ScoredJob(posting=enriched, extraction=extraction, match=match, hire=hire)


def _fit_level(level: str | None) -> str | None:
    """Source seniority in matching's vocabulary ("director" counts as "lead" there)."""
    return "lead" if level == "director" else level


def discover(
    source_configs: list[dict[str, Any]],
    profile: CandidateProfile,
    extractor: Extractor,
    *,
    http: HttpClient | None = None,
    now: datetime | None = None,
    full_text: FullTextOptions | None = None,
) -> DiscoveryReport:
    """Run every source; one failing source is reported, not fatal.

    With `full_text`, short descriptions from excerpt-only sources are replaced by the posting
    page's text (one budget for the whole run).
    """
    report = DiscoveryReport()
    collected: list[JobPosting] = []
    owns_client = http is None
    http = http or HttpClient()
    try:
        for config in source_configs:
            label = config.get("name") or config.get("type", "?")
            started = time.monotonic()
            try:
                postings = fetch_postings(config, http=http)
            except (SourceError, KeyError) as exc:
                logger.warning("source %s failed: %s", label, exc)
                report.errors[label] = str(exc)
                continue
            finally:
                report.source_seconds[label] = round(time.monotonic() - started, 1)
            report.fetched += len(postings)
            report.per_source[label] = report.per_source.get(label, 0) + len(postings)
            collected.extend(postings)
        unique = dedupe_across_sources(collected)
        if full_text:
            unique, report.full_descriptions = fetch_full_descriptions(unique, http, full_text)
    finally:
        if owns_client:
            http.close()

    for posting in unique:
        scored = analyze(posting, profile, extractor, now=now)
        if scored.extraction.method == "llm":
            report.llm_calls += 1
            report.input_tokens += scored.extraction.input_tokens
            report.output_tokens += scored.extraction.output_tokens
        report.jobs.append(scored)
    report.jobs.sort(key=lambda j: (j.rank, j.score), reverse=True)
    return report
