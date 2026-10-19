"""Full descriptions for excerpt-only sources: extraction, robots.txt, budget, fallbacks. No network."""

import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from worker.ai import HeuristicExtractor
from worker.matching import score_job
from worker.pipeline import discover
from worker.profile import load_profile
from worker.sources.adapters import HimalayasAdapter, RemoteOKAdapter
from worker.sources.base import HttpClient, JobPosting, SourceError
from worker.sources.fulltext import (
    FetchBudget,
    FullTextOptions,
    RobotsCache,
    extract_job_text,
    fetch_full_descriptions,
    jsonld_description,
    main_block_text,
)

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).resolve().parent.parent
HIMALAYAS_URL = (
    "https://himalayas.app/companies/guidepoint-security-llc/jobs/"
    "security-engineer-corporate-information-security-remote-anywhere-in-the-u-s"
)
REMOTEOK_URL = "https://remoteOK.com/remote-jobs/remote-sr-react-native-somnio-software-1137523"
ALLOW_ALL = "User-agent: *\nAllow: /\n"


def route(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.hostname}{parts.path}"


def page(name: str) -> str:
    return (FIXTURES / "pages" / f"{name}.html").read_text()


def himalayas_excerpt_posting() -> JobPosting:
    """The fixture job as the API returns it when only the excerpt is filled in."""
    raw = json.loads((FIXTURES / "himalayas.json").read_text())["jobs"][0]
    return HimalayasAdapter.parse({**raw, "description": None})


