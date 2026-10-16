import json
from types import SimpleNamespace

from worker.ai import ExtractedFields, HeuristicExtractor, LLMExtractor, _strict_schema, build_extractor
from worker.sources.base import JobPosting

LONG_DESC = (
    "We are hiring a senior engineer to build FastAPI services backed by PostgreSQL and Redis. "
    "You will design APIs, run Celery workers, and integrate LLM features with OpenAI. "
    "Salary is $120k - $150k. Fully remote, worldwide. " * 2
)


def posting(**kw) -> JobPosting:
    base = dict(source="t", external_id="1", url="https://x/1", title="Senior Backend Engineer", description=LONG_DESC)
    return JobPosting(**{**base, **kw})


class FakeCompletions:
    def __init__(self, content=None, error=None):
        self.content, self.error, self.calls = content, error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(prompt_tokens=321, completion_tokens=45),
        )


def fake_client(**kw):
    completions = FakeCompletions(**kw)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def test_heuristic_extracts_skills_seniority_salary():
    result = HeuristicExtractor().extract(posting(salary_text="$120k - $150k"))
    f = result.fields
    assert result.method == "heuristic"
    assert f.seniority == "senior"
    assert {"fastapi", "postgresql", "redis", "celery", "llm", "openai"} <= set(f.skills)
    assert (f.salary.min, f.salary.max) == (120000, 150000)
    assert f.risk_flags == []


def test_heuristic_flags_risky_and_vague_postings():
    flags = HeuristicExtractor().extract(posting(description="Commission-only sales role.")).fields.risk_flags
    assert "commission_only" in flags and "vague_description" in flags


def test_heuristic_does_not_match_skill_substrings():
    p = posting(title="Engineer", description="Gopher mascot. Django " + LONG_DESC)
    f = HeuristicExtractor().extract(p).fields
    assert "go" not in f.skills and "django" in f.skills


def test_llm_extractor_uses_strict_schema_and_records_usage():
    payload = {
        "seniority": "senior", "skills": ["python"],
        "salary": {"min": 1, "max": 2, "currency": "USD", "period": "year"}, "remote": True,
        "location_restriction": None, "role_family": "backend", "summary": "x", "risk_flags": [],
    }  # fmt: skip
    client, completions = fake_client(content=json.dumps(payload))
    result = LLMExtractor(client, "gpt-4o-mini").extract(posting())
    assert result.method == "llm"
    assert result.fields.role_family == "backend"
    assert (result.input_tokens, result.output_tokens) == (321, 45)
    fmt = completions.calls[0]["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True


def test_llm_extractor_falls_back_on_invalid_json_or_schema():
    for content in ("not json", json.dumps({"seniority": "wizard"})):
        client, _ = fake_client(content=content)
        assert LLMExtractor(client, "m").extract(posting()).method == "heuristic"


def test_llm_extractor_falls_back_on_api_error():
    client, _ = fake_client(error=RuntimeError("rate limited"))
    assert LLMExtractor(client, "m").extract(posting()).method == "heuristic"


def test_strict_schema_requires_every_property_recursively():
    schema = _strict_schema(ExtractedFields.model_json_schema())
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(ExtractedFields.model_fields)
    salary = schema["$defs"]["Salary"]
    assert salary["additionalProperties"] is False and set(salary["required"]) == {"min", "max", "currency", "period"}


def test_build_extractor_without_key_uses_rules():
    assert isinstance(build_extractor(), HeuristicExtractor)
