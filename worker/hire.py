"""Hire probability: how realistic is it that *you* get this job, 0-100, with reasons.

The fit score (worker.matching) answers "is this the right job?". This answers "can I get it?":
a perfect-fit role that only hires in the US, or that 400 people applied to last month, is a
poor use of an application. It is a relative index for ranking, not a calibrated probability.

Model (every number below is a named constant, mirrored in frontend/components/ScoreDetails.tsx):

    hire_probability = 100 × eligibility × timezone × language × risk × soft
    soft             = Σ SOFT_WEIGHTS[f] × factor[f] / Σ SOFT_WEIGHTS     (competition, freshness,
                                                                         seniority, employment)

Gates (eligibility, timezone, language) multiply, because each can make an application pointless
on its own. Soft factors average, because they trade off against each other. Each factor is 0-1.

`rank_score` combines fit and hire probability with a geometric mean, so a job needs both to
rank high: fit 90 / hire 10 ranks 30, fit 60 / hire 60 ranks 60.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from worker.profile import CandidateProfile
from worker.sources.base import JobPosting
from worker.sources.normalize import SENIORITY_LEVELS, normalize_employment_type
from worker.timezone import find_timezone_windows

SOFT_WEIGHTS: dict[str, int] = {"competition": 30, "freshness": 30, "seniority": 20, "employment": 20}
GATES = ("eligibility", "timezone", "language", "risk")

# Eligibility certainty
REGION_CERTAINTY: dict[str, float] = {
    "worldwide": 1.0, "anywhere": 1.0, "global": 1.0, "africa": 1.0,
    "emea": 0.9,
    "mena": 0.75,  # Ethiopia is usually, not always, left out of "MENA"
}  # fmt: skip
COMPANY_HIRES_IN_REGION = 0.85  # posting silent, but the company hires remotely in your region (FreeHire)
OTHER_ELIGIBLE = 0.9  # matched one of the profile's own eligible_terms
UNKNOWN_ELIGIBILITY = 0.5  # bare "Remote"
_REGION_LABELS = {"worldwide": "applicants worldwide", "anywhere": "applicants worldwide",
                  "global": "applicants worldwide", "africa": "Africa", "emea": "EMEA", "mena": "MENA"}
RESTRICTED = 0.02  # not open to your location: effectively zero
AI_ELIGIBLE = 0.9  # the LLM read an explicit "open to <your region>" / "worldwide" the rules couldn't see

# Risk flags (rules or LLM): the lowest value among the posting's flags. vague_description only costs fit.
RISK_GATE: dict[str, float] = {
    "not_a_job": 0.1, "requires_upfront_payment": 0.1, "unpaid": 0.3, "commission_only": 0.5,
}  # fmt: skip
_RISK_LABELS = {"not_a_job": "doesn't look like a real job", "requires_upfront_payment": "asks you to pay",
                "unpaid": "unpaid", "commission_only": "commission only"}  # fmt: skip

# Timezone: hours between your offset and the nearest accepted offset -> factor
TIMEZONE_CURVE: list[tuple[float, float]] = [(0, 1.0), (2, 0.75), (4, 0.45), (6, 0.25)]
TIMEZONE_FAR = 0.1

# Language
LOCAL_LANGUAGE_REQUIRED = 0.1
NON_ENGLISH_POSTING = 0.35

# Competition: pressure = applicants + views / VIEWS_PER_APPLICANT
VIEWS_PER_APPLICANT = 20  # roughly 1 in 20 viewers applies on job boards
COMPETITION_FLOOR = 0.1
UNKNOWN_COMPETITION = 0.5

# Freshness: full credit for FRESH_HOURS, then halves every HALF_LIFE_DAYS
FRESH_HOURS = 48
HALF_LIFE_DAYS = 5
FRESHNESS_FLOOR = 0.05
UNKNOWN_FRESHNESS = 0.4

# Employment type
UNKNOWN_EMPLOYMENT = 0.8
UNLISTED_EMPLOYMENT = 0.1

# Seniority: levels away from the nearest level in your profile -> factor
SENIORITY_DISTANCE = {0: 1.0, 1: 0.55, 2: 0.25}
SENIORITY_FAR = 0.1
UNKNOWN_SENIORITY = 0.7

LANGUAGE_NAMES: dict[str, str] = {
    "en": "english", "de": "german", "fr": "french", "es": "spanish", "pt": "portuguese", "nl": "dutch",
    "it": "italian", "pl": "polish", "uk": "ukrainian", "ru": "russian", "ja": "japanese", "zh": "chinese",
    "ko": "korean", "tr": "turkish", "ar": "arabic", "he": "hebrew", "sv": "swedish", "da": "danish",
    "no": "norwegian", "fi": "finnish", "cs": "czech", "ro": "romanian", "hu": "hungarian", "el": "greek",
    "am": "amharic",
}  # fmt: skip
_STOPWORDS: dict[str, set[str]] = {
    "en": {"the", "and", "to", "of", "you", "we", "with", "for", "our", "are", "will", "is"},
    "de": {"und", "der", "die", "das", "wir", "mit", "für", "sie", "ist", "ein", "eine", "zu"},
    "fr": {"et", "le", "la", "les", "des", "nous", "vous", "pour", "une", "est", "avec", "dans"},
    "es": {"y", "el", "la", "los", "las", "de", "para", "con", "una", "que", "en", "es"},
    "pt": {"e", "o", "os", "as", "de", "para", "com", "uma", "que", "em", "você", "nós"},
    "nl": {"en", "de", "het", "van", "een", "wij", "jij", "voor", "met", "zijn", "je", "is"},
    "it": {"e", "il", "la", "di", "per", "con", "una", "che", "sono", "nel", "gli", "noi"},
    "pl": {"i", "w", "z", "na", "do", "się", "jest", "oraz", "dla", "nie", "jak", "że"},
}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_PLUS = re.compile(r"\b(plus|nice to have|bonus|advantage|preferred|asset|desirable)\b")
_LANG_ALT = "|".join(sorted({n for n in LANGUAGE_NAMES.values() if n != "english"}, key=len, reverse=True))
_REQUIRES_LANGUAGE = [
    re.compile(
        r"\b(fluent|fluency|native|proficient|proficiency|business[- ]level|bilingual|speak|speaking"
        rf"|written and spoken|working knowledge of)\b[^.\n;]{{0,30}}?\b({_LANG_ALT})\b"
    ),
    re.compile(
        rf"\b({_LANG_ALT})\b(?: language)?[^.\n;]{{0,25}}?"
        r"\b(required|is a must|a must|mandatory|fluency|fluent|native|speaker|c1|c2|b2)\b"
    ),
    re.compile(rf"\b({_LANG_ALT})[- ]speaking\b"),
]
_EMPLOYMENT_PHRASE = re.compile(
    r"\b(full[- ]time|part[- ]time|contract|freelance)\s+(position|role|job|opportunity|basis|engagement)\b"
    r"|\b(this is an?|for an?|as an?)\s+(full[- ]time|part[- ]time|contract|freelance)\b"
)


@dataclass
class HireResult:
    score: int
    factors: dict[str, float] = field(default_factory=dict)  # every factor 0-1, gates and soft
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"score": self.score, "factors": self.factors, "reasons": self.reasons}


def rank_score(fit: int, hire: int) -> int:
    """Geometric mean of fit and hire probability (both 0-100): high only when both are."""
    return round(100 * math.sqrt(max(fit, 0) / 100 * max(hire, 0) / 100))


def _contains(text: str, term: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(term)}(?![\w])", text) is not None


def _utc(offset: float) -> str:
    return "UTC" if offset == 0 else f"UTC{offset:+g}"


def _ago(hours: float) -> str:
    if hours < 1:
        return "under an hour ago"
    if hours < 48:
        return f"{int(hours)} h ago"
    return f"{int(hours // 24)} days ago"


# --- gates --------------------------------------------------------------------------------------


def eligibility_factor(
    posting: JobPosting,
    profile: CandidateProfile,
    eligible: bool | None,
    location_reason: str,
    *,
    ai_read: bool = False,
) -> tuple[float, str]:
    """How sure we are that you may apply from your country (see REGION_CERTAINTY).

    `ai_read`: the verdict and reason come from the LLM's reading of the posting (pipeline.ai_eligibility).
    """
    if eligible is False:
        return RESTRICTED, f"Not open to your location ({location_reason})"
    if ai_read and eligible:
        return AI_ELIGIBLE, location_reason
    loc = (posting.location or "").lower()
    prefs = profile.location
    if _contains(loc, prefs.country.lower()) or prefs.country_code.lower() in posting.countries:
        return 1.0, f"Explicitly open to {prefs.country}"
    candidates = [(v, t) for t, v in REGION_CERTAINTY.items() if _contains(loc, t)]
    for region in posting.regions:
        # A source region counts as an eligibility statement only when no narrower country list comes with it.
        if region == "global" or (region in REGION_CERTAINTY and not posting.countries):
            candidates.append((REGION_CERTAINTY[region], region))
    if candidates:
        value, term = max(candidates)
        return value, f"Open to {_REGION_LABELS[term]}"
    company_regions = posting.extra.get("company_hires_remote_in") or []
    if "africa" in company_regions:
        return COMPANY_HIRES_IN_REGION, "Company hires remotely in Africa (FreeHire)"
    if eligible:
        return OTHER_ELIGIBLE, f"Open to your location ({posting.location})"
    return UNKNOWN_ELIGIBILITY, "Remote, but who can apply isn't stated"


def timezone_factor(posting: JobPosting, profile: CandidateProfile) -> tuple[float, str | None]:
    """Overlap between your UTC offset and the posting's stated timezone requirement, if any."""
    windows = [
        *find_timezone_windows(posting.location, short_field=True),
        *find_timezone_windows(posting.timezone_note, short_field=True),
        *find_timezone_windows(posting.description),
    ]
    if not windows:
        return 1.0, None
    offset = profile.utc_offset
    best = min(windows, key=lambda w: w.gap(offset))
    gap = best.gap(offset)
    value = next((v for limit, v in TIMEZONE_CURVE if gap <= limit), TIMEZONE_FAR)
    if gap == 0:
        return value, f"Timezone requirement '{best.describe()}' fits {_utc(offset)}"
    return value, f"Timezone requirement '{best.describe()}' is {gap:g} h from {_utc(offset)}"


