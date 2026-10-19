"""`python -m worker.cli llm-eval`: rules vs LLM extraction on recent real jobs, side by side.

Picks recent stored jobs, a mix of eligible / not open to you / unknown by the stored (rules)
verdict, extracts each with rules and with the LLM, scores both against the deployment profile and
prints where they disagree on eligibility, seniority, employment type and risk flags, with the tokens
and cost. `--dry-run` only prints the estimated cost; nothing is sent. Usage is recorded as feature
"eval": it counts toward the monthly budget but not toward the daily extraction cap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from worker.ai import EXTRACT_MAX_OUTPUT_TOKENS, RESPONSE_FORMAT, Extraction, HeuristicExtractor, extraction_messages
from worker.hire import infer_employment_type
from worker.llm import estimate_input_tokens
from worker.pipeline import score_posting
from worker.profile import CandidateProfile
from worker.sources.base import JobPosting

MAX_JOBS = 50
TYPICAL_OUTPUT_TOKENS = 350  # JSON plus low-effort reasoning, for the "typical" estimate
FIELDS = ("eligibility", "seniority", "employment", "risk_flags")
_ELIG = {True: "yes", False: "no", None: "?"}


def pick_jobs(session, n: int, *, scan: int = 600) -> list:
    """Up to `n` recent analyzed jobs, alternating eligible / restricted / unknown so all three are covered."""
    from app.models import JobAnalysis, JobRaw
    from sqlalchemy import select

    rows = session.execute(
        select(JobRaw, JobAnalysis)
        .join(JobAnalysis, JobAnalysis.job_id == JobRaw.id)
        .order_by(JobRaw.created_at.desc(), JobRaw.id.desc())
        .limit(scan)
    ).all()
    buckets: dict[object, list] = {True: [], False: [], None: []}
    for job, analysis in rows:
        eligible = ((analysis.extracted_json or {}).get("match") or {}).get("eligible")
        buckets.get(eligible, buckets[None]).append(job)
    picked: list = []
    while len(picked) < n and any(buckets.values()):
        for key in (True, False, None):
            if buckets[key] and len(picked) < n:
                picked.append(buckets[key].pop(0))
    return picked


@dataclass
class Comparison:
    label: str
    rules: dict[str, str]
    llm: dict[str, str]
    extraction: Extraction

    @property
    def disagreements(self) -> list[str]:
        return [f for f in FIELDS if self.rules[f] != self.llm[f]]


def facts(posting: JobPosting, extraction: Extraction, profile: CandidateProfile, now: datetime) -> dict[str, str]:
    scored = score_posting(posting, extraction, profile, now=now)
    level = posting.seniority or extraction.fields.seniority
    return {
        "eligibility": _ELIG[scored.match.eligible],
        "seniority": level or "?",
        "employment": infer_employment_type(scored.posting) or "?",
        "risk_flags": ",".join(sorted(extraction.fields.risk_flags)) or "-",
    }


def compare(
    label: str, posting: JobPosting, llm: Extraction, profile: CandidateProfile, now: datetime
) -> Comparison:
    rules = HeuristicExtractor().extract(posting)
    return Comparison(label, facts(posting, rules, profile, now), facts(posting, llm, profile, now), llm)


@dataclass
class Estimate:
    jobs: int = 0
    input_tokens: int = 0
    worst_usd: float = 0.0
    typical_usd: float = 0.0
    per_job: list[float] = field(default_factory=list)


def estimate(postings: list[JobPosting], price, max_chars: int = 6000) -> Estimate:
    out = Estimate(jobs=len(postings))
    for p in postings:
        tokens = estimate_input_tokens(extraction_messages(p, max_chars), RESPONSE_FORMAT)
        out.input_tokens += tokens
        worst = price.cost(tokens, EXTRACT_MAX_OUTPUT_TOKENS)
        out.per_job.append(worst)
        out.worst_usd += worst
        out.typical_usd += price.cost(tokens, TYPICAL_OUTPUT_TOKENS)
    return out


def print_estimate(est: Estimate, model: str, budget_left: float) -> None:
    print(f"{est.jobs} jobs · model {model} · ~{est.input_tokens} input tokens (over-estimated)")
    print(
        f"estimated cost: typical ${est.typical_usd:.4f}, worst case ${est.worst_usd:.4f}"
        f" (every reply using the full {EXTRACT_MAX_OUTPUT_TOKENS}-token cap)"
    )
    print(f"budget left this month: ${budget_left:.4f}")


def print_table(rows: list[Comparison]) -> None:
    disagreeing = [r for r in rows if r.disagreements]
    print(f"\n{'Job':42}  {'Field':11}  {'Rules':24}  LLM")
    print("-" * 110)
    for r in disagreeing:
        for i, f in enumerate(r.disagreements):
            print(f"{(r.label if i == 0 else '')[:42]:42}  {f:11}  {r.rules[f][:24]:24}  {r.llm[f][:28]}")
    print("-" * 110)
    counts = {f: sum(1 for r in rows if f in r.disagreements) for f in FIELDS}
    print(
        f"{len(disagreeing)} of {len(rows)} jobs differ · "
        + " · ".join(f"{name} {count}" for name, count in counts.items())
    )
    fell_back = [r for r in rows if r.extraction.method != "llm"]
    if fell_back:
        reasons = sorted({r.extraction.fallback_reason or "?" for r in fell_back})
        print(f"{len(fell_back)} LLM extraction(s) fell back to rules ({', '.join(reasons)})")
    tokens_in = sum(r.extraction.input_tokens for r in rows)
    tokens_out = sum(r.extraction.output_tokens for r in rows)
    reasoning = sum(r.extraction.reasoning_tokens for r in rows)
    cost = sum(r.extraction.cost_usd for r in rows)
    print(f"tokens: {tokens_in} in · {tokens_out} out ({reasoning} reasoning) · cost ${cost:.5f}")


def now_utc() -> datetime:
    return datetime.now(UTC)
