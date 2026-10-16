import json
from pathlib import Path

import httpx
import pytest

from worker.sources import SourceError, fetch_postings
from worker.sources.adapters import (
    ArbeitnowAdapter,
    GreenhouseAdapter,
    HimalayasAdapter,
    LeverAdapter,
    RemoteOKAdapter,
    RemotiveAdapter,
    parse_salary_text,
)
from worker.sources.base import HttpClient, JobPosting, parse_datetime
from worker.text import html_to_text

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def client_for(routes: dict[str, object], calls: list | None = None) -> HttpClient:
    """HttpClient backed by a mock transport that serves fixture JSON by URL host+path."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        key = f"{request.url.host}{request.url.path}"
        if key not in routes:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=routes[key])

    return HttpClient(transport=httpx.MockTransport(handler), min_interval=0, sleep=lambda s: None)


# --- parsing -----------------------------------------------------------------


def test_remotive_parse_maps_fields_and_salary():
    job = load("remotive")["jobs"][0]
    p = RemotiveAdapter.parse(job)
    assert p.source == "remotive"
    assert p.external_id == str(job["id"])
    assert p.url.startswith("https://remotive.com/")
    assert p.remote is True
    assert p.location == job["candidate_required_location"]
    assert p.posted_at is not None and p.posted_at.tzinfo is not None
    assert "<" not in p.description


def test_remoteok_skips_legal_notice_row():
    data = load("remoteok")
    postings = RemoteOKAdapter(client_for({"remoteok.com/api": data})).fetch({})
    assert len(postings) == len(data) - 1
    assert all(p.title for p in postings)


def test_arbeitnow_remote_only_filter_defaults_on():
    data = load("arbeitnow")
    http = client_for({"www.arbeitnow.com/api/job-board-api": {**data, "links": {"next": None}}})
    remote = ArbeitnowAdapter(http).fetch({})
    everything = ArbeitnowAdapter(http).fetch({"remote_only": False})
    assert all(p.remote for p in remote)
    assert len(everything) == len(data["data"]) > len(remote)


def test_greenhouse_unescapes_html_content():
    p = GreenhouseAdapter.parse(load("greenhouse")["jobs"][0])
    assert p.company
    assert "&lt;" not in p.description and "<div" not in p.description
    assert p.url.startswith("https://")


def test_lever_parses_salary_range_and_workplace():
    p = LeverAdapter.parse(load("lever")[0], "leverdemo")
    assert p.company == "leverdemo"
    assert p.salary_min == 85000 and p.salary_max == 175000
    assert p.remote is False  # hybrid


def test_himalayas_location_restrictions_become_location():
    p = HimalayasAdapter.parse(load("himalayas")["jobs"][0])
    assert p.remote is True
    assert p.location  # either restrictions or "Worldwide"


def test_keyword_filter_applies_to_title_tags_and_description():
    http = client_for({"boards-api.greenhouse.io/v1/boards/acme/jobs": load("greenhouse")})
    assert GreenhouseAdapter(http).fetch({"board": "acme", "keywords": ["zzz-no-match"]}) == []
    assert GreenhouseAdapter(http).fetch({"board": "acme", "keywords": ["anthropic"]})


# --- fetch_postings ------------------------------------------------------------


def test_fetch_postings_dedupes_and_limits():
    data = load("remotive")
    data["jobs"] = data["jobs"] + data["jobs"]  # duplicate every job
    http = client_for({"remotive.com/api/remote-jobs": data})
    postings = fetch_postings({"type": "remotive"}, http=http)
    assert len(postings) == len(data["jobs"]) // 2
    assert len(fetch_postings({"type": "remotive", "max_results": 1}, http=http)) == 1


def test_fetch_postings_rejects_unknown_type():
    with pytest.raises(SourceError, match="Unknown source type"):
        fetch_postings({"type": "monster"}, http=client_for({}))
    with pytest.raises(SourceError, match="needs a 'type'"):
        fetch_postings({}, http=client_for({}))


def test_content_hash_prefers_external_id():
    a = JobPosting(source="x", external_id="1", url="https://a", title="A")
    b = JobPosting(source="x", external_id="1", url="https://b", title="B")
    c = JobPosting(source="y", external_id="1", url="https://a", title="A")
    assert a.content_hash == b.content_hash != c.content_hash


# --- HttpClient ----------------------------------------------------------------


def test_http_client_retries_on_429_then_succeeds():
    responses = iter([httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200, json={"ok": True})])
    sleeps: list[float] = []
    http = HttpClient(
        transport=httpx.MockTransport(lambda r: next(responses)), min_interval=0, sleep=sleeps.append
    )
    assert http.get_json("https://api.example.com/x") == {"ok": True}
    assert sleeps == [2.0]


def test_http_client_does_not_retry_on_404():
    calls: list = []
    http = client_for({}, calls)
    with pytest.raises(SourceError, match="HTTP 404"):
        http.get_json("https://api.example.com/missing")
    assert len(calls) == 1


def test_http_client_gives_up_after_max_retries():
    calls: list = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    http = HttpClient(transport=httpx.MockTransport(handler), max_retries=2, min_interval=0, sleep=lambda s: None)
    with pytest.raises(SourceError, match="after 3 attempts"):
        http.get_json("https://api.example.com/down")
    assert len(calls) == 3


# --- helpers -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$170k - $200k", (170000, 200000)),
        ("80,000-100,000 USD", (80000, 100000)),
        ("$120k", (120000, 120000)),
        ("Competitive", (None, None)),
        (None, (None, None)),
    ],
)
def test_parse_salary_text(text, expected):
    assert parse_salary_text(text) == expected


def test_parse_datetime_handles_seconds_millis_and_iso():
    assert parse_datetime(1792159275).year == 2026
    assert parse_datetime(1788199939703).year == 2026
    assert parse_datetime("2026-10-14T20:33:44").tzinfo is not None
    assert parse_datetime("not a date") is None


def test_html_to_text_keeps_structure_and_drops_scripts():
    html = "<h2>Role</h2><p>Build&nbsp;APIs</p><ul><li>Python</li><li>FastAPI</li></ul><script>x()</script>"
    text = html_to_text(html)
    assert "Role" in text and "- Python" in text and "x()" not in text
    assert html_to_text("&lt;p&gt;escaped&lt;/p&gt;") == "escaped"
