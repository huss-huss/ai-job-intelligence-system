import json

import httpx
import pytest

from tests.llm_fakes import FakeOpenAI, ai_settings
from worker import llm
from worker.ai import ExtractedFields, HeuristicExtractor, LLMExtractor, _strict_schema, build_extractor
from worker.budget import BudgetGuard, MemoryUsageStore
from worker.llm import LLMCaller, LLMConfig
from worker.pipeline import DiscoveryReport, count_llm
from worker.sources.base import JobPosting


@pytest.fixture(autouse=True)
def _fresh_learning():
    llm.forget_learned()

LONG_DESC = (
    "We are hiring a senior engineer to build FastAPI services backed by PostgreSQL and Redis. "
    "You will design APIs, run Celery workers, and integrate LLM features with OpenAI. "
    "Salary is $120k - $150k. Fully remote, worldwide. " * 2
)


def posting(**kw) -> JobPosting:
    base = dict(source="t", external_id="1", url="https://x/1", title="Senior Backend Engineer", description=LONG_DESC)
    return JobPosting(**{**base, **kw})


def test_heuristic_extracts_skills_seniority_salary():
    result = HeuristicExtractor().extract(posting(salary_text="$120k - $150k"))
    f = result.fields
    assert result.method == "heuristic"
    assert f.seniority == "senior"
    assert {"fastapi", "postgresql", "redis", "celery", "llm", "openai"} <= set(f.skills)
    assert (f.salary.min, f.salary.max) == (120000, 150000)
    assert f.risk_flags == []


def test_heuristic_reads_currency_and_period_from_salary_text():
    f = HeuristicExtractor().extract(posting(salary_text="€5,000 per month")).fields
    assert f.salary.model_dump() == {"min": 5000, "max": 5000, "currency": "EUR", "period": "month"}


def test_heuristic_keeps_source_salary_period():
    f = HeuristicExtractor().extract(posting(salary_max=40, salary_currency="USD", salary_period="hour")).fields
    assert (f.salary.max, f.salary.currency, f.salary.period) == (40, "USD", "hour")


def test_heuristic_flags_risky_and_vague_postings():
    flags = HeuristicExtractor().extract(posting(description="Commission-only sales role.")).fields.risk_flags
    assert "commission_only" in flags and "vague_description" in flags


def test_heuristic_does_not_match_skill_substrings():
    p = posting(title="Engineer", description="Gopher mascot. Django " + LONG_DESC)
    f = HeuristicExtractor().extract(p).fields
    assert "go" not in f.skills and "django" in f.skills


def extractor(fake: FakeOpenAI, store=None, **guard_kw) -> LLMExtractor:
    guard = BudgetGuard(store or MemoryUsageStore(), **guard_kw)
    return LLMExtractor(LLMCaller(fake.client(), LLMConfig(model="gpt-6-luna"), guard))


def test_llm_extractor_uses_strict_schema_and_records_usage():
    fake = FakeOpenAI(usage={"prompt": 321, "completion_tokens": 45, "reasoning": 12})
    result = extractor(fake).extract(posting())
    assert result.method == "llm" and result.fallback_reason is None
    assert result.fields.role_family == "backend" and result.fields.employment_type == "full_time"
    assert (result.input_tokens, result.output_tokens, result.reasoning_tokens) == (321, 45, 12)
    assert result.cost_usd == pytest.approx((321 * 0.10 + 45 * 0.50) / 1e6) and result.model == "gpt-6-luna"
    fmt = fake.requests[0]["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert "temperature" not in fake.requests[0]  # gpt-6-luna takes no temperature


def test_llm_extractor_falls_back_on_invalid_json_or_schema_with_a_warning(caplog):
    for content in ("not json", json.dumps({"seniority": "wizard"})):
        caplog.clear()
        result = extractor(FakeOpenAI(content=content)).extract(posting())
        assert result.method == "heuristic" and result.fallback_reason == "invalid_output"
        assert result.cost_usd > 0  # billed anyway, and counted
        assert "invalid output" in caplog.text and "using rules" in caplog.text


def test_llm_extractor_falls_back_on_api_error_with_a_warning(caplog):
    fake = FakeOpenAI(script=[httpx.Response(429, json={"error": {"message": "rate limited"}})])
    result = extractor(fake).extract(posting())
    assert result.method == "heuristic" and result.fallback_reason == "error"
    assert "LLM extraction failed" in caplog.text and "using rules" in caplog.text


def test_llm_extractor_falls_back_without_calling_when_the_budget_says_no(caplog):
    fake = FakeOpenAI()
    result = extractor(fake, monthly_budget_usd=0.0).extract(posting())
    assert result.method == "heuristic" and result.fallback_reason == "budget"
    assert fake.requests == [] and "monthly AI budget reached" in caplog.text


def test_fallbacks_are_counted_in_run_stats():
    stats: dict = {}
    report = DiscoveryReport()
    ok = extractor(FakeOpenAI()).extract(posting())
    bad = extractor(FakeOpenAI(content="nope")).extract(posting())
    broke = extractor(FakeOpenAI(script=[httpx.Response(500, json={})])).extract(posting())
    for e in (ok, bad, broke):
        count_llm(stats, e)
        count_llm(report, e)
    assert stats["llm_calls"] == 2 and stats["llm_fallbacks"] == {"invalid_output": 1, "error": 1}
    assert report.llm_calls == 2 and report.llm_fallbacks == {"invalid_output": 1, "error": 1}
    assert stats["reasoning_tokens"] == 240 and stats["llm_cost_usd"] == pytest.approx(ok.cost_usd + bad.cost_usd)


def test_strict_schema_requires_every_property_recursively():
    schema = _strict_schema(ExtractedFields.model_json_schema())
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(ExtractedFields.model_fields)
    salary = schema["$defs"]["Salary"]
    assert salary["additionalProperties"] is False and set(salary["required"]) == {"min", "max", "currency", "period"}


def test_build_extractor_without_key_uses_rules():
    assert isinstance(build_extractor(ai_settings(openai_api_key="")), HeuristicExtractor)
    assert isinstance(build_extractor(ai_settings(), client=FakeOpenAI().client()), LLMExtractor)
