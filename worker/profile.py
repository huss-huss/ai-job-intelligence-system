"""Candidate profile: what a "good match" means for the person running FirstFetch."""

from __future__ import annotations

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
    # Location strings that mean "you can apply from here".
    eligible_terms: list[str] = Field(
        default_factory=lambda: ["worldwide", "anywhere", "global", "ethiopia", "africa", "emea"]
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

    @field_validator("skills")
    @classmethod
    def _check_weights(cls, v: dict[str, int]) -> dict[str, int]:
        bad = {k: w for k, w in v.items() if not 1 <= int(w) <= 3}
        if bad:
            raise ValueError(f"skill weights must be 1-3, got {bad}")
        return {k.lower(): int(w) for k, w in v.items()}

    def aliases_for(self, skill: str) -> list[str]:
        return SKILL_ALIASES.get(skill, [skill])


def load_profile(path: str | Path) -> CandidateProfile:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return CandidateProfile.model_validate(data)
