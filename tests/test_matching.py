from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from worker.matching import INELIGIBLE_CAP, detect_seniority, location_eligibility, score_job
from worker.profile import CandidateProfile, load_profile
from worker.sources.base import JobPosting

NOW = datetime(2026, 10, 16, tzinfo=UTC)
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def profile() -> CandidateProfile:
    return load_profile(ROOT / "profile.example.yaml")


def job(**overrides) -> JobPosting:
    base = dict(
        source="test",
        external_id="1",
        url="https://example.com/1",
        title="Senior Backend Engineer (Python)",
        company="Acme",
        location="Worldwide",
        description="We build FastAPI services on PostgreSQL and Redis, with Celery workers and LLM agents.",
        posted_at=NOW - timedelta(days=1),
        remote=True,
        salary_min=90000,
        salary_max=120000,
    )
    return JobPosting(**{**base, **overrides})


def test_strong_match_scores_high_with_reasons(profile):
    result = score_job(job(), profile, now=NOW)
    assert result.score >= 85
    assert result.eligible is True
    assert {"python", "fastapi", "postgres", "redis", "celery"} <= set(result.matched_skills)
    assert any("Title matches" in r for r in result.reasons)
    assert sum(result.breakdown.values()) >= result.score - 1


def test_country_restricted_job_is_capped(profile):
    result = score_job(job(location="USA Only"), profile, now=NOW)
    assert result.eligible is False
    assert result.score <= INELIGIBLE_CAP
    assert any("Restricted to USA Only" in r for r in result.reasons)


def test_restriction_in_description_overrides_worldwide_location(profile):
    p = job(description=job().description + " Candidates must be authorized to work in the US.")
    assert score_job(p, profile, now=NOW).eligible is False


def test_onsite_job_ineligible_unless_profile_accepts_onsite(profile):
    p = job(remote=False, location="Berlin")
    assert location_eligibility(p, profile)[0] is False
    profile.location.accepts_onsite = True
    assert location_eligibility(p, profile)[0] is False  # still Berlin, not an eligible region
    assert location_eligibility(job(remote=False, location="Addis Ababa, Ethiopia"), profile)[0] is True


@pytest.mark.parametrize(
    "location,expected",
    [
        ("Worldwide", True),
        ("Europe, Africa", True),
        ("EMEA", True),
        ("USA, Canada, USA timezones", False),
        ("Remote", None),
        (None, None),
    ],
)
def test_location_parsing(profile, location, expected):
    assert location_eligibility(job(location=location), profile)[0] is expected


def test_excluded_title_scores_zero(profile):
    result = score_job(job(title="Account Executive, AI Native"), profile, now=NOW)
    assert result.score == 0
    assert "Excluded" in result.reasons[0]


def test_skill_aliases_and_word_boundaries(profile):
    p = job(title="Engineer", description="Experience with PostgreSQL and Node.js. Javascripting is fun.")
    matched = score_job(p, profile, now=NOW).matched_skills
    assert "postgres" in matched and "node" in matched
    # "ts" must not match inside "Javascripting"; typescript is not mentioned
    assert "typescript" not in matched


def test_seniority_detection():
    assert detect_seniority("Sr. Python Developer") == "senior"
    assert detect_seniority("Staff Backend Engineer") == "lead"
    assert detect_seniority("Junior Data Engineer") == "junior"
    assert detect_seniority("Backend Engineer") is None


def test_seniority_mismatch_and_llm_override(profile):
    junior = score_job(job(title="Junior Backend Engineer"), profile, now=NOW)
    assert junior.breakdown["seniority"] == 0
    overridden = score_job(job(title="Backend Engineer"), profile, seniority="senior", now=NOW)
    assert overridden.breakdown["seniority"] == 10


def test_low_salary_and_old_posting_cost_points(profile):
    fresh = score_job(job(), profile, now=NOW)
    stale = score_job(job(salary_min=10000, salary_max=15000, posted_at=NOW - timedelta(days=60)), profile, now=NOW)
    assert stale.score < fresh.score
    assert stale.breakdown["salary"] == 0 and stale.breakdown["recency"] == 0


def test_non_usd_salary_is_treated_as_unknown(profile):
    result = score_job(job(salary_max=20000, salary_currency="EUR"), profile, now=NOW)
    assert result.breakdown["salary"] == 5


def test_risk_flags_subtract_points(profile):
    clean = score_job(job(), profile, now=NOW).score
    risky = score_job(job(), profile, risk_flags=["commission_only"], now=NOW).score
    assert risky == clean - 10


def test_profile_rejects_out_of_range_weights():
    with pytest.raises(ValueError):
        CandidateProfile(skills={"python": 5})
