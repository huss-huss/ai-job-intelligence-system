"""Find timezone requirements in job text ("US time zones only", "CET +/- 3 hours", "UTC-3 to UTC+3").

A requirement becomes a window of UTC offsets the employer accepts. `gap()` is how many hours
the candidate's offset lies outside that window (0 = inside). The parser is deliberately
conservative: a zone named in passing ("our HQ is in PST") only counts when the sentence talks
about time zones, working hours or overlap, or when it gives a tolerance or a range. In short
fields (location, timezone note) every zone counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Standard offset; zones that observe daylight time also accept offset + 1.
ZONES: dict[str, tuple[float, bool]] = {
    "UTC": (0, False), "GMT": (0, False), "WET": (0, True), "BST": (1, False), "CET": (1, True),
    "CEST": (2, False), "EET": (2, True), "EEST": (3, False), "MSK": (3, False), "EAT": (3, False),
    "GST": (4, False), "PKT": (5, False), "IST": (5.5, False), "SGT": (8, False), "JST": (9, False),
    "AEST": (10, True), "EST": (-5, True), "EDT": (-4, False), "ET": (-5, True), "CST": (-6, True),
    "CDT": (-5, False), "CT": (-6, True), "MST": (-7, True), "MDT": (-6, False), "MT": (-7, True),
    "PST": (-8, True), "PDT": (-7, False), "PT": (-8, True), "BRT": (-3, False), "ART": (-3, False),
}  # fmt: skip
NAMED_ZONES: dict[str, tuple[float, float]] = {
    "eastern time": (-5, -4),
    "central time": (-6, -5),
    "mountain time": (-7, -6),
    "pacific time": (-8, -7),
    "central european time": (1, 2),
    "eastern european time": (2, 3),
    "east africa time": (3, 3),
}
REGION_WINDOWS: list[tuple[str, tuple[float, float]]] = [
    (r"(?:us|u\.s\.|usa|north american?|american)", (-8, -4)),
    (r"americas", (-8, -3)),
    (r"(?:latam|latin american?|south american?)", (-6, -3)),
    (r"(?:european|europe|eu|cet)", (0, 3)),
    (r"emea", (-1, 4)),
    (r"(?:african?)", (0, 4)),
    (r"(?:apac|asia[- ]pacific|asian)", (5, 12)),
]
OVERLAP_SLACK = 4.0  # "overlap with EST" needs part of the day in common, not the same zone

_ZONE_ALT = "|".join(sorted(ZONES, key=len, reverse=True))
_OFFSET = r"[+\-−–]\s*\d{1,2}(?::?\d{2})?"
_ZONE_TOKEN = rf"\b(?:{_ZONE_ALT})\b(?:\s*{_OFFSET})?"
_TOLERANCE = r"\s*\(?\s*(?:\+\s*/\s*[-−]|±|\+-|plus or minus)\s*(\d{1,2})\s*(?:h\b|hrs?\b|hours?\b)?"
_RANGE = re.compile(rf"(?:between\s+)?({_ZONE_TOKEN})\s*(?:to|-|–|and|through)\s*({_ZONE_TOKEN})")
_WITH_TOLERANCE = re.compile(rf"({_ZONE_TOKEN}){_TOLERANCE}")
_SINGLE = re.compile(rf"({_ZONE_TOKEN})")
_REGION = re.compile(
    rf"\b({'|'.join(p for p, _ in REGION_WINDOWS)})\s+(?:business\s+|working\s+)?(?:time\s*zones?|timezones?|hours)\b",
    re.IGNORECASE,
)
_NAMED = re.compile(rf"\b({'|'.join(NAMED_ZONES)})\b", re.IGNORECASE)
_CONTEXT = re.compile(r"time\s*zones?|timezones?|\bhours\b|overlap|\bwithin\b|\bonly\b|based in", re.IGNORECASE)
_OVERLAP = re.compile(r"overlap", re.IGNORECASE)
_SENTENCE = re.compile(r"[^.!?\n]+")


@dataclass(frozen=True)
class TimezoneWindow:
    low: float
    high: float
    text: str

    def gap(self, offset: float) -> float:
        if self.low <= offset <= self.high:
            return 0.0
        return min(abs(offset - self.low), abs(offset - self.high))

    def describe(self) -> str:
        text = self.text.strip()
        return text + ")" if text.count("(") > text.count(")") else text


def _token_offset(token: str) -> tuple[float, float]:
    """'CET' -> (1, 2), 'UTC+3' -> (3, 3), 'GMT-05:30' -> (-5.5, -5.5)."""
    m = re.match(rf"({_ZONE_ALT})\s*({_OFFSET})?", token.strip())
    if not m:
        raise ValueError(token)
    base, dst = ZONES[m.group(1)]
    if m.group(2):
        raw = re.sub(r"\s", "", m.group(2)).replace("−", "-").replace("–", "-")
        sign = -1 if raw[0] == "-" else 1
        digits = raw[1:].replace(":", "")
        hours = int(digits[:-2]) + int(digits[-2:]) / 60 if len(digits) > 2 else int(digits)
        value = base + sign * hours
        return value, value
    return base, base + (1 if dst else 0)


def _windows_in(sentence: str, require_context: bool) -> list[TimezoneWindow]:
    found: list[TimezoneWindow] = []
    slack = OVERLAP_SLACK if _OVERLAP.search(sentence) else 0.0
    consumed: list[tuple[int, int]] = []

    def add(low: float, high: float, span: tuple[int, int], text: str) -> None:
        consumed.append(span)
        found.append(TimezoneWindow(low - slack, high + slack, text))

    for m in _RANGE.finditer(sentence):
        (a_lo, _), (_, b_hi) = _token_offset(m.group(1)), _token_offset(m.group(2))
        add(min(a_lo, b_hi), max(a_lo, b_hi), m.span(), m.group())
    for m in _WITH_TOLERANCE.finditer(sentence):
        if any(s <= m.start() < e for s, e in consumed):
            continue
        lo, hi = _token_offset(m.group(1))
        tol = float(m.group(2))
        add(lo - tol, hi + tol, m.span(), m.group())
    for m in _REGION.finditer(sentence):
        key = m.group(1).lower()
        window = next(w for p, w in REGION_WINDOWS if re.fullmatch(p, key))
        add(*window, m.span(), m.group())
    has_context = not require_context or bool(_CONTEXT.search(sentence))
    if has_context:
        for m in _NAMED.finditer(sentence):
            add(*NAMED_ZONES[m.group(1).lower()], m.span(), m.group())
        for m in _SINGLE.finditer(sentence):
            if any(s <= m.start() < e for s, e in consumed):
                continue
            add(*_token_offset(m.group(1)), m.span(), m.group())
    return found


def find_timezone_windows(text: str | None, *, short_field: bool = False) -> list[TimezoneWindow]:
    """Every timezone requirement stated in `text`. `short_field` drops the context requirement."""
    if not text:
        return []
    windows: list[TimezoneWindow] = []
    for sentence in _SENTENCE.findall(text[:20_000]):
        if re.search(rf"\b(?:{_ZONE_ALT})\b|time\s*zone|timezone|\btime\b", sentence, re.IGNORECASE):
            windows.extend(_windows_in(sentence, require_context=not short_field))
    return windows
