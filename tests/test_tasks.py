import json
from contextlib import contextmanager
from pathlib import Path

import httpx
from app.models import JobRaw, Run, Source

from worker import tasks
from worker.sources import HttpClient
from worker.sources.adapters import GreenhouseAdapter, RemoteOKAdapter
from worker.tasks import _full_text_options, _meta, _to_posting, settings

FIXTURES = Path(__file__).parent / "fixtures"


def test_updated_at_round_trips_through_jobs_raw_meta():
    """updated_at has no column; it's stored in jobs_raw.meta as an ISO string (JSON-safe)."""
    posting = GreenhouseAdapter.parse(json.loads((FIXTURES / "greenhouse.json").read_text())["jobs"][0])
    meta = _meta(posting)
    assert meta["updated_at"] == posting.updated_at.isoformat()
    json.dumps(meta)  # JSONB needs plain values
    row = JobRaw(
        url=posting.url,
        title=posting.title,
        company=posting.company,
        location=posting.location,
        description=posting.description,
        posted_at=posting.posted_at,
        meta=meta,
    )
    restored = _to_posting(row)
    assert restored.posted_at == posting.posted_at
    assert restored.updated_at == posting.updated_at


def test_rows_stored_before_updated_at_existed_still_load():
    row = JobRaw(url="https://example.com/1", title="Engineer", meta={"source": "greenhouse"})
    assert _to_posting(row).updated_at is None


def test_full_text_options_follow_settings_and_per_source_override(monkeypatch):
    monkeypatch.setattr(settings, "full_description_sources", "remoteok")
    monkeypatch.setattr(settings, "full_description_max_pages", 3)
    default = _full_text_options({"type": "himalayas"})
    assert default.sources == {"remoteok"} and default.max_pages == 3
    assert "himalayas" in _full_text_options({"type": "himalayas", "full_descriptions": True}).sources
    assert _full_text_options({"type": "remoteok", "full_descriptions": False}).min_chars == 0


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _Session:
    """Just enough of a SQLAlchemy session for run_scrape_task: one source, some stored hashes."""

    def __init__(self, source: Source, stored: dict[str, JobRaw]) -> None:
        self.source, self.stored, self.added = source, stored, []

    def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        if entity is Source:
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


def test_scrape_fetches_full_descriptions_for_new_postings_only(monkeypatch):
    api = json.loads((FIXTURES / "remoteok.json").read_text())
    page_html = (FIXTURES / "pages" / "remoteok_page.html").read_text()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/api":
            return httpx.Response(200, json=api)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, text=page_html)

    postings = [RemoteOKAdapter.parse(j) for j in api[1:]]
    stored = {postings[0].content_hash: JobRaw(id=99, url=postings[0].url, title="t", meta={})}
    source = Source(id=1, name="RemoteOK", base_url="https://remoteok.com", config={"type": "remoteok"})
    session = _Session(source, stored)

    @contextmanager
    def fake_session():
        yield session

    monkeypatch.setattr(tasks, "get_sync_session", fake_session)
    monkeypatch.setattr(
        tasks,
        "HttpClient",
        lambda: HttpClient(transport=httpx.MockTransport(handler), min_interval=0, sleep=lambda s: None),
    )
    monkeypatch.setattr(settings, "queue_mode", "inline")
    monkeypatch.setattr(tasks, "run_analyze_task", lambda ids: {"processed": len(ids)})

    result = tasks.run_scrape_task(1)

    assert result["inserted"] == 2
    page_paths = [p for p in requested if p not in ("/api", "/robots.txt")]
    assert len(page_paths) == 2 and postings[0].url.split("remoteOK.com")[1] not in page_paths
    run = next(o for o in session.added if isinstance(o, Run))
    assert run.stats["full_descriptions"]["fetched"] == 2
    assert run.stats["full_descriptions"]["candidates"] == 2
    rows = [o for o in session.added if isinstance(o, JobRaw)]
    assert all("React Native with TypeScript" in r.description for r in rows)
