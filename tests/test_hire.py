"""Hire probability: each factor, the combination, the ranking, and the timezone parser."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from worker import hire
from worker.ai import HeuristicExtractor
from worker.hire import (
    competition_factor,
    detect_language,
    eligibility_factor,
    employment_factor,
    estimate_hire_probability,
    freshness_factor,
    language_factor,
    rank_score,
    required_languages,
    seniority_factor,
    timezone_factor,
)
from worker.matching import location_eligibility
from worker.pipeline import analyze
from worker.profile import CandidateProfile, load_profile, parse_utc_offset
from worker.sources.base import JobPosting
from worker.timezone import find_timezone_windows

ROOT = Path(__file__).resolve().parent.parent
PROFILE = load_profile(ROOT / "profile.example.yaml")
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)

BASE = JobPosting(
    source="t",
    external_id="1",
    url="https://jobs.example.com/1",
    title="Senior Backend Engineer",
    company="Acme",
    location="Worldwide",
    remote=True,
    description="We build Python and FastAPI services with PostgreSQL. You will work with the team on APIs. " * 4,
    posted_at=NOW - timedelta(hours=5),
)


def job(**changes) -> JobPosting:
    return replace(BASE, **changes)


def eligibility(p: JobPosting, profile: CandidateProfile = PROFILE) -> tuple[float, str]:
    eligible, why = location_eligibility(p, profile)
    return eligibility_factor(p, profile, eligible, why)


# --- eligibility --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "expected"),
    [("Worldwide", 1.0), ("Anywhere in the World", 1.0), ("Ethiopia", 1.0), ("Africa", 1.0), ("EMEA", 0.9),
     ("MENA", 0.75), ("Remote", hire.UNKNOWN_ELIGIBILITY), (None, hire.UNKNOWN_ELIGIBILITY),
     ("USA only", hire.RESTRICTED), ("Germany", hire.RESTRICTED)],
)  # fmt: skip
def test_eligibility_certainty_by_location(location, expected):
    assert eligibility(job(location=location))[0] == expected


def test_eligibility_reads_source_regions_and_countries():
    assert eligibility(job(location=None, regions=["global"], countries=["us", "br"]))[0] == 1.0
    assert eligibility(job(location=None, regions=["africa"]))[0] == 1.0
    assert eligibility(job(location=None, countries=["et"]))[0] == 1.0
    # "africa" with a country list is where the posting is, not who may apply
    assert eligibility(job(location=None, regions=["africa"], countries=["ng"]))[0] == hire.UNKNOWN_ELIGIBILITY


def test_company_hiring_in_africa_lifts_unknown_but_not_restricted():
    signal = {"company_hires_remote_in": ["africa"]}
    value, reason = eligibility(job(location="Remote", extra=signal))
    assert value == hire.COMPANY_HIRES_IN_REGION and "Africa" in reason
    assert eligibility(job(location="USA only", extra=signal))[0] == hire.RESTRICTED


def test_restricted_job_has_near_zero_hire_probability():
    p = job(location="United States")
    eligible, why = location_eligibility(p, PROFILE)
    result = estimate_hire_probability(p, PROFILE, eligible=eligible, location_reason=why, now=NOW)
    assert result.score <= 2 and result.reasons[0].startswith("Not open to your location")


# --- timezone -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "short", "gap"),
    [("Time zone: CET (+/- 3 hours)", True, 0), ("US time zones only", True, 7), ("CET ±2h", True, 0),
     ("You must work EST hours.", False, 7), ("Candidates between UTC-3 and UTC+3 are welcome.", False, 0),
     ("Remote within European time zones.", False, 0), ("Must overlap 4 hours with EST.", False, 3),
     ("Remote (UTC+2 to UTC+4)", True, 0), ("Work in Pacific time zone.", False, 10)],
)  # fmt: skip
def test_timezone_windows(text, short, gap):
    windows = find_timezone_windows(text, short_field=short)
    assert windows and min(w.gap(3) for w in windows) == gap


def test_zone_named_in_passing_is_not_a_requirement():
    assert find_timezone_windows("Our HQ is in PST and we ship weekly.") == []
    assert find_timezone_windows("Anywhere in the World", short_field=True) == []


@pytest.mark.parametrize(
    ("location", "note", "expected"),
    [("Time zone: CET (+/- 3 hours)", None, 1.0), ("US time zones only", None, hire.TIMEZONE_FAR),
     ("Worldwide", "Overlap with CET business hours", 1.0), ("Worldwide", None, 1.0)],
)  # fmt: skip
def test_timezone_factor(location, note, expected):
    value, reason = timezone_factor(job(location=location, timezone_note=note), PROFILE)
    assert value == expected
    assert (reason is None) == (location == "Worldwide" and note is None)


def test_timezone_follows_profile_offset():
    us_profile = PROFILE.model_copy(update={"timezone": "UTC-5"})
    assert timezone_factor(job(location="US time zones only"), us_profile)[0] == 1.0


def test_timezone_only_location_leaves_eligibility_unknown():
    eligible, why = location_eligibility(job(location="Time zone: CET (+/- 3 hours)"), PROFILE)
    assert eligible is None and why.startswith("Timezone requirement only")


# --- language -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "required"),
    [("Fluent German is required.", {"german"}), ("You speak French and English.", {"french"}),
     ("Spanish is a must.", {"spanish"}), ("Japanese-speaking support engineer", {"japanese"}),
     ("Fluent English; Spanish is a plus.", set()), ("You will serve the German market.", set()),
     ("Native English speaker preferred.", set())],
)  # fmt: skip
def test_required_languages(text, required):
    assert required_languages(text) == required


def test_language_factor_penalizes_local_language_and_non_english_postings():
    assert language_factor(job(description="Fluent Polish required. " + BASE.description), PROFILE)[0] == 0.1
    assert language_factor(job(posting_language="de"), PROFILE)[0] == hire.NON_ENGLISH_POSTING
    assert language_factor(job(), PROFILE) == (1.0, None)
    # a language you list is fine
    amharic = job(description="Amharic is required. " + BASE.description)
    assert language_factor(amharic, PROFILE)[0] == 1.0


def test_detect_language():
    assert detect_language(BASE.description) == "en"
    german = (
        "Wir suchen eine Entwicklerin und einen Entwickler für unser Team. Sie ist mit der Arbeit und die Zukunft " * 3
    )
    assert detect_language(german) == "de"
    assert detect_language("Шукаємо розробника Python у нашу команду, робота віддалена, гнучкий графік " * 3) == "uk"
    assert detect_language("short") is None


# --- competition --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("applied", "views", "expected"),
    [(0, 0, 1.0), (12, 10, 0.5), (25, 0, 0.33), (50, 0, 0.2), (500, 5000, hire.COMPETITION_FLOOR)],
)  # fmt: skip
def test_competition_curve(applied, views, expected):
    # pressure = applied + views/20; factor = 1 / (1 + 4 × pressure / max_competition), max_competition = 50
    value, reason = competition_factor(job(applied_count=applied, view_count=views), PROFILE)
    assert value == pytest.approx(expected, abs=0.01)
    assert f"{applied} applied" in reason


def test_competition_unknown_is_neutral():
    assert competition_factor(job(), PROFILE) == (hire.UNKNOWN_COMPETITION, None)
    stricter = PROFILE.model_copy(update={"max_competition": 10})
    assert competition_factor(job(applied_count=5), stricter)[0] < competition_factor(job(applied_count=5), PROFILE)[0]


# --- freshness ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(1, 1.0), (47, 1.0), (48 + 24 * 5, 0.5), (48 + 24 * 10, 0.25), (24 * 60, hire.FRESHNESS_FLOOR)],
)
def test_freshness_curve(hours, expected):
    value, _ = freshness_factor(job(posted_at=NOW - timedelta(hours=hours)), NOW)
    assert value == pytest.approx(expected, abs=0.01)


def test_freshness_unknown_date():
    assert freshness_factor(job(posted_at=None), NOW) == (hire.UNKNOWN_FRESHNESS, None)


# --- employment type ----------------------------------------------------------------------------


def test_employment_type_weights_come_from_profile():
    assert employment_factor(job(employment_type="full_time"), PROFILE) == (1.0, "Full-time")
    value, reason = employment_factor(job(employment_type="contract"), PROFILE)
    assert value == 0.6 and "weighted 0.6" in reason
    assert employment_factor(job(employment_type="part_time"), PROFILE)[0] == hire.UNLISTED_EMPLOYMENT
    assert employment_factor(job(), PROFILE) == (hire.UNKNOWN_EMPLOYMENT, None)
    contract_first = PROFILE.model_copy(update={"employment_types": {"contract": 1.0, "full_time": 0.5}})
    assert employment_factor(job(employment_type="contract"), contract_first)[0] == 1.0


def test_employment_type_inferred_from_title_and_description():
    assert employment_factor(job(title="Python Developer (Contract)"), PROFILE)[0] == 0.6
    described = job(description="This is a part-time role for a backend engineer. " + BASE.description)
    assert employment_factor(described, PROFILE)[0] == hire.UNLISTED_EMPLOYMENT


def test_profile_rejects_bad_employment_weights_and_timezones():
    with pytest.raises(ValueError):
        CandidateProfile(employment_types={"full_time": 2})
    with pytest.raises(ValueError):
        CandidateProfile(timezone="EAT")
    assert CandidateProfile(employment_types={"Full-Time": 1}).employment_types == {"full_time": 1.0}
    assert parse_utc_offset("UTC-05:30") == -5.5 and parse_utc_offset("UTC") == 0.0


# --- seniority ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "expected"),
    [("senior", 1.0), ("mid", 1.0), ("lead", 0.55), ("junior", 0.55), ("director", 0.25), ("intern", 0.25),
     (None, hire.UNKNOWN_SENIORITY)],
)  # fmt: skip
def test_seniority_distance(level, expected):
    assert seniority_factor(job(seniority=level), PROFILE, None)[0] == expected


def test_seniority_years_requirement():
    five = PROFILE.model_copy(update={"years_experience": 5})
    value, reason = seniority_factor(job(seniority="senior", extra={"experience_years_min": 8}), five, None)
    assert value == 0.25 and "8+ years" in reason
    assert seniority_factor(job(seniority="senior", extra={"experience_years_min": 4}), five, None)[0] == 1.0


# --- combination and ranking ----------------------------------------------------------------------


def test_hire_probability_combines_gates_and_weighted_factors():
    p = job(location="Remote", applied_count=0, view_count=0, employment_type="contract", seniority="senior")
    result = estimate_hire_probability(p, PROFILE, eligible=None, location_reason="", now=NOW)
    soft = (30 * 1.0 + 30 * 1.0 + 20 * 1.0 + 20 * 0.6) / 100
    assert result.score == round(100 * 0.5 * soft)
    assert set(result.factors) == {
        "eligibility",
        "timezone",
        "language",
        "competition",
        "freshness",
        "seniority",
        "employment",
    }
    assert result.to_dict()["factors"]["eligibility"] == 0.5


def test_ideal_posting_scores_high_and_us_timezone_drags_it_down():
    ideal = job(applied_count=0, view_count=10, employment_type="full_time", seniority="senior")
    best = estimate_hire_probability(ideal, PROFILE, eligible=True, location_reason="", now=NOW)
    us_hours = estimate_hire_probability(
        replace(ideal, description=BASE.description + " You must work US time zones."),
        PROFILE, eligible=True, location_reason="", now=NOW,
    )  # fmt: skip
    assert best.score >= 95 and us_hours.score <= 10


@pytest.mark.parametrize(("fit", "hire_p", "rank"), [(90, 10, 30), (60, 60, 60), (100, 100, 100), (80, 0, 0)])
def test_rank_is_geometric_mean(fit, hire_p, rank):
    assert rank_score(fit, hire_p) == rank


def test_analyze_attaches_hire_probability_and_rank():
    scored = analyze(job(applied_count=1, view_count=20), PROFILE, HeuristicExtractor(), now=NOW)
    assert 0 < scored.hire.score <= 100
    assert scored.rank == rank_score(scored.score, scored.hire.score)
    assert any(r.startswith("Posted") for r in scored.hire.reasons)


def test_analyze_uses_source_seniority_over_title():
    scored = analyze(job(title="Backend Engineer", seniority="director"), PROFILE, HeuristicExtractor(), now=NOW)
    assert scored.match.breakdown["seniority"] == 0  # "director" counts as "lead" for fit: not in [mid, senior]
    assert scored.hire.factors["seniority"] == 0.25
