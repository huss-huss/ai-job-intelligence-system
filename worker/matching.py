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
from worker.salary import format_salary, to_annual_usd
from worker.sources.base import JobPosting
from worker.timezone import find_timezone_windows

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
# "...in the US" that is about hours, not residence ("able to work in the US time zone").
_NOT_HOURS = r"(?!\s*(time|hours|timezone|tz|business hours|working hours|eastern|pacific|central))"
_US_UK = r"(us|u\.s\.|usa|united states|uk|united kingdom)"
_RESTRICTION_PATTERNS = [
    rf"must (be|reside|live) (based )?in (the )?({_US_UK[1:-1]}|eu|europe|canada)\b{_NOT_HOURS}",
    r"(us|u\.s\.) citizens? only",
    rf"(authori[sz]ed|eligible|legally able) to work in the {_US_UK}\b{_NOT_HOURS}",
    r"\b(require|requires|need|needs) (us|u\.s\.) work authori[sz]ation",
    r"\b(usa|us|uk|canada|eu) only\b",
    r"\b(us|u\.s\.)[- ]based (candidates|applicants|employees) only\b",
    r"\bacross all (us|u\.s\.) locations\b",  # a US-only pay range ("...for new hires across all US locations")
]
_DAYS = r"([1-5]|one|two|three|four|five)"
# The description asks for office days even when the job is tagged "Remote" (feeds often tag hybrid jobs
# that way): "working from the office four days per week", "3 days a week in our office", "hybrid policy".
_OFFICE_PATTERNS = [
    rf"\b(in|from|at) (the|our) office\b[^.]{{0,30}}?\b{_DAYS} days?\b",
    rf"\b{_DAYS} days? (a|per|each|every) week (in|at|from) (the|our|an|one of our) offices?\b",
    rf"\b{_DAYS}[- ]days?[- ](a[- ]week[- ])?in[- ](the[- ])?office\b",
    r"\bhybrid (work(ing)? )?(policy|schedule|model|arrangement|role|position|setup)\b",
    r"\bthis (is|will be) a hybrid\b",
    r"\b(on[- ]?site|in[- ]office) (role|position|only)\b",
]
_NEGATION = re.compile(r"\b(not|no|never|isn't|without)\b[^.]{0,20}$")


def _office_days_required(desc: str) -> bool:
    """True when the description asks for office or on-site days. "Not a hybrid role" doesn't count."""
    for pattern in _OFFICE_PATTERNS:
        for m in re.finditer(pattern, desc):
            if not _NEGATION.search(desc[max(0, m.start() - 30) : m.start()]):
                return True
    return False


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


def _timezone_only(location: str) -> bool:
    """True for locations that only state a timezone ("Time zone: CET (+/- 3 hours)", "US time zones only").

    Those say when you work, not where you may live, so they leave eligibility unknown; the timezone
    itself is scored by worker.hire.
    """
    windows = find_timezone_windows(location, short_field=True)
    if not windows:
        return False
    rest = location
    for window in windows:
        rest = rest.replace(window.text, " ")
    rest = re.sub(r"(?i)time\s*zones?|timezones?|\bhours?\b|\bremote\b|\bonly\b|[^a-z]", " ", rest)
    return not rest.strip()


def location_eligibility(posting: JobPosting, profile: CandidateProfile) -> tuple[bool | None, str]:
    """True/False when we can tell, None when the posting doesn't say."""
    prefs = profile.location
    if posting.remote is False and not prefs.accepts_onsite:
        return False, f"On-site/hybrid in {posting.location or 'unspecified location'}"

    desc = (posting.description or "").lower()
    if not prefs.accepts_onsite and _office_days_required(desc):
        return False, "Hybrid/on-site: the description asks for office days"
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
    if _timezone_only(posting.location or ""):
        return None, f"Timezone requirement only ({posting.location})"
    return False, f"Restricted to {posting.location}"


def recency_date(posting: JobPosting) -> tuple[datetime | None, bool]:
    """The date recency is scored from, and whether it is the "updated" date.

    Greenhouse keeps first_published from when a requisition first went live, so a role that is
    still open looks months old; its updated_at moves while the role is maintained. An update less
    than a day after posting is the same event and still counts as "posted".
    """
    posted, updated = posting.posted_at, posting.updated_at
    if updated and (posted is None or (updated - posted).days >= 1):
        return updated, True
    return posted, False


def _days_ago(days: int) -> str:
    return "today" if days <= 0 else "1 day ago" if days == 1 else f"{days} days ago"


def _salary_points(posting: JobPosting, floor_usd: float | None) -> tuple[float, str | None]:
    top = posting.salary_max or posting.salary_min
    if not top:
        return WEIGHTS["salary"] / 2, None
    original = format_salary(top, posting.salary_currency, posting.salary_period)
    annual = to_annual_usd(posting.salary_min, posting.salary_max, posting.salary_currency, posting.salary_period)
    if annual is None:
        return WEIGHTS["salary"] / 2, f"Salary {original} not converted to USD (unknown currency or period)"
    # "$120,000/yr" needs no conversion note; "€60,000/yr ≈ $70,200" and "$40/hr ≈ $83,200" do.
    shown = original if original == format_salary(annual.top, "USD", "year") else f"{original} ≈ ${annual.top:,.0f}"
    if floor_usd is None:
        return WEIGHTS["salary"] / 2, None
    if annual.top >= floor_usd:
        return WEIGHTS["salary"], f"Salary up to {shown}"
    return 0, f"Salary below target ({shown})"


def score_job(
    posting: JobPosting,
    profile: CandidateProfile,
    *,
    seniority: str | None = None,
    risk_flags: list[str] | None = None,
    eligibility: tuple[bool | None, str] | None = None,
    now: datetime | None = None,
) -> MatchResult:
    """`eligibility` overrides location_eligibility (the LLM's reading, see pipeline.ai_eligibility)."""
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
    eligible, why = eligibility or location_eligibility(posting, profile)
    breakdown["location"] = {True: WEIGHTS["location"], None: WEIGHTS["location"] / 2, False: 0}[eligible]
    reasons.append(why)

    # Salary, normalized to annual USD. Unknown currencies or periods score as unknown, never guessed.
    breakdown["salary"], salary_reason = _salary_points(posting, profile.min_salary_usd)
    if salary_reason:
        reasons.append(salary_reason)

    # Recency, from the newer of "posted" and "updated" (see recency_date)
    seen_at, updated = recency_date(posting)
    if seen_at:
        now = now or datetime.now(UTC)
        age = (now - seen_at).days
        breakdown["recency"] = (
            WEIGHTS["recency"] if age <= 3 else 3 if age <= 14 else 1 if age <= profile.max_age_days else 0
        )
        if updated:
            first = f", first posted {_days_ago((now - posting.posted_at).days)}" if posting.posted_at else ""
            reasons.append(f"Updated {_days_ago(age)}{first}")
        elif age > profile.max_age_days:
            reasons.append(f"Posted {_days_ago(age)}")
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
