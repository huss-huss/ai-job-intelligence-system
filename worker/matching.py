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


# --------------------------------------------------------------------------- who may apply (description)
# Sentences like "candidates must reside in South America, Central America or Mexico" or "Role open and
# available for Latam and Europe remote ONLY" restrict a job tagged "Remote" to a few regions. The places
# a sentence names are compared with the profile's eligible terms, so "must be based in EMEA" stays open
# to someone in Ethiopia while "Latam and Europe only" does not. A place followed by "time zone"/"hours"
# is about working hours, not residence, and is ignored.
_PLACE_WORDS = [
    "european union", "europe", "european", "united kingdom", "great britain", "britain", "united states",
    "north america", "south america", "central america", "latin america", "latam", "the americas",
    "americas", "canada", "mexico", "brazil", "argentina", "colombia", "chile", "peru", "uruguay",
    "apac", "asia-pacific", "asia pacific", "southeast asia", "asia", "india", "pakistan", "philippines",
    "vietnam", "australia", "new zealand", "ukraine", "poland", "germany", "israel", "turkey",
    "middle east", "mena", "emea", "africa", "worldwide", "anywhere", "global", "globally",
]
# Upper-case only, so "join us" or "contact us" never reads as the United States.
_PLACE_ABBREVIATIONS = r"(?-i:U\.S\.A\.?|U\.S\.|USA|US|U\.K\.|UK|E\.U\.|EU|EEA|LATAM|APAC|EMEA)"
_PLACE_WORDS_ALT = "|".join(re.escape(w) for w in sorted(_PLACE_WORDS, key=len, reverse=True))
_PLACE = rf"(?:{_PLACE_WORDS_ALT}|{_PLACE_ABBREVIATIONS})"
_PLACE_RE = re.compile(rf"(?<![\w.]){_PLACE}(?![\w])", re.IGNORECASE)
_HOURS_AFTER = re.compile(
    r"[\s-]*(?:\(|\))?\s*(?:time\s*zones?|timezones?|business\s+hours|working\s+hours|hours|tz|overlap)\b",
    re.IGNORECASE,
)
_WHO_MAY_APPLY = re.compile(
    r"\b(?:"
    r"(?:must|should|need\s+to|needs\s+to|required\s+to|have\s+to|has\s+to)\s+(?:be\s+)?"
    r"(?:reside|residing|live|living|located|based|resident|a\s+resident)(?:\s+(?:in|within|of))?"
    r"|(?:candidates?|applicants?|contractors?|talent)\s+(?:who\s+(?:are|live|reside)\s+|(?:must|need\s+to|should)\s+be\s+)?"
    r"(?:based|located|residing|living|from)(?:\s+(?:in|within))?"
    r"|(?:open|available|restricted|limited|hiring|accepting\s+applications)\s+(?:only\s+)?(?:to|for|in|from)"
    r"|(?:only|exclusively)\s+(?:from|in|for|within)"
    r"|remote\s+(?:only\s+)?(?:in|within)"
    r")\b",
    re.IGNORECASE,
)
_ONLY = re.compile(r"\b(?:only|exclusively)\b", re.IGNORECASE)
# A list of places right before "only": "Latam and Europe remote ONLY", "EU/UK only".
_PLACE_LIST_END = re.compile(
    rf"{_PLACE}(?:\s*(?:,|/|&|\+|\band\b|\bor\b)\s*(?:the\s+)?{_PLACE})*\s*(?:-\s*)?(?:remote\s+|based\s+)?$",
    re.IGNORECASE,
)
_RESTRICTION_NEGATION = re.compile(
    r"\b(?:not|no|never|without|don't|do\s+not|doesn't|does\s+not|needn't|isn't|aren't)\b[^.]{0,25}$",
    re.IGNORECASE,
)
# Sentence end; a dot after a single capital ("U.S.", "U.K.") doesn't end the sentence.
_SENTENCE_END = re.compile(r"(?<![A-Z])\.(?:\s|$)|[;!?\n]")


