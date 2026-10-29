"""Djinni job pages: who may apply, experience and languages, and the budgeted fetch. No network.

Fixtures are trimmed copies of real pages (October 2026) for the three shapes Djinni uses:
an EU region, a country (Ukraine), and no requirement at all (worldwide).
"""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import httpx
from app.models import JobRaw, Run, Source

from worker import tasks
from worker.config import settings
from worker.hire import blockers
from worker.matching import location_eligibility
from worker.profile import CandidateProfile
from worker.sources.base import HttpClient, JobPosting
from worker.sources.djinni_page import (
    CHECKED,
    DjinniPageOptions,
    apply_details,
    fetch_djinni_details,
    parse_djinni_page,
)
from worker.sources.fulltext import RobotsCache

PAGES = Path(__file__).parent / "fixtures" / "pages"
ALLOW_ALL = "User-agent: *\nDisallow: /q\n"


def page(name: str) -> str:
    return (PAGES / f"{name}.html").read_text()


def posting(job_id: int = 855643, **kw) -> JobPosting:
    return JobPosting(
        source="djinni",
        external_id=str(job_id),
        url=f"https://djinni.co/jobs/{job_id}-python-developer/",
        title="Python Developer",
        description="We build AI products.",
        remote=True,
        **kw,
    )


def client(routes: dict[str, object], calls: list | None = None) -> HttpClient:
    """Serves `routes` by URL path: a str body, an int status, or an exception to raise."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        body = routes.get(request.url.path, 404)
        if isinstance(body, Exception):
            raise body
        if isinstance(body, int):
            return httpx.Response(body)
        return httpx.Response(200, text=body)

    return HttpClient(transport=httpx.MockTransport(handler), min_interval=0, max_retries=0, sleep=lambda s: None)


def fetch(postings, routes, calls=None, options=DjinniPageOptions()):
    return fetch_djinni_details(
        postings, client({"/robots.txt": ALLOW_ALL, **routes}, calls), options, robots=RobotsCache()
    )


# --- parsing -------------------------------------------------------------------------------------


def test_europe_region_page():
    d = parse_djinni_page(page("djinni_europe"))
    assert d.location == "Countries of Europe or Ukraine"
    assert d.regions == ["eu"]
    assert d.experience_years_min == 5
    assert d.languages == {"English": "B2 - Upper Intermediate", "Ukrainian": "Native"}


def test_ukraine_country_page():
    d = parse_djinni_page(page("djinni_ukraine"))
    assert d.location == "Ukraine"
    assert d.countries == ["ua"]
    assert d.experience_years_min == 3


def test_worldwide_page_has_no_requirement():
    d = parse_djinni_page(page("djinni_worldwide"))
    assert d.location == "Worldwide"
    assert d.regions == ["global"]
    assert d.experience_years_min == 4
    assert "Ukrainian" not in d.languages


def test_country_list_in_json_ld_without_sidebar_label():
    html = (
        '<script type="application/ld+json">{"@type": "JobPosting", "applicantLocationRequirements": '
        '{"@type": "AdministrativeArea", "address": {"addressCountry": "PRT, ESP, ITA"}}}</script>'
    )
    d = parse_djinni_page(html)
    assert d.location == "Portugal, Spain, Italy"
    assert d.countries == ["pt", "es", "it"]


def test_worldwide_from_json_ld_alone_when_label_missing():
    html = (
        '<script type="application/ld+json">{"@type": "JobPosting", "title": "AI Engineer"}</script>'
        "<div>Full Remote</div><div>Countries where we consider candidates</div>"
    )
    assert parse_djinni_page(html).location == "Worldwide"


def test_not_a_job_page():
    assert parse_djinni_page("<html><body>Just a moment...</body></html>") is None


# --- effect on eligibility and blockers ----------------------------------------------------------


def test_strict_mode_keeps_eu_and_ukraine_out_and_lets_worldwide_in():
    profile = CandidateProfile()
    europe = apply_details(posting(), parse_djinni_page(page("djinni_europe")))
    ukraine = apply_details(posting(), parse_djinni_page(page("djinni_ukraine")))
    worldwide = apply_details(posting(), parse_djinni_page(page("djinni_worldwide")))
    assert location_eligibility(europe, profile)[0] is False
    assert location_eligibility(ukraine, profile)[0] is False
    assert location_eligibility(worldwide, profile)[0] is True


def test_ukrainian_native_and_experience_become_blockers():
    profile = CandidateProfile(years_experience=2)
    p = apply_details(posting(), parse_djinni_page(page("djinni_ukraine")))
    assert p.extra["experience_years_min"] == 3 and p.extra[CHECKED] is True
    assert any("Ukrainian" in b for b in blockers(p, profile))
    assert not any(
        "Ukrainian" in b
        for b in blockers(apply_details(posting(), parse_djinni_page(page("djinni_worldwide"))), profile)
    )


def test_apply_details_is_idempotent_for_the_languages_line():
    d = parse_djinni_page(page("djinni_europe"))
    once = apply_details(posting(), d)
    twice = apply_details(replace(once, extra={}), d)
    assert twice.description.count("Required languages:") == 1


# --- fetching ------------------------------------------------------------------------------------


def test_fetch_reads_each_new_djinni_page():
    jobs = [posting(1), posting(2)]
    out, stats = fetch(
        jobs,
        {"/jobs/1-python-developer/": page("djinni_worldwide"), "/jobs/2-python-developer/": page("djinni_europe")},
    )
    assert [p.location for p in out] == ["Worldwide", "Countries of Europe or Ukraine"]
    assert stats.as_dict() == {"candidates": 2, "read": 2, "failed": 0, "skipped": 0}


def test_failed_fetch_leaves_posting_unchanged_and_unmarked():
    job = posting(1)
    out, stats = fetch([job], {"/jobs/1-python-developer/": 503})
    assert out[0] is job and CHECKED not in out[0].extra
    assert stats.failed == 1


def test_network_error_is_not_fatal():
    job = posting(1)
    out, stats = fetch([job], {"/jobs/1-python-developer/": httpx.ConnectError("boom")})
    assert out == [job] and stats.failed == 1


def test_already_checked_and_other_sources_are_skipped():
    checked = posting(1, extra={CHECKED: True})
    other = replace(posting(2), source="remoteok", url="https://remoteok.com/remote-jobs/2")
    calls: list[str] = []
    out, stats = fetch([checked, other], {}, calls)
    assert out == [checked, other] and stats.candidates == 0 and calls == []


def test_page_budget_caps_fetches():
    jobs = [posting(i) for i in range(1, 5)]
    routes = {f"/jobs/{i}-python-developer/": page("djinni_worldwide") for i in range(1, 5)}
    calls: list[str] = []
    out, stats = fetch(jobs, routes, calls, DjinniPageOptions(max_pages=2, max_seconds=60))
    assert stats.read == 2 and stats.skipped == 2
    assert [p.location for p in out] == ["Worldwide", "Worldwide", None, None]


def test_stops_after_repeated_failures():
    jobs = [posting(i) for i in range(1, 5)]
    calls: list[str] = []
    _, stats = fetch(jobs, {}, calls)  # every page 404s
    assert stats.failed == 2 and stats.skipped == 2
    assert len([c for c in calls if c != "/robots.txt"]) == 2


# --- scrape task: new jobs and stored ones without a location -------------------------------------


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    def __init__(self, source: Source, stored: dict[str, JobRaw]) -> None:
        self.source, self.stored, self.added = source, stored, []

    def execute(self, stmt):
        if stmt.column_descriptions[0]["entity"] is Source:
            return _Result(self.source)
        content_hash = next(c.right.value for c in stmt.whereclause.clauses if c.left.name == "content_hash")
        return _Result(self.stored.get(content_hash))

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        for i, obj in enumerate(self.added, start=1):
            obj.id = obj.id or i

    def commit(self):
        pass


RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Python Developer</title><link>https://djinni.co/jobs/1-python-developer/</link>
<description>New job.</description><pubDate>Tue, 28 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Python Developer</title><link>https://djinni.co/jobs/2-python-developer/</link>
<description>Stored job.</description><pubDate>Mon, 27 Oct 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""


def test_scrape_fills_new_jobs_and_backfills_stored_ones(monkeypatch):
    routes = {
        "/jobs/rss/": RSS,
        "/robots.txt": ALLOW_ALL,
        "/jobs/1-python-developer/": page("djinni_worldwide"),
        "/jobs/2-python-developer/": page("djinni_ukraine"),
    }
    stored_posting = posting(2)
    stored = {
        stored_posting.content_hash: JobRaw(
            id=42,
            url=stored_posting.url,
            title="Python Developer",
            location=None,
            description="Stored job.",
            meta={"source": "djinni", "external_id": "2"},
        )
    }
    source = Source(id=1, name="Djinni", base_url="https://djinni.co", config={"type": "djinni"})
    session = _Session(source, stored)

    @contextmanager
    def fake_session():
        yield session

    analyzed: list[list[int]] = []
    monkeypatch.setattr(tasks, "get_sync_session", fake_session)
    monkeypatch.setattr(tasks, "HttpClient", lambda: client(routes))
    monkeypatch.setattr(tasks.djinni_page, "_default_robots", RobotsCache())
    monkeypatch.setattr(settings, "queue_mode", "inline")
    monkeypatch.setattr(tasks, "run_analyze_task", lambda ids: analyzed.append(ids) or {"processed": len(ids)})

    result = tasks.run_scrape_task(1)

    assert result["inserted"] == 1
    new_row = next(o for o in session.added if isinstance(o, JobRaw))
    assert new_row.location == "Worldwide" and new_row.meta["extra"][CHECKED] is True
    backfilled = stored[stored_posting.content_hash]
    assert backfilled.location == "Ukraine" and backfilled.meta["countries"] == ["ua"]
    assert "Ukrainian (Native)" in backfilled.description
    assert analyzed == [[new_row.id, 42]]
    run = next(o for o in session.added if isinstance(o, Run))
    assert run.stats["djinni_pages"] == {"candidates": 2, "read": 2, "failed": 0, "skipped": 0, "updated": 1}