def detect_language(text: str | None) -> str | None:
    """Best-effort ISO 639-1 code of a posting's language from script and stopwords; None if unsure."""
    words = _WORD.findall((text or "")[:4000].lower())
    if len(words) < 20:
        return None
    cyrillic = sum(1 for w in words if re.match(r"[Ѐ-ӿ]", w))
    if cyrillic / len(words) > 0.3:
        return "uk" if any(ch in "іїєґ" for w in words for ch in w) else "ru"
    counts = {code: sum(1 for w in words if w in stop) for code, stop in _STOPWORDS.items()}
    top = max(counts, key=lambda c: counts[c])
    if top != "en" and counts[top] >= 5 and counts[top] > 1.5 * counts["en"]:
        return top
    return "en" if counts["en"] >= 3 else None


def required_languages(text: str | None) -> set[str]:
    """Non-English languages the posting requires ("fluent German", "French is a must")."""
    lowered = (text or "").lower()
    found: set[str] = set()
    for pattern in _REQUIRES_LANGUAGE:
        for m in pattern.finditer(lowered):
            name = next(g for g in m.groups() if g and g in LANGUAGE_NAMES.values())
            tail = lowered[m.end() : m.end() + 40]
            if not _PLUS.search(m.group()) and not _PLUS.search(tail):
                found.add(name)
    return found