def _places(text: str) -> list[str]:
    """Places named in `text`, minus those followed by "time zone"/"hours"."""
    found = []
    for m in _PLACE_RE.finditer(text):
        if not _HOURS_AFTER.match(text, m.end()):
            found.append(m.group(0))
    return found


def _place_open(place: str, eligible_terms: list[str]) -> bool:
    lowered = place.lower().replace(".", "")
    return any(lowered == term.lower() or _location_matches(lowered, term.lower()) for term in eligible_terms)


def _restriction_verdict(places: list[str], eligible_terms: list[str]) -> str | None:
    """The restricted places when none of them is open to the profile, else None."""
    if not places or any(_place_open(p, eligible_terms) for p in places):
        return None
    return ", ".join(dict.fromkeys(places))


def description_region_restriction(description: str | None, eligible_terms: list[str]) -> str | None:
    """Regions a "who may apply" sentence limits the job to, when none of them is open to the profile.

    None when the description has no such sentence, names no recognizable place, or names a place the
    profile is open to ("must be based in EMEA" for someone in Ethiopia).
    """
    text = description or ""
    for m in _WHO_MAY_APPLY.finditer(text):
        if _RESTRICTION_NEGATION.search(text[max(0, m.start() - 40) : m.start()]):
            continue
        tail = text[m.end() : m.end() + 160]
        end = _SENTENCE_END.search(tail)
        verdict = _restriction_verdict(_places(tail[: end.start()] if end else tail), eligible_terms)
        if verdict:
            return verdict
    for m in _ONLY.finditer(text):
        head = text[max(0, m.start() - 120) : m.start()]
        start = max((e.end() for e in _SENTENCE_END.finditer(head)), default=0)
        listed = _PLACE_LIST_END.search(head[start:])
        if listed and not _RESTRICTION_NEGATION.search(head[start : start + listed.start()]):
            verdict = _restriction_verdict(_places(listed.group(0)), eligible_terms)
            if verdict:
                return verdict
    return None


@dataclass
class MatchResult:
    score: int
    eligible: bool | None
    breakdown: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    matched_skills: list[str] = field(default_factory=list)
    # Hard requirements this candidate can't meet (a language, many more years): kept out of Telegram.
    blockers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "eligible": self.eligible,
            "breakdown": self.breakdown,
            "reasons": self.reasons,
            "matched_skills": self.matched_skills,
            "blockers": self.blockers,
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


# Countries whose names contain a region word: "Johannesburg, South Africa" is one country, not "Africa".
_REGION_LOOKALIKES = ("south africa", "central african republic")
# "Anywhere in the US" / "global within Canada" name one place; only "... in the world" stays open.
_SCOPED_OPEN = re.compile(
    r"\b(anywhere|worldwide|global(?:ly)?)\s+(?:in|within|across|from)\s+(?:the\s+)?([a-z][a-z .]*)"
)


def _location_matches(part: str, term: str) -> bool:
    """True when an eligible term (a region, country or "worldwide") really opens this location part."""
    text = part
    for name in _REGION_LOOKALIKES:
        if name != term:
            text = re.sub(rf"(?<![\w]){re.escape(name)}(?![\w])", " ", text)

    def scope(m: re.Match) -> str:
        place = m.group(2).strip()
        return m.group(0) if place in ("world", "world.") else f" {place} "

    return _contains(_SCOPED_OPEN.sub(scope, text), term)


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
    limited_to = description_region_restriction(posting.description, prefs.eligible_terms)
    if limited_to:
        return False, f"Description limits applicants to {limited_to}"

    loc = (posting.location or "").strip().lower()
    if not loc:
        return None, "Location not specified"
    parts = [p.strip() for p in re.split(r"[,;/|]| or ", loc) if p.strip()]
    if all(p in ("remote", "remote job", "fully remote") for p in parts):
        return None, "Remote, but eligible regions not specified"
    for part in parts:
        if any(_location_matches(part, term) for term in prefs.eligible_terms):
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
