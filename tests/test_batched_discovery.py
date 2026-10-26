"""The cron run scrapes sources inside a time budget, in a rotating order, and resumes the rest next run."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import main
import pytest
from app import discovery
from app.config import settings
from app.database import get_db
from app.models import Run
from app.routes import cron
from app.telegram import digest
from app.telegram.config import telegram_settings
from fastapi.testclient import TestClient

T0 = datetime(2026, 10, 26, 4, 0, tzinfo=UTC)


@dataclass
class FakeSource:
    id: int
    config: dict = field(default_factory=dict)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class World:
    """Fake clock + fake sources; dispatching a source takes `cost` seconds and records a scrape attempt."""

    def __init__(self, n: int, cost: float = 40.0) -> None:
        self.sources = [FakeSource(i) for i in range(1, n + 1)]
        self.clock = FakeClock()
        self.cost = cost
        self.attempts: dict[int, datetime] = {}  # what the `runs` table would say
        self.dispatched: list[int] = []
        self.fail: set[int] = set()
        self._tick = 0

    async def dispatch(self, source_id: int, _config) -> str:
        self._tick += 1
        self.attempts[source_id] = T0 + timedelta(minutes=self._tick)  # a scrape run is written either way
        self.dispatched.append(source_id)
        self.clock.now += self.cost
        if source_id in self.fail:
            raise RuntimeError("board down")
        return f"inline:{source_id}"

    def run(self, max_seconds: float = 100.0) -> tuple[list[dict], list[int]]:
        """One cron invocation: order by the run history, then scrape within the budget."""
        self.clock.now = 0.0
        ordered = discovery.rotation_order(self.sources, dict(self.attempts))
        return asyncio.run(discovery.scrape_within_budget(ordered, self.dispatch, max_seconds=max_seconds, started=0.0))


@pytest.fixture
def world(monkeypatch):
    w = World(5)
    monkeypatch.setattr(discovery, "clock", w.clock)
    return w


def test_rotation_puts_never_attempted_first_then_oldest_attempt():
    sources = [FakeSource(i) for i in (1, 2, 3, 4)]
    last = {1: T0 + timedelta(hours=2), 2: T0, 4: T0 + timedelta(hours=1)}
    assert [s.id for s in discovery.rotation_order(sources, last)] == [3, 2, 4, 1]


def test_stops_starting_sources_once_the_budget_is_spent(world):
    results, skipped = world.run(max_seconds=100)
    # sources start at t=0, 40 and 80; at t=120 the budget is spent
    assert [r["source_id"] for r in results] == [1, 2, 3]
    assert skipped == [4, 5]
    assert world.clock.now == 120  # never started a source at or past the deadline


def test_zero_budget_means_no_limit(world):
    results, skipped = world.run(max_seconds=0)
    assert len(results) == 5 and skipped == []


def test_resumes_with_the_remaining_sources_and_never_double_processes(world):
    _, skipped = world.run(max_seconds=60)  # room for two sources a run
    assert world.dispatched == [1, 2] and skipped == [3, 4, 5]
    world.dispatched.clear()

    _, skipped = world.run(max_seconds=60)
    assert world.dispatched == [3, 4]  # yesterday's leftovers first, nothing repeated
    assert skipped == [5, 1, 2]
    world.dispatched.clear()

    world.run(max_seconds=60)
    assert world.dispatched == [5, 1]  # every source served before any goes twice


def test_a_failing_source_counts_as_attempted_and_does_not_stop_the_others(world):
    world.fail = {1}
    results, _ = world.run(max_seconds=0)
    assert results[0] == {"source_id": 1, "error": "board down"}
    assert [r["source_id"] for r in results[1:]] == [2, 3, 4, 5]
    world.dispatched.clear()
    world.run(max_seconds=0)
    assert world.dispatched == [1, 2, 3, 4, 5]  # attempts were in order, so the rotation keeps it


# ----------------------------------------------------------------- the cron endpoint


class _Rows:
    def __init__(self, rows) -> None:
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class FakeDb:
    """Answers the cron's two queries (enabled sources, last scrape attempts) from the fake world."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.calls = 0
        self.added: list = []

    async def execute(self, *_):
        self.calls += 1
        if self.calls == 1:
            return _Rows(list(self.world.sources))
        return _Rows(list(self.world.attempts.items()))

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        pass


def _call_cron(monkeypatch, world: World) -> tuple[dict, FakeDb]:
    db = FakeDb(world)

    async def fake_db():
        yield db

    world.clock.now = 0.0
    main.app.dependency_overrides[get_db] = fake_db
    try:
        resp = TestClient(main.app).get("/api/cron/discover", headers={"Authorization": "Bearer cron"})
    finally:
        main.app.dependency_overrides.clear()
    assert resp.status_code == 200
    return resp.json(), db


@pytest.fixture
def cron_world(monkeypatch, world):
    monkeypatch.setattr(settings, "cron_secret", "cron")
    monkeypatch.setattr(settings, "discover_max_seconds", 60.0)
    monkeypatch.setattr(settings, "openai_api_key", "")
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(cron, "dispatch_scrape", world.dispatch)
    monkeypatch.setattr(telegram_settings, "bot_token", "123:abc")
    monkeypatch.setattr(telegram_settings, "chat_id", "42")
    world.digests = []

    def fake_digest():
        world.digests.append(world.clock.now)
        return {"sent": 1}

    monkeypatch.setattr(digest, "run_digest", fake_digest)
    return world


def test_cron_records_skipped_sources_and_still_sends_the_digest(monkeypatch, cron_world):
    body, db = _call_cron(monkeypatch, cron_world)
    assert [r["source_id"] for r in body["results"]] == [1, 2]
    assert body["skipped"] == [3, 4, 5]
    assert body["telegram"] == {"sent": 1} and cron_world.digests == [80.0]

    (run,) = db.added
    assert isinstance(run, Run) and run.type == "discover" and run.status == "completed"
    assert run.stats["processed"] == [1, 2] and run.stats["skipped"] == [3, 4, 5]
    assert run.stats["elapsed_seconds"] == 80.0 and run.stats["max_seconds"] == 60.0


def test_cron_resumes_the_remaining_sources_on_the_next_call(monkeypatch, cron_world):
    _call_cron(monkeypatch, cron_world)
    body, _ = _call_cron(monkeypatch, cron_world)
    assert [r["source_id"] for r in body["results"]] == [3, 4]
    assert body["skipped"] == [5, 1, 2]
    assert cron_world.dispatched == [1, 2, 3, 4]  # no source scraped twice before all were scraped once
    assert len(cron_world.digests) == 2


def test_cron_response_is_unchanged_when_everything_fits(monkeypatch, cron_world):
    monkeypatch.setattr(settings, "discover_max_seconds", 0.0)
    body, _ = _call_cron(monkeypatch, cron_world)
    assert "skipped" not in body and len(body["results"]) == 5
