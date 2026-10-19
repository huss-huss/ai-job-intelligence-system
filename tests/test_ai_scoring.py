"""LLM-read facts feed fit and hire probability: who may apply, employment type, timezone, years, risks."""

from datetime import UTC, datetime
from pathlib import Path

from worker.ai import ExtractedFields, Extraction, HeuristicExtractor
from worker.hire import AI_ELIGIBLE, RESTRICTED
from worker.pipeline import score_posting
from worker.profile import load_profile
from worker.sources.base import JobPosting

ROOT = Path(__file__).resolve().parent.parent
PROFILE = load_profile(ROOT / "profile.example.yaml")
NOW = datetime(2026, 10, 19, tzinfo=UTC)
DESC = "We build FastAPI and PostgreSQL services in Python with Docker. " * 12


def job(**kw) -> JobPosting:
    base = dict(source="t", external_id="1", url="https://jobs.example.com/1", title="Senior Backend Engineer",
                location="Remote", remote=True, description=DESC, posted_at=NOW)  # fmt: skip
    return JobPosting(**{**base, **kw})


def llm(**fields) -> Extraction:
    rules = HeuristicExtractor().extract(job()).fields.model_dump()
    return Extraction(ExtractedFields.model_validate({**rules, **fields}), "llm")


def test_ai_reads_a_us_only_restriction_the_rules_could_not_see():
    rules = score_posting(job(), HeuristicExtractor().extract(job()), PROFILE, now=NOW)
    assert rules.match.eligible is None
    ai = score_posting(job(), llm(location_restriction="United States"), PROFILE, now=NOW)
    assert ai.match.eligible is False
    assert "AI read: applicants must be in the United States" in ai.match.reasons
    assert ai.hire.factors["eligibility"] == RESTRICTED
    assert any("AI read: applicants must be in the United States" in r for r in ai.hire.reasons)
    assert ai.match.score <= 20 < rules.match.score


def test_ai_reads_worldwide_and_your_region():
    worldwide = score_posting(job(), llm(worldwide=True), PROFILE, now=NOW)
    assert worldwide.match.eligible is True and "AI read: open to applicants worldwide" in worldwide.match.reasons
    assert worldwide.hire.factors["eligibility"] == AI_ELIGIBLE
    africa = score_posting(job(), llm(location_restriction="Africa, Europe"), PROFILE, now=NOW)
    assert africa.match.eligible is True and "AI read: open to applicants in Africa, Europe" in africa.match.reasons


def test_ai_never_reopens_a_job_the_rules_found_restricted():
    restricted = job(location="USA only")
    scored = score_posting(restricted, llm(worldwide=True, location_restriction=None), PROFILE, now=NOW)
    assert scored.match.eligible is False
    assert not any(r.startswith("AI read") for r in scored.match.reasons)


def test_ai_can_catch_a_restriction_behind_a_rules_yes():
    scored = score_posting(job(location="Worldwide"), llm(location_restriction="US"), PROFILE, now=NOW)
    assert scored.match.eligible is False and "AI read: applicants must be in the US" in scored.match.reasons


def test_rules_extraction_never_gets_ai_reasons():
    rules = Extraction(ExtractedFields(location_restriction="United States", worldwide=True), "heuristic")
    scored = score_posting(job(), rules, PROFILE, now=NOW)
    assert scored.match.eligible is None and not any(r.startswith("AI read") for r in scored.match.reasons)


def test_ai_employment_type_timezone_and_years_feed_hire_probability():
    plain = score_posting(job(), llm(employment_type=None), PROFILE, now=NOW)
    ai = score_posting(job(), llm(employment_type="contract", timezone_requirement="PST"), PROFILE, now=NOW)
    assert plain.hire.factors["employment"] == 0.8 and ai.hire.factors["employment"] == 0.6  # contract weight
    assert plain.hire.factors["timezone"] == 1.0 and ai.hire.factors["timezone"] < 0.3  # PST vs UTC+3
    assert ai.posting.employment_type == "contract" and ai.posting.timezone_note == "PST"
    source_says = job(employment_type="full_time", timezone_note="CET")
    kept = score_posting(source_says, llm(employment_type="contract", timezone_requirement="PST"), PROFILE, now=NOW)
    assert (kept.posting.employment_type, kept.posting.timezone_note) == ("full_time", "CET")  # the source wins
    five = PROFILE.model_copy(update={"years_experience": 5})
    senior = score_posting(job(), llm(years_experience_min=10), five, now=NOW)
    assert senior.hire.factors["seniority"] == 0.25 and "asks for 10+ years" in " ".join(senior.hire.reasons)


def test_risk_flags_gate_hire_probability():
    clean = score_posting(job(), llm(risk_flags=[]), PROFILE, now=NOW)
    scam = score_posting(job(), llm(risk_flags=["requires_upfront_payment", "vague_description"]), PROFILE, now=NOW)
    assert clean.hire.factors["risk"] == 1.0 and scam.hire.factors["risk"] == 0.1
    assert scam.hire.score < clean.hire.score * 0.2 and "Risk: asks you to pay" in scam.hire.reasons
    vague = score_posting(job(), llm(risk_flags=["vague_description"]), PROFILE, now=NOW)
    assert vague.hire.factors["risk"] == 1.0  # only costs fit points
