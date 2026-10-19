import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from worker.matching import INELIGIBLE_CAP, detect_seniority, location_eligibility, recency_date, score_job
from worker.profile import CandidateProfile, load_profile
from worker.salary import FX_ENV_VAR
from worker.sources.adapters import GreenhouseAdapter, LeverAdapter
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


def test_recent_update_beats_old_first_published(profile):
    """A role still being maintained scores as fresh; the reason says it was updated, not posted."""
    live = job(posted_at=NOW - timedelta(days=251), updated_at=NOW - timedelta(days=3))
    result = score_job(live, profile, now=NOW)
    assert result.breakdown["recency"] == 5
    assert "Updated 3 days ago, first posted 251 days ago" in result.reasons
    assert not any(r.startswith("Posted") for r in result.reasons)


def test_old_posting_without_update_says_posted(profile):
    result = score_job(job(posted_at=NOW - timedelta(days=60)), profile, now=NOW)
    assert result.breakdown["recency"] == 0
    assert "Posted 60 days ago" in result.reasons


def test_same_day_update_counts_as_posted(profile):
    """Greenhouse sets updated_at a few hours after publishing; that's not a separate update."""
    posted = NOW - timedelta(days=2, hours=5)
    result = score_job(job(posted_at=posted, updated_at=posted + timedelta(hours=4)), profile, now=NOW)
    assert recency_date(job(posted_at=posted, updated_at=posted + timedelta(hours=4))) == (posted, False)
    assert result.breakdown["recency"] == 5
    assert not any(r.startswith(("Updated", "Posted")) for r in result.reasons)


def test_greenhouse_fixture_recency_uses_updated_at(profile):
    """Fixture job: first_published 2024-12-20, updated_at 2026-08-21, so it's live, not 600 days old."""
    raw = json.loads((ROOT / "tests/fixtures/greenhouse.json").read_text())["jobs"][0]
    # The fixture's sales title is in the example profile's exclude list, which skips scoring.
    posting = replace(GreenhouseAdapter.parse(raw), title="Senior Software Engineer")
    now = posting.updated_at + timedelta(days=3, hours=1)
    result = score_job(posting, profile, now=now)
    assert result.breakdown["recency"] == 5
    assert any(r.startswith("Updated 3 days ago, first posted 6") for r in result.reasons)
    stale = score_job(replace(posting, updated_at=None), profile, now=now)  # what scoring did before
    assert stale.breakdown["recency"] == 0
    assert any(r.startswith("Posted 6") for r in stale.reasons)


def test_lever_recency_uses_created_at_only(profile):
    posting = LeverAdapter.parse(json.loads((ROOT / "tests/fixtures/lever.json").read_text())[0], "leverdemo")
    assert posting.updated_at is None
    assert recency_date(posting) == (posting.posted_at, False)


def test_usd_annual_salary_reason_has_no_conversion(profile):
    result = score_job(job(), profile, now=NOW)
    assert result.breakdown["salary"] == 10
    assert "Salary up to $120,000/yr" in result.reasons


def test_non_usd_salary_is_converted_and_reason_shows_both(profile, monkeypatch):
    monkeypatch.setenv(FX_ENV_VAR, '{"EUR": 1.1}')
    result = score_job(job(salary_min=None, salary_max=60000, salary_currency="EUR"), profile, now=NOW)
    assert result.breakdown["salary"] == 10
    assert "Salary up to €60,000/yr ≈ $66,000" in result.reasons


def test_hourly_salary_is_annualized_before_scoring(profile):
    # $40/hr would fail a $30k floor if read as annual; 40 × 2080 = $83,200 clears it.
    result = score_job(job(salary_min=None, salary_max=40, salary_period="hour"), profile, now=NOW)
    assert result.breakdown["salary"] == 10
    assert "Salary up to $40/hr ≈ $83,200" in result.reasons


def test_low_monthly_salary_falls_below_target(profile):
    result = score_job(job(salary_min=None, salary_max=2000, salary_period="month"), profile, now=NOW)
    assert result.breakdown["salary"] == 0  # 2,000 × 12 = 24,000 < 30,000
    assert "Salary below target ($2,000/mo ≈ $24,000)" in result.reasons


def test_unknown_currency_scores_as_unknown_with_reason(profile):
    result = score_job(job(salary_max=20000, salary_currency="XYZ"), profile, now=NOW)
    assert result.breakdown["salary"] == 5
    assert any("XYZ 20,000/yr not converted" in r for r in result.reasons)


def test_salary_points_scale_the_same_after_normalization(profile):
    annual = score_job(job(salary_min=None, salary_max=83200), profile, now=NOW)
    hourly = score_job(job(salary_min=None, salary_max=40, salary_period="hour"), profile, now=NOW)
    assert annual.score == hourly.score and annual.breakdown == hourly.breakdown


def test_risk_flags_subtract_points(profile):
    clean = score_job(job(), profile, now=NOW).score
    risky = score_job(job(), profile, risk_flags=["commission_only"], now=NOW).score
    assert risky == clean - 10


def test_profile_rejects_out_of_range_weights():
    with pytest.raises(ValueError):
        CandidateProfile(skills={"python": 5})
