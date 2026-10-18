import main
from app.config import settings
from app.database import async_database_url, get_db, sync_database_url
from fastapi.testclient import TestClient


def test_neon_style_url_is_adapted_for_asyncpg_and_psycopg2():
    url, connect_args = async_database_url("postgresql://u:p@ep-x.neon.tech/db?sslmode=require&channel_binding=require")
    assert url.drivername == "postgresql+asyncpg"
    assert "sslmode" not in url.query and "channel_binding" not in url.query
    assert connect_args == {"ssl": "require"}
    assert sync_database_url("postgres://u:p@h/db?sslmode=require").drivername == "postgresql+psycopg2"


def test_local_url_keeps_defaults():
    url, connect_args = async_database_url("postgresql+asyncpg://postgres:postgres@localhost:5432/jobs")
    assert url.drivername == "postgresql+asyncpg" and connect_args == {}


class _FakeResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _FakeSession:
    async def execute(self, *_):
        return _FakeResult()

    async def commit(self):
        pass


def _client(monkeypatch, secret: str) -> TestClient:
    async def fake_db():
        yield _FakeSession()

    monkeypatch.setattr(settings, "cron_secret", secret)
    main.app.dependency_overrides[get_db] = fake_db
    return TestClient(main.app)  # no `with`: skip lifespan, which needs a database


def test_cron_rejects_missing_or_wrong_secret(monkeypatch):
    client = _client(monkeypatch, "s3cret")
    assert client.get("/api/cron/discover").status_code == 401
    assert client.get("/api/cron/discover", headers={"Authorization": "Bearer nope"}).status_code == 401
    main.app.dependency_overrides.clear()


def test_cron_is_disabled_when_no_secret_configured(monkeypatch):
    client = _client(monkeypatch, "")
    assert client.get("/api/cron/discover", headers={"Authorization": "Bearer "}).status_code == 401
    main.app.dependency_overrides.clear()


def test_cron_runs_with_correct_secret(monkeypatch):
    client = _client(monkeypatch, "s3cret")
    resp = client.get("/api/cron/discover", headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200 and resp.json() == {"sources": 0, "results": []}
    main.app.dependency_overrides.clear()
