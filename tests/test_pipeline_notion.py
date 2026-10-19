import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx

from worker.ai import ExtractedFields, Extraction, HeuristicExtractor, LLMExtractor, Salary
from worker.notion import SCHEMA, NotionSync, page_properties
from worker.pipeline import LLM_PRESCORE_THRESHOLD, analyze, discover, enrich
from worker.profile import load_profile
from worker.sources.base import HttpClient, JobPosting

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
NOW = datetime(2026, 10, 16, tzinfo=UTC)
PROFILE = load_profile(ROOT / "profile.example.yaml")

GOOD = JobPosting(
    source="t",
    external_id="good",
    url="https://jobs.example.com/good",
    title="Senior Backend Engineer",
    company="Acme",
    location="Worldwide",
    remote=True,
    description="Python, FastAPI, PostgreSQL, Redis, Celery, Docker and LLM agents. " * 10,
    posted_at=NOW,
)
BAD = JobPosting(source="t", external_id="bad", url="https://jobs.example.com/bad", title="Office Manager")


class CountingLLM(LLMExtractor):
    def __init__(self):
        self.calls = 0

    def extract(self, posting):
        self.calls += 1
        return HeuristicExtractor().extract(posting)


def mock_http(handler) -> HttpClient:
    return HttpClient(transport=httpx.MockTransport(handler), min_interval=0, sleep=lambda s: None)


# --- pipeline --------------------------------------------------------------------


def test_llm_is_only_called_for_promising_jobs():
    llm = CountingLLM()
    good = analyze(GOOD, PROFILE, llm, now=NOW)
    bad = analyze(BAD, PROFILE, llm, now=NOW)
    assert llm.calls == 1
    assert good.score > LLM_PRESCORE_THRESHOLD > bad.score


def test_enrich_fills_monthly_non_usd_salary_from_extraction():
    llm_fields = ExtractedFields(salary=Salary(min=4000, max=5000, currency="EUR", period="month"))
    enriched = enrich(GOOD, Extraction(fields=llm_fields, method="llm"))
    assert (enriched.salary_max, enriched.salary_currency, enriched.salary_period) == (5000, "EUR", "month")
    source_salary = replace(GOOD, salary_max=90000, salary_currency="USD")
    assert enrich(source_salary, Extraction(fields=llm_fields, method="llm")) is source_salary  # source wins


def test_analyze_scores_hourly_salary_text_as_annual():
    hourly = analyze(replace(GOOD, salary_text="$45/hr"), PROFILE, HeuristicExtractor(), now=NOW)
    assert hourly.posting.salary_period == "hour"
    assert hourly.match.breakdown["salary"] == 10
    assert "Salary up to $45/hr ≈ $93,600" in hourly.match.reasons


def test_discover_ranks_jobs_and_isolates_failing_sources():
    remotive = json.loads((FIXTURES / "remotive.json").read_text())

    def handler(request):
        if request.url.host == "remotive.com":
            return httpx.Response(200, json=remotive)
        return httpx.Response(500)

    report = discover(
        [{"type": "remotive"}, {"type": "remoteok", "name": "broken"}],
        PROFILE,
        HeuristicExtractor(),
        http=mock_http(handler),
        now=NOW,
    )
    assert report.fetched == len(remotive["jobs"])
    assert "broken" in report.errors
    ranks = [(j.rank, j.score) for j in report.jobs]
    assert ranks == sorted(ranks, reverse=True)
    assert report.per_source == {"remotive": len(remotive["jobs"])}
    assert set(report.source_seconds) == {"remotive", "broken"}


# --- notion ----------------------------------------------------------------------


