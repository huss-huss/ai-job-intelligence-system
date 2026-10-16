"""Discovery pipeline: fetch → extract facts → score. Shared by the RQ worker and the CLI."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from worker.ai import Extraction, Extractor, HeuristicExtractor, LLMExtractor
from worker.matching import MatchResult, score_job
from worker.profile import CandidateProfile
from worker.sources import HttpClient, JobPosting, SourceError, fetch_postings

logger = logging.getLogger(__name__)

# Only spend LLM tokens on jobs that already look promising from cheap rules.
LLM_PRESCORE_THRESHOLD = 35


_COMPANY_SUFFIXES = re.compile(r"\b(inc|llc|ltd|gmbh|corp|co|company|limited)\b\.?")


def cross_source_key(posting: JobPosting) -> str:
    """Same job reposted on several boards: normalized title + company."""
    company = _COMPANY_SUFFIXES.sub("", (posting.company or "").lower())
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # noqa: E731
    return f"{norm(posting.title)}|{norm(company)}"


def dedupe_across_sources(postings: list[JobPosting]) -> list[JobPosting]:
    """Keep one posting per cross-source key, preferring the most detailed description."""
    best: dict[str, JobPosting] = {}
    for p in postings:
        key = cross_source_key(p)
        if key not in best or len(p.description or "") > len(best[key].description or ""):
            best[key] = p
    return list(best.values())


@dataclass
class ScoredJob:
    posting: JobPosting
    extraction: Extraction
    match: MatchResult

    @property
    def score(self) -> int:
        return self.match.score


@dataclass
class DiscoveryReport:
    jobs: list[ScoredJob] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    fetched: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def enrich(posting: JobPosting, extraction: Extraction) -> JobPosting:
    """Fill gaps in the posting with extracted facts, never overwrite what the source said."""
    f = extraction.fields
    updates: dict[str, Any] = {}
    if not posting.location and f.location_restriction:
        updates["location"] = f.location_restriction
    if posting.remote is None and f.remote is not None:
        updates["remote"] = f.remote
    if posting.salary_max is None and f.salary.max and f.salary.period in (None, "year"):
        updates.update(salary_min=f.salary.min, salary_max=f.salary.max, salary_currency=f.salary.currency)
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

    enriched = enrich(posting, extraction)
    match = score_job(
        enriched,
        profile,
        seniority=extraction.fields.seniority,
        risk_flags=list(extraction.fields.risk_flags),
        now=now,
    )
    return ScoredJob(posting=enriched, extraction=extraction, match=match)


def discover(
    source_configs: list[dict[str, Any]],
    profile: CandidateProfile,
    extractor: Extractor,
    *,
    http: HttpClient | None = None,
    now: datetime | None = None,
) -> DiscoveryReport:
    """Run every source; one failing source is reported, not fatal."""
    report = DiscoveryReport()
    collected: list[JobPosting] = []
    owns_client = http is None
    http = http or HttpClient()
    try:
        for config in source_configs:
            label = config.get("name") or config.get("type", "?")
            try:
                postings = fetch_postings(config, http=http)
            except (SourceError, KeyError) as exc:
                logger.warning("source %s failed: %s", label, exc)
                report.errors[label] = str(exc)
                continue
            report.fetched += len(postings)
            collected.extend(postings)
    finally:
        if owns_client:
            http.close()

    for posting in dedupe_across_sources(collected):
        scored = analyze(posting, profile, extractor, now=now)
        if scored.extraction.method == "llm":
            report.llm_calls += 1
            report.input_tokens += scored.extraction.input_tokens
            report.output_tokens += scored.extraction.output_tokens
        report.jobs.append(scored)
    report.jobs.sort(key=lambda j: j.score, reverse=True)
    return report
