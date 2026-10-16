"""Explainable job-to-profile scoring.

Every point in the 0-100 score comes from a named component, and every component
adds a human-readable reason, so a match can be audited ("why is this 82?") without
re-running anything. The LLM, when configured, only supplies extracted facts
(seniority, salary, risk flags); the scoring itself stays deterministic and testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from worker.profile import CandidateProfile
from worker.sources.base import JobPosting

WEIGHTS = {"role": 25, "skills": 35, "seniority": 10, "location": 15, "salary": 10, "recency": 5}
INELIGIBLE_CAP = 20
RISK_PENALTY = 10

_SENIORITY_PATTERNS = [
    ("intern", r"\bintern(ship)?\b"),
    ("junior", r"\b(junior|jr\.?|entry[- ]level|graduate)\b"),
    ("lead", r"\b(lead|staff|principal|head of|architect)\b"),
    ("senior", r"\b(senior|sr\.?)\b"),
    ("mid", r"\b(mid[- ]level|intermediate)\b"),
]
_RESTRICTION_PATTERNS = [
    r"must (be|reside|live) (based )?in the (us|u\.s\.|united states|uk|eu)",
    r"(us|u\.s\.) citizens? only",
    r"authori[sz]ed to work in the (us|u\.s\.|united states|uk)",
    r"\b(usa|us|uk|canada|eu) only\b",
]


@dataclass
class MatchResult:
    score: int
    eligible: bool | None
    breakdown: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    matched_skills: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "eligible": self.eligible,
            "breakdown": self.breakdown,
            "reasons": self.reasons,
            "matched_skills": self.matched_skills,
        }


def _contains(text: str, term: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(term.lower())}(?![\w])", text) is not None


def detect_seniority(title: str) -> str | None:
    t = title.lower()
    for level, pattern in _SENIORITY_PATTERNS:
        if re.search(pattern, t):
            return level
    return None


def location_eligibility(posting: JobPosting, profile: CandidateProfile) -> tuple[bool | None, str]:
    """True/False when we can tell, None when the posting doesn't say."""
    prefs = profile.location
    if posting.remote is False and not prefs.accepts_onsite:
        return False, f"On-site/hybrid in {posting.location or 'unspecified location'}"

    desc = (posting.description or "").lower()
    for pattern in _RESTRICTION_PATTERNS:
        if re.search(pattern, desc):
            return False, "Description restricts applicants by country"

    loc = (posting.location or "").strip().lower()
    if not loc:
        return None, "Location not specified"
    parts = [p.strip() for p in re.split(r"[,;/|]| or ", loc) if p.strip()]
    if all(p in ("remote", "remote job", "fully remote") for p in parts):
        return None, "Remote, but eligible regions not specified"
    for part in parts:
        if any(_contains(part, term) for term in prefs.eligible_terms):
            return True, f"Open to applicants in {prefs.country} ({posting.location})"
    return False, f"Restricted to {posting.location}"


def score_job(
    posting: JobPosting,
    profile: CandidateProfile,
    *,
    seniority: str | None = None,
    risk_flags: list[str] | None = None,
    now: datetime | None = None,
) -> MatchResult:
    title = posting.title.lower()
    body = f"{title}\n{' '.join(posting.tags).lower()}\n{(posting.description or '').lower()}"
    reasons: list[str] = []
    breakdown: dict[str, float] = {}

    excluded = next((t for t in profile.exclude_titles if _contains(title, t)), None)
    if excluded:
        return MatchResult(0, None, {}, [f"Excluded title keyword: {excluded!r}"])

    # Role relevance
    title_hit = next((t for t in profile.target_titles if _contains(title, t)), None)
    if title_hit:
        breakdown["role"] = WEIGHTS["role"]
        reasons.append(f"Title matches target role {title_hit!r}")
    elif any(_contains(body, t) for t in profile.target_titles):
        breakdown["role"] = WEIGHTS["role"] * 0.2
        reasons.append("Target role mentioned in description, not title")
    else:
        breakdown["role"] = 0

    # Skills: weighted coverage of the profile's top skills
    matched = [s for s in profile.skills if any(_contains(body, a) for a in profile.aliases_for(s))]
    if profile.skills:
        top_weights = sorted(profile.skills.values(), reverse=True)[:8]
        coverage = sum(profile.skills[s] for s in matched) / sum(top_weights)
        # Skills listed in an off-target role's description are weak evidence of fit.
        relevance = 1.0 if title_hit else 0.5
        breakdown["skills"] = round(WEIGHTS["skills"] * min(coverage, 1.0) * relevance, 1)
        if matched:
            reasons.append(f"Skills: {', '.join(sorted(matched, key=lambda s: -profile.skills[s]))}")
    else:
        breakdown["skills"] = 0

    # Seniority
    level = seniority or detect_seniority(posting.title)
    if level is None:
        breakdown["seniority"] = WEIGHTS["seniority"] / 2
    elif level in profile.seniority:
        breakdown["seniority"] = WEIGHTS["seniority"]
        reasons.append(f"Seniority fits ({level})")
    else:
        breakdown["seniority"] = 0
        reasons.append(f"Seniority mismatch ({level})")

    # Location
    eligible, why = location_eligibility(posting, profile)
    breakdown["location"] = {True: WEIGHTS["location"], None: WEIGHTS["location"] / 2, False: 0}[eligible]
    reasons.append(why)

    # Salary (annual USD; non-USD salaries are treated as unknown rather than guessed)
    top = posting.salary_max or posting.salary_min
    usd = posting.salary_currency in (None, "USD")
    if profile.min_salary_usd is None or not top or not usd:
        breakdown["salary"] = WEIGHTS["salary"] / 2
    elif top >= profile.min_salary_usd:
        breakdown["salary"] = WEIGHTS["salary"]
        reasons.append(f"Salary up to ${top:,.0f}")
    else:
        breakdown["salary"] = 0
        reasons.append(f"Salary below target (${top:,.0f})")

    # Recency
    if posting.posted_at:
        age = ((now or datetime.now(UTC)) - posting.posted_at).days
        breakdown["recency"] = (
            WEIGHTS["recency"] if age <= 3 else 3 if age <= 14 else 1 if age <= profile.max_age_days else 0
        )
        if age > profile.max_age_days:
            reasons.append(f"Posted {age} days ago")
    else:
        breakdown["recency"] = 1

    total = sum(breakdown.values())
    for flag in risk_flags or []:
        total -= RISK_PENALTY
        reasons.append(f"Risk: {flag}")
    if eligible is False:
        total = min(total, INELIGIBLE_CAP)

    return MatchResult(
        score=max(0, min(100, round(total))),
        eligible=eligible,
        breakdown=breakdown,
        reasons=reasons,
        matched_skills=matched,
    )
