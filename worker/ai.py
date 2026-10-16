"""AI extraction and deterministic scoring."""

import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, Field

# Optional OpenAI - graceful if not installed
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


EXTRACTION_SCHEMA = {
    "seniority": "junior|mid|senior|lead",
    "skills": "list of strings",
    "salary": {"min": "number or 0", "max": "number or 0", "currency": "USD", "period": "hour|month|year"},
    "remote": "boolean",
    "role_family": "backend|data|ml|devops|other",
    "summary": "short string",
    "risk_flags": "list of strings e.g. vague, commission_only",
}


class ExtractedFields(BaseModel):
    """Validated extraction output."""

    seniority: str | None = Field(None, description="junior|mid|senior|lead")
    skills: list[str] = Field(default_factory=list)
    salary: dict[str, Any] = Field(default_factory=lambda: {"min": 0, "max": 0, "currency": "USD", "period": "year"})
    remote: bool = False
    role_family: str | None = None
    summary: str | None = None
    risk_flags: list[str] = Field(default_factory=list)


def _deterministic_score(extracted: ExtractedFields, text: str) -> float:
    """Compute a stable score from rules (no LLM)."""
    score = 0.5  # base
    text_lower = (text or "").lower()

    # Seniority
    if extracted.seniority in ("senior", "lead"):
        score += 0.1
    if extracted.role_family:
        score += 0.05

    # Remote
    if extracted.remote or "remote" in text_lower:
        score += 0.1

    # Salary mentioned
    if (extracted.salary.get("min") or 0) > 0 or (extracted.salary.get("max") or 0) > 0:
        score += 0.1

    # Skills count
    if len(extracted.skills) >= 3:
        score += 0.05

    # Risk flags
    for _ in extracted.risk_flags:
        score -= 0.05
    if "vague" in extracted.risk_flags or "commission_only" in extracted.risk_flags:
        score -= 0.1

    return max(0.0, min(1.0, round(score, 3)))


def extract_with_llm(description: str, api_key: str, model: str = "gpt-4o-mini") -> ExtractedFields:
    """Call LLM to extract structured fields. Falls back to heuristic if no API key."""
    if not api_key or not OpenAI:
        return _heuristic_extract(description)

    client = OpenAI(api_key=api_key)
    prompt = f"""Extract job attributes from this job description. Return ONLY valid JSON matching this schema:
{json.dumps(EXTRACTION_SCHEMA, indent=2)}

Job description:
---
{description[:6000]}
---

Return only the JSON object, no markdown or explanation."""

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        content = resp.choices[0].message.content or "{}"
        content = re.sub(r"^```\w*\n?", "", content).strip()
        content = re.sub(r"\n?```$", "", content).strip()
        data = json.loads(content)
        return ExtractedFields(
            seniority=data.get("seniority"),
            skills=data.get("skills") or [],
            salary=data.get("salary") or {},
            remote=bool(data.get("remote")),
            role_family=data.get("role_family"),
            summary=data.get("summary"),
            risk_flags=data.get("risk_flags") or [],
        )
    except Exception:
        return _heuristic_extract(description)


def _heuristic_extract(description: str) -> ExtractedFields:
    """Fallback when LLM is unavailable."""
    text = (description or "").lower()
    seniority = None
    for level in ("lead", "senior", "mid", "junior"):
        if level in text:
            seniority = level
            break
    skills = []
    for skill in ("python", "fastapi", "redis", "postgresql", "react", "typescript"):
        if skill in text:
            skills.append(skill)
    remote = "remote" in text or "work from home" in text
    return ExtractedFields(
        seniority=seniority,
        skills=skills,
        remote=remote,
        role_family="backend" if any(s in text for s in ["api", "backend", "server"]) else "other",
    )


def compute_score(extracted: ExtractedFields, job_text: str) -> float:
    """Deterministic score from extracted fields and raw text."""
    return _deterministic_score(extracted, job_text)