def test_page_properties_shape_and_status_only_on_create():
    job = analyze(GOOD, PROFILE, HeuristicExtractor(), now=NOW)
    created = page_properties(job, include_status=True)
    updated = page_properties(job, include_status=False)
    assert created["Name"]["title"][0]["text"]["content"] == GOOD.title
    assert created["Score"]["number"] == job.score
    assert created["URL"]["url"] == GOOD.url
    assert created["Status"]["select"]["name"] == "New"
    assert "Status" not in updated
    assert set(updated) - {"Name"} <= set(SCHEMA)


def test_upsert_creates_then_updates_by_url():
    pages: dict[str, str] = {}
    calls: list[tuple[str, str]] = []

    def handler(request):
        calls.append((request.method, request.url.path))
        body = json.loads(request.content or b"{}")
        if request.url.path.endswith("/query"):
            url = body["filter"]["url"]["equals"]
            return httpx.Response(200, json={"results": [{"id": pages[url]}] if url in pages else []})
        if request.method == "POST" and request.url.path == "/v1/pages":
            url = body["properties"]["URL"]["url"]
            pages[url] = f"page-{len(pages)}"
            return httpx.Response(200, json={"id": pages[url]})
        return httpx.Response(200, json={"id": "patched"})

    sync = NotionSync("secret", "db1", http=mock_http(handler))
    job = analyze(GOOD, PROFILE, HeuristicExtractor(), now=NOW)
    assert sync.upsert(job) == ("page-0", True)
    assert sync.upsert(job) == ("page-0", False)
    assert ("PATCH", "/v1/pages/page-0") in calls


def test_sync_skips_low_scores_and_ineligible_jobs():
    created = []

    def handler(request):
        if request.url.path.endswith("/query"):
            return httpx.Response(200, json={"results": []})
        created.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "p"})

    good = analyze(GOOD, PROFILE, HeuristicExtractor(), now=NOW)
    restricted = analyze(
        JobPosting(**{**GOOD.__dict__, "external_id": "us", "url": "https://x/us", "location": "USA only"}),
        PROFILE,
        HeuristicExtractor(),
        now=NOW,
    )
    stats = NotionSync("s", "db", http=mock_http(handler)).sync([good, restricted], min_score=50)
    assert stats == {"created": 1, "updated": 0, "skipped": 1}


def test_setup_database_adds_only_missing_properties():
    patched = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"properties": {"Name": {}, "Score": {}, "URL": {}}})
        patched.update(json.loads(request.content)["properties"])
        return httpx.Response(200, json={})

    added = NotionSync("s", "db", http=mock_http(handler)).setup_database()
    assert "Score" not in added and "Company" in added
    assert set(patched) == set(added)


def test_notion_headers_carry_version_and_token():
    seen = SimpleNamespace(headers=None)

    def handler(request):
        seen.headers = request.headers
        return httpx.Response(200, json={"results": []})

    NotionSync("tok", "db", http=mock_http(handler)).find_page("https://x")
    assert seen.headers["Authorization"] == "Bearer tok"
    assert seen.headers["Notion-Version"]


def test_same_job_on_two_boards_is_scored_once_keeping_longer_description():
    from worker.pipeline import dedupe_across_sources

    a = JobPosting(source="remotive", external_id="1", url="https://a", title="Senior Shopify Developer",
                   company="Sanctuary Computer Inc", description="short")
    b = JobPosting(source="remoteok", external_id="9", url="https://b", title="Senior Shopify Developer",
                   company="Sanctuary Computer", description="a much longer description")
    c = JobPosting(source="remoteok", external_id="7", url="https://c", title="Backend Engineer", company="Other")
    kept = dedupe_across_sources([a, b, c])
    assert {p.url for p in kept} == {"https://b", "https://c"}


def test_off_target_title_gets_half_skill_credit():
    from worker.matching import score_job

    on = score_job(GOOD, PROFILE, now=NOW)
    off = score_job(JobPosting(**{**GOOD.__dict__, "title": "Senior React Developer"}), PROFILE, now=NOW)
    assert off.breakdown["skills"] == round(on.breakdown["skills"] / 2, 1)
    assert off.score < on.score
