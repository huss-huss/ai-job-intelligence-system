"""Structured extraction of job facts, by LLM or by rules.

The LLM only extracts facts (seniority, salary, risk flags...). Scoring happens in
worker.matching so it stays deterministic. Any OpenAI-compatible endpoint works,
including a local Ollama server (base_url=http://localhost:11434/v1).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from worker.matching import detect_seniority
from worker.salary import normalize_period, parse_salary
from worker.sources.base import JobPosting

logger = logging.getLogger(__name__)

RiskFlag = Literal["commission_only", "unpaid", "vague_description", "requires_upfront_payment", "not_a_job"]


class Salary(BaseModel):
    min: float | None = None
    max: float | None = None
    currency: str | None = None
    period: Literal["hour", "day", "week", "month", "year"] | None = None


class ExtractedFields(BaseModel):
    seniority: Literal["intern", "junior", "mid", "senior", "lead"] | None = None
    skills: list[str] = Field(default_factory=list)
    salary: Salary = Field(default_factory=Salary)
    remote: bool | None = None
    location_restriction: str | None = Field(None, description="Countries/regions applicants must be in, if stated")
    role_family: Literal["backend", "fullstack", "frontend", "data", "ml", "devops", "other"] | None = None
    summary: str | None = None
    risk_flags: list[RiskFlag] = Field(default_factory=list)


@dataclass
class Extraction:
    fields: ExtractedFields
    method: str  # "llm" or "heuristic"
    input_tokens: int = 0
    output_tokens: int = 0


class Extractor(Protocol):
    def extract(self, posting: JobPosting) -> Extraction: ...


# --- rules -----------------------------------------------------------------------

_SKILL_TERMS = [
    "python", "fastapi", "django", "flask", "node.js", "typescript", "javascript", "go", "rust", "java",
    "postgresql", "mysql", "mongodb", "redis", "celery", "kafka", "docker", "kubernetes", "aws", "gcp",
    "azure", "terraform", "llm", "openai", "langchain", "rag", "pytorch", "airflow", "spark", "react",
]  # fmt: skip
_RISK_PATTERNS: dict[str, str] = {
    "commission_only": r"commission[- ]only|100% commission",
    "unpaid": r"\bunpaid\b|no salary|equity only",
    "requires_upfront_payment": r"(pay|purchase) (a|the) (fee|starter kit)|registration fee",
}


class HeuristicExtractor:
    """Keyword rules. Free, instant, and good enough when no LLM is configured."""

    def extract(self, posting: JobPosting) -> Extraction:
        text = f"{posting.title}\n{posting.description or ''}".lower()
        skills = [s for s in _SKILL_TERMS if re.search(rf"(?<![\w.]){re.escape(s)}(?![\w])", text)]
        smin, smax = posting.salary_min, posting.salary_max
        currency, period = posting.salary_currency, normalize_period(posting.salary_period)
        if smin is None and smax is None:
            parsed = parse_salary(posting.salary_text)
            smin, smax, currency, period = parsed.min, parsed.max, parsed.currency, parsed.period
        flags = [flag for flag, pattern in _RISK_PATTERNS.items() if re.search(pattern, text)]
        if len((posting.description or "").split()) < 40:
            flags.append("vague_description")
        fields = ExtractedFields(
            seniority=detect_seniority(posting.title),
            skills=skills,
            salary=Salary(min=smin, max=smax, currency=currency, period=period or ("year" if smax else None)),
            remote=posting.remote,
            role_family=_role_family(text),
            risk_flags=flags,
        )
        return Extraction(fields=fields, method="heuristic")


def _role_family(text: str) -> str:
    for family, terms in (
        ("ml", ["machine learning", "ml engineer", "llm", "ai engineer"]),
        ("data", ["data engineer", "etl", "data pipeline", "analytics engineer"]),
        ("devops", ["devops", "sre", "site reliability", "platform engineer"]),
        ("fullstack", ["full-stack", "fullstack", "full stack"]),
        ("frontend", ["frontend", "front-end", "react developer"]),
        ("backend", ["backend", "back-end", "api", "server-side"]),
    ):
        if any(t in text for t in terms):
            return family
    return "other"


# --- LLM -------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You extract facts from job postings. Only report what the posting states; use null when it "
    "doesn't say. Salary numbers are plain numbers (120000, not '120k'). location_restriction lists "
    "the countries or regions applicants must live in, if any. risk_flags marks postings that look "
    "commission-only, unpaid, ask for money, are too vague to evaluate, or are not real job offers."
)


def _strict_schema(schema: dict) -> dict:
    """OpenAI strict mode needs additionalProperties=false and every property required."""
    schema = dict(schema)
    if schema.get("type") == "object" or "properties" in schema:
        schema["additionalProperties"] = False
        schema["required"] = list(schema.get("properties", {}))
        schema["properties"] = {k: _strict_schema(v) for k, v in schema.get("properties", {}).items()}
    for key in ("$defs", "definitions"):
        if key in schema:
            schema[key] = {k: _strict_schema(v) for k, v in schema[key].items()}
    for key in ("anyOf", "allOf"):
        if key in schema:
            schema[key] = [_strict_schema(s) for s in schema[key]]
    if "items" in schema:
        schema["items"] = _strict_schema(schema["items"])
    schema.pop("default", None)
    return schema


class LLMExtractor:
    """OpenAI-compatible chat completions with a strict JSON schema; falls back to rules on any failure."""

    def __init__(self, client, model: str, fallback: Extractor | None = None, max_chars: int = 6000) -> None:
        self.client = client
        self.model = model
        self.fallback = fallback or HeuristicExtractor()
        self.max_chars = max_chars
        self.schema = _strict_schema(ExtractedFields.model_json_schema())

    def extract(self, posting: JobPosting) -> Extraction:
        user = (
            f"Title: {posting.title}\nCompany: {posting.company or 'unknown'}\n"
            f"Location: {posting.location or 'unknown'}\n\n{(posting.description or '')[: self.max_chars]}"
        )
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                temperature=0,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "job_facts", "schema": self.schema, "strict": True},
                },
            )
            fields = ExtractedFields.model_validate(json.loads(resp.choices[0].message.content or "{}"))
        except (ValidationError, json.JSONDecodeError) as exc:
            logger.warning("LLM returned invalid output for %s: %s", posting.url, exc)
            return self.fallback.extract(posting)
        except Exception as exc:  # network, auth, rate limit: degrade, don't fail the run
            logger.warning("LLM call failed for %s: %s", posting.url, exc)
            return self.fallback.extract(posting)
        usage = getattr(resp, "usage", None)
        return Extraction(
            fields=fields,
            method="llm",
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )


def build_extractor(api_key: str = "", model: str = "gpt-4o-mini", base_url: str = "") -> Extractor:
    """LLM extractor when a key (or a local base_url) is configured, otherwise rules."""
    if not api_key and not base_url:
        return HeuristicExtractor()
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai package not installed; using heuristic extraction")
        return HeuristicExtractor()
    client = OpenAI(api_key=api_key or "local", base_url=base_url or None)
    return LLMExtractor(client, model)
