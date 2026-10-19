"""Candidate profile: what a "good match" means for the person running FirstFetch."""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

# Common spellings so a profile can say "postgres" and still match "PostgreSQL".
SKILL_ALIASES: dict[str, list[str]] = {
    "python": ["python"],
    "fastapi": ["fastapi"],
    "django": ["django"],
    "flask": ["flask"],
    "node": ["node.js", "nodejs", "node"],
    "typescript": ["typescript", "ts"],
    "javascript": ["javascript", "js"],
    "postgres": ["postgresql", "postgres"],
    "mongodb": ["mongodb", "mongo"],
    "redis": ["redis"],
    "celery": ["celery"],
    "docker": ["docker", "containers"],
    "kubernetes": ["kubernetes", "k8s"],
    "aws": ["aws", "amazon web services"],
    "gcp": ["gcp", "google cloud"],
    "llm": ["llm", "llms", "large language model", "large language models", "genai", "generative ai"],
    "openai": ["openai", "gpt-4", "gpt-4o", "chatgpt"],
    "agents": ["ai agent", "ai agents", "agentic", "multi-agent", "tool calling", "tool-calling"],
    "rag": ["rag", "retrieval-augmented", "retrieval augmented", "vector database", "embeddings"],
    "scraping": ["web scraping", "scraping", "crawler", "crawling", "playwright", "scrapy", "selenium"],
    "etl": ["etl", "elt", "data pipeline", "data pipelines"],
    "rest": ["rest api", "restful", "rest"],
    "graphql": ["graphql"],
    "sql": ["sql"],
    "ci/cd": ["ci/cd", "github actions", "continuous integration"],
}


class LocationPrefs(BaseModel):
    country: str = "Ethiopia"
    country_code: str = "et"  # ISO 3166-1 alpha-2, matched against sources that list countries (FreeHire)
    # Location strings that mean "you can apply from here".
    eligible_terms: list[str] = Field(
        default_factory=lambda: ["worldwide", "anywhere", "global", "ethiopia", "africa", "emea", "mena"]
    )
    accepts_onsite: bool = False


class CandidateProfile(BaseModel):
    name: str = "Candidate"
    target_titles: list[str] = Field(default_factory=lambda: ["backend", "python", "software engineer"])
    exclude_titles: list[str] = Field(default_factory=list)
    skills: dict[str, int] = Field(default_factory=dict, description="skill -> weight (1-3)")
    seniority: list[str] = Field(default_factory=lambda: ["mid", "senior"])
    location: LocationPrefs = Field(default_factory=LocationPrefs)
    min_salary_usd: float | None = None
    max_age_days: int = 30
    notify_threshold: int = 70

    # Hire-probability settings (worker/hire.py)
    # How much each employment type is worth to you, 0-1. Types not listed count as 0.1.
    employment_types: dict[str, float] = Field(default_factory=lambda: {"full_time": 1.0, "contract": 0.6})
    timezone: str = "UTC+3"  # your UTC offset, e.g. "UTC+3", "UTC-05:00", "+5.5"
    # Competition (applicants + views/20) at which the competition factor drops to 0.2.
    max_competition: int = Field(50, ge=1)
    languages: list[str] = Field(default_factory=lambda: ["english"], description="languages you work in")
    years_experience: int | None = None  # compared with a posting's stated minimum, when known

    @field_validator("skills")
    @classmethod
    def _check_weights(cls, v: dict[str, int]) -> dict[str, int]:
        bad = {k: w for k, w in v.items() if not 1 <= int(w) <= 3}
        if bad:
            raise ValueError(f"skill weights must be 1-3, got {bad}")
        return {k.lower(): int(w) for k, w in v.items()}

    @field_validator("employment_types")
    @classmethod
    def _check_employment_types(cls, v: dict[str, float]) -> dict[str, float]:
        bad = {k: w for k, w in v.items() if not 0 <= float(w) <= 1}
        if bad:
            raise ValueError(f"employment_types weights must be 0-1, got {bad}")
        return {k.lower().replace("-", "_").replace(" ", "_"): float(w) for k, w in v.items()}

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, v: str) -> str:
        parse_utc_offset(v)
        return v

    @field_validator("languages")
    @classmethod
    def _lower_languages(cls, v: list[str]) -> list[str]:
        return [lang.strip().lower() for lang in v if lang.strip()]

    @property
    def utc_offset(self) -> float:
        return parse_utc_offset(self.timezone)

    def aliases_for(self, skill: str) -> list[str]:
        return SKILL_ALIASES.get(skill, [skill])


_UTC_OFFSET = re.compile(r"(?:utc|gmt)?\s*(?:([+-]?)\s*(\d{1,2}(?:\.\d+)?)(?::(\d{2}))?)?", re.IGNORECASE)


def parse_utc_offset(value: str | float | int) -> float:
    """'UTC+3' -> 3.0, 'GMT-05:30' -> -5.5, '+5.5' -> 5.5, 'UTC' -> 0.0, 3 -> 3.0."""
    if isinstance(value, (int, float)):
        return float(value)
    text = value.strip()
    m = _UTC_OFFSET.fullmatch(text)
    if not text or not m:
        raise ValueError(f"timezone must look like 'UTC+3' or 'UTC-05:30', got {value!r}")
    if m.group(2) is None:
        return 0.0
    hours = float(m.group(2)) + (int(m.group(3)) / 60 if m.group(3) else 0.0)
    if hours > 14:
        raise ValueError(f"UTC offset out of range: {value!r}")
    return -hours if m.group(1) == "-" else hours


def load_profile(path: str | Path) -> CandidateProfile:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return CandidateProfile.model_validate(data)
