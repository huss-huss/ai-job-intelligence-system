"""FreeHire, Djinni, We Work Remotely, Jobicy and Working Nomads adapters, against trimmed real responses."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from worker.sources import fetch_postings
from worker.sources.base import HttpClient, JobPosting, SourceError, parse_rfc822
from worker.sources.feeds import (
    DjinniAdapter,
    JobicyAdapter,
    WeWorkRemotelyAdapter,
    WorkingNomadsAdapter,
    check_applied_filters,
)
from worker.sources.freehire import FreeHireAdapter, IgnoredParamsError, check_ignored_params
from worker.sources.normalize import normalize_employment_type, normalize_seniority
from worker.sources.rss import parse_rss

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text())


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def mock_client(handler, sleeps: list | None = None) -> HttpClient:
    sleep = sleeps.append if sleeps is not None else (lambda s: None)
    return HttpClient(transport=httpx.MockTransport(handler), min_interval=0, sleep=sleep)


def freehire_handler(calls: list, *, meta_extra: dict | None = None, headers: dict | None = None):
    jobs, companies = load_json("freehire.json"), load_json("freehire_companies.json")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = companies if request.url.path.endswith("/companies") else jobs
        if meta_extra:
            body = {**body, "meta": {**body["meta"], **meta_extra}}
        return httpx.Response(200, json=body, headers=headers or {})

    return handler


# --- FreeHire ----------------------------------------------------------------------------------


def test_freehire_parse_maps_geography_counts_and_enrichment():
    rows = load_json("freehire.json")["data"]
    p = FreeHireAdapter.parse(next(j for j in rows if j.get("source") == "ashby"))
    assert p.source == "freehire" and p.external_id.startswith("finops-data-engineer")
    assert p.location == "Remote, Global" and p.regions == ["global"] and p.remote is True
    assert p.employment_type == "full_time"
    assert p.timezone_note and "global hiring" in p.timezone_note
    assert p.view_count == 19 and p.applied_count == 0
    assert p.posting_language == "en"
    assert p.extra["origin"] == "ashby" and p.extra["freehire_url"].startswith("https://freehire.me/jobs/")
    assert "aws" in p.tags


def test_freehire_parse_salary_seniority_and_countries():
    rows = load_json("freehire.json")["data"]
    p = FreeHireAdapter.parse(next(j for j in rows if j.get("source") == "himalayas"))
    assert (p.salary_min, p.salary_max, p.salary_currency, p.salary_period) == (1100, 2000, "EUR", "month")
    assert p.seniority == "senior" and p.countries == ["tn"] and p.regions == ["mena"]


def test_freehire_dates_fake_freshness_from_first_seen():
    rows = load_json("freehire.json")["data"]
    raw = next(j for j in rows if (j.get("reality") or {}).get("fake_freshness"))
    p = FreeHireAdapter.parse(raw)
    assert p.posted_at.isoformat().startswith(raw["created_at"][:19])
    assert p.extra["fake_freshness"] is True


def test_freehire_keeps_board_url_for_djinni_origin():
    rows = load_json("freehire.json")["data"]
    p = FreeHireAdapter.parse(next(j for j in rows if j.get("source") == "djinni"))
    assert p.url.startswith("https://djinni.co/jobs/851382-")
    assert p.location == "Europe" and p.extra["origin"] == "djinni"


def test_freehire_sends_each_query_with_repeated_facets_and_attaches_company_regions():
    calls: list[httpx.Request] = []
    config = {
        "type": "freehire",
        "defaults": {"employment_type": ["full_time", "contract"], "limit": 50},
        "queries": [{"q": "title:backend", "regions": ["global", "africa"]}, {"q": "title:llm"}],
        "company_remote_regions": ["africa"],
        "company_pages": 1,
    }
    postings = FreeHireAdapter(mock_client(freehire_handler(calls))).fetch(config)
    searches = [c for c in calls if c.url.path.endswith("/agent/jobs/search")]
    assert len(searches) == 2
    first = searches[0].url.params
    assert first.get_list("regions") == ["global", "africa"]
    assert first.get_list("employment_type") == ["full_time", "contract"]
    assert first["work_mode"] == "remote" and first["sort"] == "posted_at" and first["description_format"] == "text"
    assert "queries" not in first and "company_pages" not in first
    companies = [c for c in calls if c.url.path.endswith("/companies")]
    assert len(companies) == 1 and companies[0].url.params["remote_regions"] == "africa"
    hiring = next(p for p in postings if p.extra.get("origin") == "weworkremotely")  # its company is listed
    assert hiring.extra["company_hires_remote_in"] == ["africa"]


def test_freehire_ignored_param_fails_loudly():
    calls: list = []
    meta = {"ignored_params": [{"param": "country", "did_you_mean": "countries"}]}
    adapter = FreeHireAdapter(mock_client(freehire_handler(calls, meta_extra=meta)))
    with pytest.raises(IgnoredParamsError, match="did you mean 'countries'"):
        adapter.fetch({"type": "freehire", "country": "et"})


def test_freehire_non_strict_logs_ignored_params(caplog):
    calls: list = []
    meta = {"ignored_params": [{"param": "skil"}]}
    adapter = FreeHireAdapter(mock_client(freehire_handler(calls, meta_extra=meta)))
    postings = adapter.fetch({"type": "freehire", "skil": "go", "strict": False})
    assert postings and "ignored params" in caplog.text


def test_check_ignored_params_accepts_clean_meta():
    check_ignored_params({"total": 3, "limit": 10, "offset": 0}, {})
    check_ignored_params(None, {})


def test_freehire_pages_until_total_and_clamps_limit():
    calls: list = []
    jobs = load_json("freehire.json")

    def handler(request):
        calls.append(request)
        offset = int(request.url.params["offset"])
        rows = jobs["data"][offset : offset + 3]
        return httpx.Response(200, json={"data": rows, "meta": {"total": 7, "limit": 3, "offset": offset}})

    config = {"type": "freehire", "limit": 3, "max_pages": 10}
    assert FreeHireAdapter.query_params({"type": "freehire", "limit": 500})[0]["limit"] == 100
    postings = FreeHireAdapter(mock_client(handler)).fetch(config)
    assert len(postings) == 7 and [c.url.params["offset"] for c in calls] == ["0", "3", "6"]


def test_freehire_parse_survives_minimal_rows():
    p = FreeHireAdapter.parse({"public_slug": "x-1", "title": "Engineer", "company": "Acme"})
    assert p.url == "https://freehire.me/jobs/x-1" and p.location is None and p.remote is None


# --- HttpClient rate limits ---------------------------------------------------------------------


def test_http_client_pauses_when_rate_limit_runs_low():
    sleeps: list[float] = []
    remaining = iter(["10", "1", "50"])

    def handler(request):
        return httpx.Response(
            200, json={}, headers={"X-RateLimit-Remaining": next(remaining), "X-RateLimit-Reset": "7"}
        )

    http = mock_client(handler, sleeps)
    for _ in range(3):
        http.get_json("https://freehire.me/api/v1/jobs/search")
    assert sleeps == [7.0]  # only before the request after Remaining hit the low-water mark


def test_http_client_honors_retry_after_without_double_waiting():
    sleeps: list[float] = []
    responses = iter([
        httpx.Response(429, headers={"Retry-After": "3", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "3"}),
        httpx.Response(200, json={"ok": True}),
    ])  # fmt: skip
    http = mock_client(lambda request: next(responses), sleeps)
    assert http.get_json("https://freehire.me/api/v1/agent/jobs/search") == {"ok": True}
    assert sleeps == [3.0]


def test_http_client_retries_cloudflare_522():
    responses = iter([httpx.Response(522, text="error code: 522"), httpx.Response(200, json={"data": []})])
    http = mock_client(lambda request: next(responses))
    assert http.get_json("https://freehire.me/api/v1/companies") == {"data": []}


# --- RSS ----------------------------------------------------------------------------------------


def test_parse_rss_reads_items_and_repeated_tags():
    items = parse_rss(load_text("djinni.xml"))
    assert len(items) == 3
    assert items[0].get("link").startswith("https://djinni.co/jobs/")
    assert items[0].all("category") == ["Python"]


def test_parse_rss_refuses_dtd_and_entities():
    evil = (
        '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]>'
        "<rss><channel><item><title>&a;</title></item></channel></rss>"
    )
    with pytest.raises(SourceError, match="DTD"):
        parse_rss(evil)


def test_parse_rss_reports_malformed_xml():
    with pytest.raises(SourceError, match="malformed"):
        parse_rss("<rss><channel><item>")


def test_parse_rfc822_keeps_offset():
    dt = parse_rfc822("Mon, 19 Oct 2026 14:19:05 +0300")
    assert dt == datetime(2026, 10, 19, 11, 19, 5, tzinfo=UTC)
    assert parse_rfc822(None) is None and parse_rfc822("2026-10-19T00:00:00Z").year == 2026


# --- Djinni -------------------------------------------------------------------------------------


def test_djinni_fetch_sends_supported_params_and_parses_items():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=load_text("djinni.xml"))

    config = {
        "type": "djinni",
        "employment": "remote",
        "queries": [{"primary_keyword": "Python"}, {"primary_keyword": "ML AI"}],
    }
    postings = DjinniAdapter(mock_client(handler)).fetch(config)
    assert [c.url.params["primary_keyword"] for c in calls] == ["Python", "ML AI"]
    assert all(c.url.params["employment"] == "remote" for c in calls)
    p = next(p for p in postings if p.external_id == "851382")
    assert p.url == "https://djinni.co/jobs/851382-staff-backend-engineer-python-lun-app/"
    assert p.remote is True and p.company is None and p.location is None
    assert p.posted_at.tzinfo is not None and "<" not in p.description


@pytest.mark.parametrize("config", [{"region": "other"}, {"queries": [{"keywords": "fastapi"}]}])
def test_djinni_refuses_params_the_feed_ignores(config):
    with pytest.raises(SourceError, match="ignores"):
        DjinniAdapter(mock_client(lambda r: httpx.Response(200, text=""))).fetch({"type": "djinni", **config})


# --- We Work Remotely ---------------------------------------------------------------------------


def test_wwr_splits_company_and_prefers_country_list_over_region():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=load_text("weworkremotely.xml"))

    postings = WeWorkRemotelyAdapter(mock_client(handler)).fetch({"categories": ["back-end-programming"]})
    assert calls[0].url.path == "/categories/remote-back-end-programming-jobs.rss"
    worldwide = next(p for p in postings if p.location == "Anywhere in the World" and "Python" in p.title)
    assert worldwide.title == "Senior Backend Developer (Python)" and worldwide.company  # "<company>: <title>" split
    assert worldwide.location == "Anywhere in the World" and worldwide.employment_type == "full_time"
    assert worldwide.url.startswith("https://weworkremotely.com/remote-jobs/")  # link back to the listing
    assert "Python" in worldwide.tags and "Back-End Programming" in worldwide.tags
    restricted = next(p for p in postings if p.location != "Anywhere in the World")
    assert restricted.location.startswith("Andorra, Albania") and "\U0001f1e6" not in restricted.location


def test_wwr_rejects_bad_category_slug():
    with pytest.raises(SourceError, match="category"):
        WeWorkRemotelyAdapter(mock_client(lambda r: httpx.Response(200))).fetch({"categories": ["../admin"]})


# --- Jobicy -------------------------------------------------------------------------------------


def test_jobicy_parse_maps_geo_type_level_and_salary():
    data = load_json("jobicy.json")
    usa = JobicyAdapter.parse(data["jobs"][0])
    assert usa.location == "USA" and usa.employment_type == "full_time" and usa.seniority == "director"
    assert (usa.salary_min, usa.salary_max, usa.salary_currency, usa.salary_period) == (145000, 165000, "USD", "year")
    anywhere = JobicyAdapter.parse(next(j for j in data["jobs"] if j["jobGeo"] == "Anywhere"))
    assert anywhere.location == "Anywhere" and anywhere.url.startswith("https://jobicy.com/jobs/")


def test_jobicy_fetch_checks_applied_filters():
    data = load_json("jobicy.json")  # appliedFilters: count + tag

    def handler(request):
        return httpx.Response(200, json=data)

    postings = JobicyAdapter(mock_client(handler)).fetch({"queries": [{"tag": "python"}]})
    assert len(postings) == 3
    with pytest.raises(SourceError, match="ignored filter"):
        JobicyAdapter(mock_client(handler)).fetch({"queries": [{"tag": "python", "geo": "emea"}]})


def test_jobicy_error_payload_and_unknown_params_raise():
    bad = {"success": False, "error": "Invalid 'geo' value."}
    with pytest.raises(SourceError, match="Invalid 'geo'"):
        JobicyAdapter(mock_client(lambda r: httpx.Response(200, json=bad))).fetch({"geo": "africa"})
    with pytest.raises(SourceError, match="ignores"):
        JobicyAdapter(mock_client(lambda r: httpx.Response(200, json={}))).fetch({"queries": [{"page": 2}]})
    check_applied_filters(None, {"tag": "x"})


# --- Working Nomads -----------------------------------------------------------------------------


def test_workingnomads_filters_category_and_keywords():
    data = load_json("workingnomads.json")
    http = mock_client(lambda r: httpx.Response(200, json=data))
    dev = WorkingNomadsAdapter(http).fetch({"categories": ["Development"]})
    assert dev and all("Development" in p.tags for p in dev)
    everything = WorkingNomadsAdapter(http).fetch({})
    assert len(everything) == len(data)
    cet = next(p for p in everything if "CET" in (p.location or ""))
    assert cet.external_id.isdigit() and cet.remote is True and cet.company


def test_workingnomads_rejects_non_list_payload():
    with pytest.raises(SourceError):
        WorkingNomadsAdapter(mock_client(lambda r: httpx.Response(200, json={"jobs": []}))).fetch({})


# --- shared -------------------------------------------------------------------------------------


def test_fetch_postings_max_age_days_drops_old_postings():
    now = datetime.now(UTC)
    rows = [
        {"url": "https://www.workingnomads.com/job/go/1/", "title": "New", "pub_date": now.isoformat()},
        {
            "url": "https://www.workingnomads.com/job/go/2/",
            "title": "Old",
            "pub_date": (now - timedelta(days=90)).isoformat(),
        },
        {"url": "https://www.workingnomads.com/job/go/3/", "title": "Undated"},
    ]
    http = mock_client(lambda r: httpx.Response(200, json=rows))
    kept = fetch_postings({"type": "workingnomads", "max_age_days": 30}, http=http)
    assert [p.title for p in kept] == ["New", "Undated"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Full-Time", "full_time"), (["Contract"], "contract"), ("part_time", "part_time"), ("Internship", "internship"),
     ("Freelance", "contract"), (None, None), ("Other", None)],
)  # fmt: skip
def test_normalize_employment_type(value, expected):
    assert normalize_employment_type(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Senior", "senior"), ("middle", "mid"), ("Midweight", "mid"), ("staff", "lead"), ("c_level", "director"),
     ("Entry-Level, Junior", "junior"), ("Director", "director"), ("Any", None), (None, None)],
)  # fmt: skip
def test_normalize_seniority(value, expected):
    assert normalize_seniority(value) == expected


def test_new_postings_have_independent_defaults():
    a, b = (
        JobPosting(source="s", external_id="1", url="u", title="t"),
        JobPosting(source="s", external_id="2", url="u", title="t"),
    )
    a.extra["x"] = 1
    a.regions.append("global")
    assert b.extra == {} and b.regions == []


def test_freehire_time_budget_skips_remaining_queries():
    calls: list = []
    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0])
    adapter = FreeHireAdapter(mock_client(freehire_handler(calls)), clock=lambda: next(ticks))
    config = {
        "type": "freehire",
        "max_seconds": 30,
        "queries": [{"q": "a"}, {"q": "b"}],
        "company_remote_regions": ["africa"],
    }
    postings = adapter.fetch(config)
    assert postings and [c.url.params.get("q") for c in calls] == ["a"]  # "b" and the company lookup skipped


def test_freehire_company_lookup_failure_is_not_fatal():
    jobs = load_json("freehire.json")

    def handler(request):
        if request.url.path.endswith("/companies"):
            return httpx.Response(522, text="error code: 522", headers={"Retry-After": "300"})
        return httpx.Response(200, json=jobs)

    sleeps: list[float] = []
    postings = FreeHireAdapter(mock_client(handler, sleeps)).fetch({"company_remote_regions": ["africa"]})
    assert len(postings) == len(jobs["data"])
    assert sleeps == [10.0]  # one retry, Retry-After capped at RETRY_WAIT_CAP
    assert not any("company_hires_remote_in" in p.extra for p in postings)
