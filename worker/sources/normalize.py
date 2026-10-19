"""Map each board's vocabulary for employment type and seniority onto one small set."""

from __future__ import annotations

import re

EMPLOYMENT_TYPES = ("full_time", "part_time", "contract", "internship")
SENIORITY_LEVELS = ("intern", "junior", "mid", "senior", "lead", "director")

_EMPLOYMENT_PATTERNS: list[tuple[str, str]] = [
    ("internship", r"\bintern(ship)?\b|\bfellowship\b|\btrainee\b"),
    ("part_time", r"\bpart[\s_-]?time\b"),
    ("contract", r"\bcontract(or)?\b|\bfreelance(r)?\b|\btemporary\b|\btemp\b|\bb2b\b"),
    ("full_time", r"\bfull[\s_-]?time\b|\bpermanent\b"),
]
_SENIORITY_PATTERNS: list[tuple[str, str]] = [
    ("director", r"\b(director|vp|vice president|c[\s_-]?level|head of|chief)\b"),
    ("lead", r"\b(lead|staff|principal|architect)\b"),
    ("senior", r"\b(senior|sr\.?|expert)\b"),
    ("mid", r"\b(mid|middle|midweight|intermediate|regular)\b"),
    ("junior", r"\b(junior|jr\.?|entry[\s_-]?level|graduate)\b"),
    ("intern", r"\bintern(ship)?\b"),
]


def normalize_employment_type(value: str | list[str] | None) -> str | None:
    """'Full-Time', ['Full-Time'], 'full_time', 'Contract' -> one of EMPLOYMENT_TYPES, or None.

    A list naming several types (Jobicy: ["Full-Time", "Contract"]) resolves to the first one
    that matches in list order.
    """
    if not value:
        return None
    for text in [value] if isinstance(value, str) else value:
        lowered = str(text).lower()
        for kind, pattern in _EMPLOYMENT_PATTERNS:
            if re.search(pattern, lowered):
                return kind
    return None


def normalize_seniority(value: str | None) -> str | None:
    """'Senior', 'middle', 'Midweight', 'staff', 'c_level', 'Entry-Level, Junior' -> SENIORITY_LEVELS, or None.

    "Any" and unknown words give None. With several levels ("Entry-Level, Junior", "Senior/Lead")
    the most senior one wins.
    """
    if not value:
        return None
    lowered = value.lower().replace("_", " ")
    for level, pattern in _SENIORITY_PATTERNS:
        if re.search(pattern, lowered):
            return level
    return None