def remoteok_posting() -> JobPosting:
    return RemoteOKAdapter.parse(json.loads((FIXTURES / "remoteok.json").read_text())[3])


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def site(routes: dict[str, object], calls: list | None = None, clock: FakeClock | None = None, cost: float = 0.0):
    """HttpClient whose transport serves `routes` keyed by host+path (str body, int status, or exception).

    Each request advances the fake clock by `cost` seconds, to simulate slow pages.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        if clock is not None:
            clock.now += cost
        key = f"{request.url.host}{request.url.path}"
        value = routes.get(key, 404)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, int):
            return httpx.Response(value, text="error")
        return httpx.Response(200, text=value, headers={"Content-Type": "text/html; charset=utf-8"})

    return HttpClient(transport=httpx.MockTransport(handler), min_interval=0, sleep=lambda s: None)


def run(postings, http, clock: FakeClock | None = None, **options):
    clock = clock or FakeClock()
    return fetch_full_descriptions(
        postings,
        http,
        FullTextOptions(**options),
        robots=RobotsCache(clock=clock),
        clock=clock,
        sleep=clock.sleep,
    )


# --- extraction --------------------------------------------------------------


def test_jsonld_jobposting_description_is_preferred():
    html = page("himalayas_jobposting")
    text = extract_job_text(html)
    assert text == jsonld_description(html)
    assert "Own the ETL that moves security logs" in text
    assert "We’re a remote-first team" in text  # JSON \u escape and HTML entities decoded
    assert "Sign up to see the full description" not in text  # the visible teaser is ignored
    assert "<" not in text and "&lt;" not in text


def test_jsonld_in_graph_or_list_and_type_lists_are_found():
    desc = "<p>Build agents with Python.</p>"
    graph = {"@graph": [{"@type": "Organization"}, {"@type": ["JobPosting"], "description": desc}]}
    for data in (graph, [{"@type": "WebPage"}, {"@type": "JobPosting", "description": desc}]):
        html = f'<script type="application/ld+json">{json.dumps(data)}</script><div><p>other</p></div>'
        assert jsonld_description(html) == "Build agents with Python."


def test_invalid_jsonld_falls_back_to_largest_block():
    html = '<script type="application/ld+json">{not json</script>' + page("remoteok_page")
    assert extract_job_text(html).startswith("About the project")


def test_largest_block_skips_navigation_sidebar_links_and_footer():
    text = main_block_text(page("remoteok_page"))
    assert text.startswith("About the project")
    assert "Build new features in React Native with TypeScript" in text
    assert text.endswith("Contract: full time, USD, remote from LATAM.")
    for noise in ("Related jobs", "Senior Python Developer", "Post a job", "Apply for this job", "Remote OK ©"):
        assert noise not in text


def test_block_text_drops_boilerplate_nested_inside_the_chosen_block():
    html = (
        "<div><p>Intro paragraph about the role and the team behind it.</p>"
        "<nav><a href='/'>Home</a> Breadcrumbs</nav>"
        "<p>Requirements: Python, Redis and a lot of curiosity about distributed systems.</p></div>"
    )
    text = main_block_text(html)
    assert "Breadcrumbs" not in text and "Home" not in text
    assert "Intro paragraph" in text and "Requirements: Python" in text


def test_unclosed_element_inside_boilerplate_does_not_hide_the_rest_of_the_page():
    html = (
        "<form><button>Apply<input></form>"
        "<div><p>Requirements: Python, Redis and a lot of curiosity about distributed systems.</p></div>"
    )
    text = main_block_text(html)
    assert text == "Requirements: Python, Redis and a lot of curiosity about distributed systems."


def test_extraction_survives_unclosed_tags():
    html = "<html><body><div class='job'><p>Senior Python role<p>FastAPI and Postgres<div>Remote"
    assert "FastAPI and Postgres" in extract_job_text(html)


# --- selection and replacement -----------------------------------------------


def test_short_description_is_replaced_and_skills_are_counted():
    posting = himalayas_excerpt_posting()
    http = site({"himalayas.app/robots.txt": ALLOW_ALL, route(HIMALAYAS_URL): page("himalayas_jobposting")})
    [enriched], stats = run([posting], http)
    assert posting.description.startswith("GuidePoint Security provides") and len(posting.description) < 200
    assert "Write Python tooling" in enriched.description
    assert stats.as_dict() == {
        "candidates": 1, "fetched": 1, "failed": 0, "skipped": 0,
        "skipped_budget": 0, "skipped_robots": 0, "skipped_host": 0,
    }  # fmt: skip

    profile = load_profile(ROOT / "profile.example.yaml")
    assert score_job(posting, profile).matched_skills == []
    assert score_job(enriched, profile).matched_skills == ["python", "postgres", "etl", "docker", "aws", "gcp"]


def test_remoteok_page_without_jsonld_adds_skills():
    posting = remoteok_posting()
    http = site({"remoteok.com/robots.txt": ALLOW_ALL, route(REMOTEOK_URL): page("remoteok_page")})
    [enriched], stats = run([posting], http)
    assert stats.fetched == 1
    profile = load_profile(ROOT / "profile.example.yaml")
    before, after = score_job(posting, profile).matched_skills, score_job(enriched, profile).matched_skills
    assert before == ["python", "node"]  # from the API tags
    assert after == ["python", "docker", "node", "typescript", "aws"]


def test_only_short_postings_from_listed_sources_are_fetched():
    calls: list[httpx.Request] = []
    http = site({"himalayas.app/robots.txt": ALLOW_ALL}, calls)
    long_text = replace(himalayas_excerpt_posting(), description="x" * 2000)
    other_source = replace(himalayas_excerpt_posting(), source="greenhouse")
    no_url = replace(himalayas_excerpt_posting(), url="mailto:jobs@example.com")
    postings = [long_text, other_source, no_url]
    out, stats = run(postings, http)
    assert out == postings and calls == [] and stats.candidates == 0


def test_min_chars_zero_disables_fetching():
    calls: list[httpx.Request] = []
    out, stats = run([himalayas_excerpt_posting()], site({}, calls), min_chars=0)
    assert calls == [] and stats.candidates == 0


def test_page_with_less_text_than_the_excerpt_keeps_the_excerpt():
    posting = himalayas_excerpt_posting()
    http = site({"himalayas.app/robots.txt": ALLOW_ALL, route(HIMALAYAS_URL): "<p>Apply now</p>"})
    [kept], stats = run([posting], http)
    assert kept == posting and stats.failed == 1 and stats.fetched == 0


# --- failures ----------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [500, 403, httpx.ConnectTimeout("timed out"), httpx.TooManyRedirects("redirect loop")],
    ids=["http-500", "http-403-challenge", "timeout", "redirect-loop"],
)
def test_fetch_failure_keeps_the_api_excerpt(failure):
    posting = himalayas_excerpt_posting()
    http = site({"himalayas.app/robots.txt": ALLOW_ALL, route(HIMALAYAS_URL): failure})
    [kept], stats = run([posting], http)
    assert kept == posting
    assert stats.failed == 1 and stats.fetched == 0


def test_host_that_keeps_failing_is_skipped_for_the_rest_of_the_run():
    calls: list[httpx.Request] = []
    base = himalayas_excerpt_posting()
    postings = [replace(base, url=f"https://himalayas.app/companies/x/jobs/{i}", external_id=str(i)) for i in range(4)]
    http = site({"himalayas.app/robots.txt": ALLOW_ALL}, calls)  # every page 404s
    out, stats = run(postings, http)
    assert out == postings
    assert (stats.failed, stats.skipped_host) == (2, 2)
    assert sum(1 for r in calls if r.url.path != "/robots.txt") == 2


# --- robots.txt --------------------------------------------------------------


def test_robots_disallow_skips_the_page():
    calls: list[httpx.Request] = []
    robots = "User-agent: *\nAllow: /\n\nUser-agent: FirstFetch\nDisallow: /companies/\n"
    http = site({"himalayas.app/robots.txt": robots, route(HIMALAYAS_URL): page("himalayas_jobposting")}, calls)
    [kept], stats = run([himalayas_excerpt_posting()], http)
    assert kept.description.startswith("GuidePoint Security provides")
    assert stats.skipped_robots == 1 and stats.skipped == 1
    assert [r.url.path for r in calls] == ["/robots.txt"]


@pytest.mark.parametrize(("status", "allowed"), [(404, True), (403, True), (503, False)])
def test_robots_status_handling_follows_rfc_9309(status, allowed):
    """4xx: no rules, fetch freely. 5xx/unreachable: assume everything is disallowed."""
    http = site({"himalayas.app/robots.txt": status})
    assert RobotsCache().check(HIMALAYAS_URL, http) == (allowed, 0.0)


def test_robots_is_fetched_once_per_host_and_cached():
    calls: list[httpx.Request] = []
    http = site({"himalayas.app/robots.txt": ALLOW_ALL}, calls)
    cache = RobotsCache()
    for i in range(3):
        cache.check(f"https://himalayas.app/companies/x/jobs/{i}", http)
    assert [r.url.path for r in calls] == ["/robots.txt"]


def test_robots_cache_expires():
    calls: list[httpx.Request] = []
    clock = FakeClock()
    http = site({"himalayas.app/robots.txt": ALLOW_ALL}, calls)
    cache = RobotsCache(ttl=60, clock=clock)
    cache.check(HIMALAYAS_URL, http)
    clock.now += 61
    cache.check(HIMALAYAS_URL, http)
    assert len(calls) == 2


def test_crawl_delay_is_honored_between_pages_on_the_same_host():
    clock = FakeClock()
    base = himalayas_excerpt_posting()
    postings = [replace(base, url=f"https://himalayas.app/companies/x/jobs/{i}", external_id=str(i)) for i in range(2)]
    routes = {"himalayas.app/robots.txt": "User-agent: *\nCrawl-delay: 3\nAllow: /\n"}
    routes.update({f"himalayas.app/companies/x/jobs/{i}": page("himalayas_jobposting") for i in range(2)})
    _, stats = run(postings, site(routes), clock, max_seconds=60)
    assert stats.fetched == 2
    assert clock.sleeps == [3.0]


# --- budget ------------------------------------------------------------------


def _many(n: int) -> tuple[list[JobPosting], dict[str, str]]:
    base = himalayas_excerpt_posting()
    postings = [replace(base, url=f"https://himalayas.app/companies/x/jobs/{i}", external_id=str(i)) for i in range(n)]
    routes = {"himalayas.app/robots.txt": ALLOW_ALL}
    routes.update({f"himalayas.app/companies/x/jobs/{i}": page("himalayas_jobposting") for i in range(n)})
    return postings, routes


def test_page_budget_caps_fetches():
    postings, routes = _many(5)
    calls: list[httpx.Request] = []
    out, stats = run(postings, site(routes, calls), max_pages=2)
    assert (stats.fetched, stats.skipped_budget) == (2, 3)
    assert [p.description.startswith("GuidePoint Security provides trusted") for p in out] == [True] * 5
    assert ["Write Python tooling" in p.description for p in out] == [True, True, False, False, False]
    assert sum(1 for r in calls if r.url.path != "/robots.txt") == 2


def test_time_budget_stops_fetching_with_a_fake_clock():
    """Every request takes 8 s of a 20 s budget."""
    postings, routes = _many(5)
    clock = FakeClock()
    calls: list[httpx.Request] = []
    _, stats = run(postings, site(routes, calls, clock, cost=8), clock, max_seconds=20, max_pages=100)
    # robots.txt (8 s) + page 1 (8 s) = 16 s, 4 s left -> page 2 = 24 s -> out of time
    assert (stats.fetched, stats.skipped_budget) == (2, 3)
    timeouts = [r.extensions["timeout"]["read"] for r in calls if r.url.path != "/robots.txt"]
    assert timeouts == [6.0, 2.0]  # remaining time split across the attempt and its retry


def test_budget_counts_from_creation():
    clock = FakeClock()
    budget = FetchBudget(max_pages=1, max_seconds=10, clock=clock)
    assert budget.allows_page()
    clock.now += 8.5
    assert not budget.allows_page()  # 1.5 s left is below the floor


def test_options_from_settings_parse_the_source_list():
    class S:
        full_description_sources = " RemoteOK, himalayas ,,"
        full_description_min_chars = 900
        full_description_max_pages = 4
        full_description_max_seconds = 12.5

    options = FullTextOptions.from_settings(S())
    expected = FullTextOptions(
        min_chars=900, max_pages=4, max_seconds=12.5, sources=frozenset({"remoteok", "himalayas"})
    )
    assert options == expected


# --- wiring ------------------------------------------------------------------


def test_get_text_sends_html_accept_and_polite_user_agent():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="<p>hi</p>")

    http = HttpClient(transport=httpx.MockTransport(handler), min_interval=0, sleep=lambda s: None)
    assert http.get_text("https://example.com/job") == "<p>hi</p>"
    assert seen[0].headers["Accept"].startswith("text/html")
    assert seen[0].headers["User-Agent"].startswith("FirstFetch/")


def test_source_error_carries_the_http_status():
    http = site({})
    with pytest.raises(SourceError) as exc:
        http.get_text("https://example.com/missing")
    assert exc.value.status == 404


def test_discover_fetches_full_descriptions_within_one_budget():
    data = json.loads((FIXTURES / "remoteok.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api":
            return httpx.Response(200, json=data)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ALLOW_ALL)
        return httpx.Response(200, text=page("remoteok_page"))

    http = HttpClient(transport=httpx.MockTransport(handler), min_interval=0, sleep=lambda s: None)
    profile = load_profile(ROOT / "profile.example.yaml")
    rules = HeuristicExtractor()
    report = discover([{"type": "remoteok"}], profile, rules, http=http, full_text=FullTextOptions(max_pages=2))
    assert report.full_descriptions.candidates == 3
    assert (report.full_descriptions.fetched, report.full_descriptions.skipped_budget) == (2, 1)
    without = discover([{"type": "remoteok"}], profile, rules, http=http)
    assert without.full_descriptions.candidates == 0


def test_redirect_loop_fails_fast():
    """Some bot walls answer every page with a redirect to itself; give up after a few hops."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://remoteok.com/"})

    http = HttpClient(transport=httpx.MockTransport(handler), min_interval=0, sleep=lambda s: None)
    with pytest.raises(SourceError, match="redirects"):
        http.get_text("https://remoteok.com/remote-jobs/x")
    assert len(seen) == 6  # the request plus 5 redirects, no retries