def language_factor(posting: JobPosting, profile: CandidateProfile) -> tuple[float, str | None]:
    spoken = set(profile.languages)
    required = required_languages(f"{posting.title}\n{posting.description or ''}") - spoken
    if required:
        names = ", ".join(sorted(n.title() for n in required))
        return LOCAL_LANGUAGE_REQUIRED, f"Requires {names}"
    code = (posting.posting_language or detect_language(posting.description) or "").lower()
    name = LANGUAGE_NAMES.get(code)
    if code and code != "en" and name not in spoken:
        return NON_ENGLISH_POSTING, f"Posting is in {name.title() if name else code}"
    return 1.0, None


def risk_factor(risk_flags: list[str] | None) -> tuple[float, str | None]:
    flags = [f for f in (risk_flags or []) if f in RISK_GATE]
    if not flags:
        return 1.0, None
    worst = min(flags, key=lambda f: RISK_GATE[f])
    return RISK_GATE[worst], f"Risk: {_RISK_LABELS[worst]}"


# --- soft factors -------------------------------------------------------------------------------


def competition_factor(posting: JobPosting, profile: CandidateProfile) -> tuple[float, str | None]:
    """1 / (1 + 4 × pressure / max_competition): 0.5 at a quarter of max_competition, 0.2 at it."""
    applied, views = posting.applied_count, posting.view_count
    if applied is None and views is None:
        return UNKNOWN_COMPETITION, None
    pressure = (applied or 0) + (views or 0) / VIEWS_PER_APPLICANT
    value = max(COMPETITION_FLOOR, 1 / (1 + 4 * pressure / profile.max_competition))
    counts = ", ".join(
        part for part in (
            f"{applied} applied" if applied is not None else "",
            f"{views} views" if views is not None else "",
        ) if part
    )  # fmt: skip
    level = "low" if value >= 0.7 else "moderate" if value >= 0.4 else "high"
    return value, f"Competition {level}: {counts}"


