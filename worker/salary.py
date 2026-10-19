"""Salary normalization: (min, max, currency, period) → annual USD.

Scoring compares every salary against the profile's ``min_salary_usd``, so hourly,
monthly and non-USD figures are converted to one unit first. The assumptions are
deliberately simple and visible:

- An hourly rate is a full-time rate: 40 hours × 52 weeks = ``HOURS_PER_YEAR`` (2080).
  No unpaid leave is subtracted, so contract rates come out slightly optimistic.
- A monthly salary is paid ``MONTHS_PER_YEAR`` (12) times; 13th/14th-month bonuses are ignored.
- A daily rate covers ``WORKDAYS_PER_YEAR`` (5 × 52 = 260) days, a weekly one ``WEEKS_PER_YEAR`` (52).
- No period means annual, and no currency means USD (what the sources mean when they say neither).
- Currencies convert with a static table (``USD_PER_UNIT``). A currency that isn't in the
  table is unknown and the salary is not scored; a rate is never guessed.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from worker.config import settings

logger = logging.getLogger(__name__)

Period = Literal["hour", "day", "week", "month", "year"]

HOURS_PER_YEAR = 40 * 52
WORKDAYS_PER_YEAR = 5 * 52
WEEKS_PER_YEAR = 52
MONTHS_PER_YEAR = 12
PERIODS_PER_YEAR: dict[str, int] = {
    "hour": HOURS_PER_YEAR,
    "day": WORKDAYS_PER_YEAR,
    "week": WEEKS_PER_YEAR,
    "month": MONTHS_PER_YEAR,
    "year": 1,
}

# USD per one unit of each currency. Approximate mid-market rates as of 2025-06-30;
# precise enough for a salary floor, not for accounting. Refresh them here, or override
# without a deploy via FX_RATES_JSON='{"EUR": 1.1, "KES": 0.0077}' (merged over this table).
FX_RATES_AS_OF = "2025-06-30"
USD_PER_UNIT: dict[str, float] = {
    "USD": 1.0,
    "EUR": 1.17,
    "GBP": 1.37,
    "CHF": 1.26,
    "CAD": 0.73,
    "AUD": 0.66,
    "NZD": 0.61,
    "SEK": 0.105,
    "NOK": 0.099,
    "DKK": 0.157,
    "PLN": 0.28,
    "CZK": 0.047,
    "JPY": 0.0069,
    "SGD": 0.79,
    "HKD": 0.127,
    "INR": 0.0117,
    "BRL": 0.18,
    "MXN": 0.053,
    "ZAR": 0.056,
    "AED": 0.272,
    "ILS": 0.30,
    "ETB": 0.0074,
}
FX_ENV_VAR = "FX_RATES_JSON"


@lru_cache(maxsize=8)
def _rates(override: str) -> dict[str, float]:
    rates = dict(USD_PER_UNIT)
    if not override:
        return rates
    try:
        extra = json.loads(override)
        rates.update({str(k).upper(): float(v) for k, v in extra.items() if float(v) > 0})
    except (ValueError, TypeError, AttributeError) as exc:
        logger.warning("Ignoring invalid %s: %s", FX_ENV_VAR, exc)
    return rates


def fx_rates() -> dict[str, float]:
    """The static table merged with the FX_RATES_JSON override (env var or .env; parsed once per value)."""
    return _rates(os.environ.get(FX_ENV_VAR, settings.fx_rates_json))


def usd_rate(currency: str | None) -> float | None:
    """USD per unit of ``currency``; None (no currency) means USD; unknown codes give None."""
    if not currency or not currency.strip():
        return 1.0
    return fx_rates().get(currency.strip().upper())


@dataclass(frozen=True)
class AnnualSalary:
    min_usd: float | None
    max_usd: float | None

    @property
    def top(self) -> float | None:
        return self.max_usd or self.min_usd


def to_annual_usd(
    min_value: float | None,
    max_value: float | None,
    currency: str | None = None,
    period: str | None = None,
) -> AnnualSalary | None:
    """Convert a salary range to annual USD, or None when the currency or period is unknown."""
    if not min_value and not max_value:
        return None
    rate = usd_rate(currency)
    canonical = normalize_period(period) if period else "year"
    per_year = PERIODS_PER_YEAR.get(canonical) if canonical else None  # e.g. "one-time": unknown
    if rate is None or per_year is None:
        return None

    def convert(v: float | None) -> float | None:
        return round(v * per_year * rate, 2) if v else None

    return AnnualSalary(convert(min_value), convert(max_value))


# --- display ---------------------------------------------------------------------

_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹", "JPY": "¥"}
_PERIOD_SUFFIX = {"hour": "/hr", "day": "/day", "week": "/wk", "month": "/mo", "year": "/yr"}


def format_salary(value: float, currency: str | None = None, period: str | None = None) -> str:
    """Display form of an amount: "€60,000/yr", "$40/hr", "CAD 9,000/mo"."""
    code = (currency or "USD").upper()
    amount = f"{value:,.2f}".removesuffix(".00") if value < 1000 else f"{value:,.0f}"
    money = f"{_SYMBOLS[code]}{amount}" if code in _SYMBOLS else f"{code} {amount}"
    return money + _PERIOD_SUFFIX.get(normalize_period(period) or "year", "")


# --- detection from free text ----------------------------------------------------

_PERIOD_ALIASES: dict[str, Period] = {
    "hour": "hour", "hourly": "hour", "hr": "hour", "per-hour-wage": "hour",
    "day": "day", "daily": "day", "per-day-wage": "day",
    "week": "week", "weekly": "week", "per-week-wage": "week",
    "month": "month", "monthly": "month", "per-month-salary": "month",
    "year": "year", "yearly": "year", "annual": "year", "annually": "year", "per-year-salary": "year",
}  # fmt: skip
_PERIOD_PATTERNS: list[tuple[Period, str]] = [
    ("hour", r"/\s*(h|hr|hour)\b|\bper\s+hour\b|\bhourly\b|\ban\s+hour\b|\bp/?h\b"),
    ("day", r"/\s*(d|day)\b|\bper\s+day\b|\bdaily\b|\ba\s+day\b"),
    ("week", r"/\s*(wk|week)\b|\bper\s+week\b|\bweekly\b|\ba\s+week\b"),
    ("month", r"/\s*(mo|mth|month)\b|\bper\s+month\b|\bmonthly\b|\ba\s+month\b|\bpro\s+monat\b|\bmonatlich\b"),
    ("year", r"/\s*(y|yr|year|annum)\b|\bper\s+(year|annum)\b|\b(yearly|annual|annually)\b|\ba\s+year\b"
             r"|\bp\.?a\.?(?!\w)|\bpro\s+jahr\b|\bj(ä|ae)hrlich\b"),
]  # fmt: skip
# Prefixed dollar signs first so "CA$" isn't read as USD.
_SYMBOL_CURRENCIES: list[tuple[str, str]] = [
    ("US$", "USD"), ("CA$", "CAD"), ("C$", "CAD"), ("AU$", "AUD"), ("A$", "AUD"), ("NZ$", "NZD"),
    ("S$", "SGD"), ("HK$", "HKD"), ("R$", "BRL"), ("$", "USD"), ("€", "EUR"), ("£", "GBP"), ("₹", "INR"),
]  # fmt: skip
# A three-letter upper-case code next to a number: "USD 120,000", "100k EUR", "KES 150,000".
_CODE_RE = re.compile(r"\b([A-Z]{3})\s*\d|\d\s*[kK]?\s*([A-Z]{3})\b")
# 1,234,567 / 1.234.567 (thousands separators) or 35,3 / 40.50 (decimal), optional k suffix.
_NUMBER_RE = re.compile(
    r"(\d{1,3}(?:([.,])\d{3})(?:\2\d{3})*(?!\d)|\d+(?:[.,]\d+)?)\s*(?:(k)(?![a-z]))?", re.IGNORECASE
)


def normalize_period(value: str | None) -> Period | None:
    """Map source vocabularies ("per-hour-wage", "annual", "monthly") to a canonical period."""
    if not value:
        return None
    return _PERIOD_ALIASES.get(value.strip().lower())


def detect_period(text: str | None) -> Period | None:
    if not text:
        return None
    t = text.lower()
    for period, pattern in _PERIOD_PATTERNS:
        if re.search(pattern, t):
            return period
    return None


def detect_currency(text: str | None) -> str | None:
    """ISO code from a code or symbol in the text.

    A known code wins ("USD 120,000"), then a symbol ("£70k"), then an unknown code next to a
    number ("KES 150,000"), which is returned as-is so conversion reports it as unknown.
    """
    if not text:
        return None
    codes = [a or b for a, b in _CODE_RE.findall(text)]
    known = next((c for c in codes if c in fx_rates()), None)
    if known:
        return known
    for symbol, code in _SYMBOL_CURRENCIES:
        if symbol in text:
            return code
    return codes[0] if codes else None


def _to_float(number: str, separator: str) -> float:
    if separator:  # "120,000" or "5.000": thousands separators
        return float(number.replace(separator, ""))
    return float(number.replace(",", "."))  # "35,3" or "40.50": decimal


@dataclass(frozen=True)
class ParsedSalary:
    min: float | None = None
    max: float | None = None
    currency: str | None = None
    period: Period | None = None


def parse_salary(text: str | None) -> ParsedSalary:
    """Best-effort parse of "$40/hr", "€5,000 per month", "£70k", "USD 120,000 - 150,000"."""
    if not text:
        return ParsedSalary()
    period = detect_period(text)
    # Without an hourly/daily/weekly marker, small numbers are noise ("2 weeks PTO").
    floor = 1 if period in ("hour", "day", "week") else 1000
    values = []
    for number, separator, k in _NUMBER_RE.findall(text):
        n = _to_float(number, separator)
        if k:
            n *= 1000
        if n >= floor:
            values.append(n)
    if not values:
        return ParsedSalary()
    return ParsedSalary(min(values), max(values), detect_currency(text), period)
