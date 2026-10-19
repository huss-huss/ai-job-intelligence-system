import logging

import pytest

from worker.salary import (
    FX_ENV_VAR,
    HOURS_PER_YEAR,
    MONTHS_PER_YEAR,
    USD_PER_UNIT,
    AnnualSalary,
    ParsedSalary,
    detect_currency,
    detect_period,
    format_salary,
    fx_rates,
    normalize_period,
    parse_salary,
    to_annual_usd,
    usd_rate,
)


@pytest.fixture(autouse=True)
def no_fx_override(monkeypatch):
    monkeypatch.delenv(FX_ENV_VAR, raising=False)


# --- conversion ------------------------------------------------------------------


def test_documented_assumptions():
    assert HOURS_PER_YEAR == 2080
    assert MONTHS_PER_YEAR == 12


def test_annual_usd_passes_through():
    assert to_annual_usd(90000, 120000) == AnnualSalary(90000, 120000)
    assert to_annual_usd(90000, 120000, "USD", "year") == AnnualSalary(90000, 120000)
    assert to_annual_usd(90000, 120000, "", "") == AnnualSalary(90000, 120000)  # blank fields mean defaults


def test_hourly_is_multiplied_by_full_time_hours():
    assert to_annual_usd(40, 60, "USD", "hour") == AnnualSalary(83200, 124800)


def test_monthly_is_multiplied_by_twelve():
    assert to_annual_usd(5000, None, "USD", "month") == AnnualSalary(60000, None)


def test_daily_and_weekly_rates():
    assert to_annual_usd(None, 400, "USD", "day").max_usd == 400 * 260
    assert to_annual_usd(None, 2000, "USD", "week").max_usd == 2000 * 52


def test_currency_converts_with_table_rate():
    result = to_annual_usd(50000, 60000, "EUR", "year")
    assert result == AnnualSalary(round(50000 * USD_PER_UNIT["EUR"], 2), round(60000 * USD_PER_UNIT["EUR"], 2))


def test_currency_and_period_combine():
    result = to_annual_usd(None, 5000, "eur", "month")  # codes are case-insensitive
    assert result.max_usd == pytest.approx(5000 * 12 * USD_PER_UNIT["EUR"])


def test_unknown_currency_is_unknown_not_guessed():
    assert usd_rate("XYZ") is None
    assert to_annual_usd(100000, 150000, "KES", "year") is None


def test_unknown_period_is_unknown_not_guessed():
    assert to_annual_usd(5000, 5000, "USD", "one-time") is None


def test_missing_amounts_are_unknown():
    assert to_annual_usd(None, None, "USD", "year") is None


def test_top_prefers_max():
    assert AnnualSalary(10, 20).top == 20
    assert AnnualSalary(10, None).top == 10


# --- FX override -----------------------------------------------------------------


def test_env_override_replaces_and_adds_rates(monkeypatch):
    monkeypatch.setenv(FX_ENV_VAR, '{"eur": 2.0, "KES": 0.0077}')
    assert usd_rate("EUR") == 2.0
    assert usd_rate("KES") == 0.0077
    assert usd_rate("GBP") == USD_PER_UNIT["GBP"]  # untouched entries keep the table rate
    assert to_annual_usd(None, 1000, "EUR", "month").max_usd == 24000


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", '{"EUR": "abc"}'])
def test_invalid_env_override_is_ignored(monkeypatch, caplog, raw):
    monkeypatch.setenv(FX_ENV_VAR, raw)
    with caplog.at_level(logging.WARNING):
        assert fx_rates() == USD_PER_UNIT
    assert FX_ENV_VAR in caplog.text


def test_override_does_not_mutate_static_table(monkeypatch):
    monkeypatch.setenv(FX_ENV_VAR, '{"EUR": 9}')
    fx_rates()
    assert USD_PER_UNIT["EUR"] != 9


# --- detection -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$40/hr", "hour"),
        ("$40 per hour", "hour"),
        ("Hourly: $35-45", "hour"),
        ("€5,000 per month", "month"),
        ("$6k/mo", "month"),
        ("5.000 € pro Monat", "month"),
        ("$120,000 a year", "year"),
        ("£70k p.a.", "year"),
        ("$400/day", "day"),
        ("$2,000 weekly", "week"),
        ("£70k", None),
        ("2 weeks PTO", None),
        (None, None),
    ],
)
def test_detect_period(text, expected):
    assert detect_period(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$40/hr", "USD"),
        ("€5,000 per month", "EUR"),
        ("£70k", "GBP"),
        ("₹12,00,000", "INR"),
        ("USD 120,000 - 150,000", "USD"),
        ("100k EUR", "EUR"),
        ("CA$90,000", "CAD"),
        ("A$120k", "AUD"),
        ("$120k DOE", "USD"),  # an unrelated capitalized word doesn't override the symbol
        ("KES 150,000", "KES"),  # unknown code next to a number is kept, so it converts to unknown
        ("80,000-100,000", None),
        (None, None),
    ],
)
def test_detect_currency(text, expected):
    assert detect_currency(text) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("per-hour-wage", "hour"),
        ("per-month-salary", "month"),
        ("per-year-salary", "year"),
        ("annual", "year"),
        ("Monthly", "month"),
        ("one-time", None),
        (None, None),
    ],
)
def test_normalize_period(value, expected):
    assert normalize_period(value) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$40/hr", ParsedSalary(40, 40, "USD", "hour")),
        ("$40-60 per hour", ParsedSalary(40, 60, "USD", "hour")),
        ("€5,000 per month", ParsedSalary(5000, 5000, "EUR", "month")),
        ("£70k", ParsedSalary(70000, 70000, "GBP", None)),
        ("USD 120,000 - 150,000", ParsedSalary(120000, 150000, "USD", None)),
        ("$35,3k- $52k", ParsedSalary(35300, 52000, "USD", None)),  # decimal comma
        ("5.000 € pro Monat", ParsedSalary(5000, 5000, "EUR", "month")),  # dot thousands separator
        ("80,000-100,000USD", ParsedSalary(80000, 100000, "USD", None)),
        ("$120k, 2 weeks PTO", ParsedSalary(120000, 120000, "USD", None)),
        ("Competitive", ParsedSalary()),
        (None, ParsedSalary()),
    ],
)
def test_parse_salary(text, expected):
    assert parse_salary(text) == expected


def test_parsed_text_converts_end_to_end():
    p = parse_salary("€5,000 per month")
    assert to_annual_usd(p.min, p.max, p.currency, p.period).top == pytest.approx(60000 * USD_PER_UNIT["EUR"])


# --- display ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "args,expected",
    [
        ((60000, "EUR", "year"), "€60,000/yr"),
        ((40, None, "hour"), "$40/hr"),
        ((40.5, "USD", "hour"), "$40.50/hr"),
        ((9000, "CAD", "month"), "CAD 9,000/mo"),
        ((120000, None, None), "$120,000/yr"),
    ],
)
def test_format_salary(args, expected):
    assert format_salary(*args) == expected