def freshness_factor(posting: JobPosting, now: datetime) -> tuple[float, str | None]:
    """Full credit in the first FRESH_HOURS, then halves every HALF_LIFE_DAYS (early applicants get read)."""
    if posting.posted_at is None:
        return UNKNOWN_FRESHNESS, None
    hours = max((now - posting.posted_at).total_seconds() / 3600, 0)
    if hours <= FRESH_HOURS:
        return 1.0, f"Posted {_ago(hours)}: early applicant"
    value = max(FRESHNESS_FLOOR, 0.5 ** ((hours - FRESH_HOURS) / (24 * HALF_LIFE_DAYS)))
    return value, f"Posted {_ago(hours)}"


def infer_employment_type(posting: JobPosting) -> str | None:
    """The source's employment type, else one stated in the title, else a clear phrase in the description."""
    if posting.employment_type:
        return posting.employment_type
    from_title = normalize_employment_type(posting.title)
    if from_title:
        return from_title
    m = _EMPLOYMENT_PHRASE.search((posting.description or "").lower())
    return normalize_employment_type(m.group()) if m else None


def employment_factor(posting: JobPosting, profile: CandidateProfile) -> tuple[float, str | None]:
    kind = infer_employment_type(posting)
    if kind is None:
        return UNKNOWN_EMPLOYMENT, None
    label = kind.replace("_", "-").capitalize()
    if kind not in profile.employment_types:
        return UNLISTED_EMPLOYMENT, f"{label}: not one of your employment types"
    value = profile.employment_types[kind]
    if value >= max(profile.employment_types.values()):
        return value, label
    return value, f"{label} (weighted {value:g} in your profile)"


def seniority_factor(
    posting: JobPosting, profile: CandidateProfile, seniority: str | None
) -> tuple[float, str | None]:
    level = posting.seniority or seniority
    if level not in SENIORITY_LEVELS or not profile.seniority:
        return UNKNOWN_SENIORITY, None
    wanted = [SENIORITY_LEVELS.index(s) for s in profile.seniority if s in SENIORITY_LEVELS]
    distance = min(abs(SENIORITY_LEVELS.index(level) - w) for w in wanted) if wanted else 0
    value = SENIORITY_DISTANCE.get(distance, SENIORITY_FAR)
    reason = f"Seniority fits ({level})" if distance == 0 else f"Seniority {level}: {distance} level(s) off"
    years = posting.extra.get("experience_years_min")
    if profile.years_experience is not None and isinstance(years, int) and years > profile.years_experience:
        value *= 0.5 if years - profile.years_experience <= 2 else 0.25
        reason += f"; asks for {years}+ years (you have {profile.years_experience})"
    return value, reason


# --- combine ------------------------------------------------------------------------------------


def estimate_hire_probability(
    posting: JobPosting,
    profile: CandidateProfile,
    *,
    eligible: bool | None,
    location_reason: str,
    seniority: str | None = None,
    risk_flags: list[str] | None = None,
    ai_eligibility: bool = False,
    now: datetime | None = None,
) -> HireResult:
    """`eligible`/`location_reason` come from matching.location_eligibility (or the LLM's reading of the
    posting, `ai_eligibility`), so both scores agree."""
    now = now or datetime.now(UTC)
    results: dict[str, tuple[float, str | None]] = {
        "eligibility": eligibility_factor(posting, profile, eligible, location_reason, ai_read=ai_eligibility),
        "timezone": timezone_factor(posting, profile),
        "language": language_factor(posting, profile),
        "risk": risk_factor(risk_flags),
        "competition": competition_factor(posting, profile),
        "freshness": freshness_factor(posting, now),
        "seniority": seniority_factor(posting, profile, seniority),
        "employment": employment_factor(posting, profile),
    }
    gates = math.prod(results[g][0] for g in GATES)
    soft = sum(SOFT_WEIGHTS[f] * results[f][0] for f in SOFT_WEIGHTS) / sum(SOFT_WEIGHTS.values())
    return HireResult(
        score=max(0, min(100, round(100 * gates * soft))),
        factors={name: round(value, 2) for name, (value, _) in results.items()},
        reasons=[reason for _, reason in results.values() if reason],
    )
