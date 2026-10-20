"""Tracker, talent-network and Telegram webhook endpoints, including the admin-token rules."""

import main
import pytest
from app import talent, tracker
from app.config import settings as app_settings
from app.database import get_db
from app.db_sync import get_sync_db
from app.routes.jobs import status_filter
from app.routes.telegram import get_client
from app.telegram.config import telegram_settings
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from tests.tracker_fixtures import OWNER, SECRET, add_job, message_update, oid, settings

HOOK = "/api/telegram/webhook"
ADMIN = {"X-Admin-Token": "adm"}


@pytest.fixture
def client(engine, fake_tg, monkeypatch):
    def db():
        with Session(engine, expire_on_commit=False) as s:
            yield s
            s.commit()

    def tg():
        yield fake_tg.client()

    for key, value in settings().model_dump().items():
        monkeypatch.setattr(telegram_settings, key, value)
    monkeypatch.setattr(app_settings, "admin_token", "adm")
    main.app.dependency_overrides[get_sync_db] = db
    main.app.dependency_overrides[get_client] = tg
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


@pytest.fixture
def admin(client):
    """The same app, every request carrying the admin token."""
    client.headers.update(ADMIN)
    return client


# ----------------------------------------------------------------- applications


def test_applications_crud(admin, session):
    client = admin
    job = add_job(session, "Backend Engineer", company="Acme", score=81)
    assert client.get("/api/applications").json() == []
    resp = client.patch(f"/api/applications/{job.id}", json={"status": "applied", "notes": "Referred by a friend"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "applied" and body["notes"] == "Referred by a friend"
    assert body["applied_at"] and body["status_changed_at"]
    assert body["job"] == {
        "title": "Backend Engineer",
        "company": "Acme",
        "location": "Remote (Worldwide)",
        "url": job.url,
        "score": 81.0,
    }
    assert [a["job_id"] for a in client.get("/api/applications").json()] == [job.id]
    assert client.get("/api/applications", params={"status": "saved"}).json() == []
    assert len(client.get("/api/applications", params={"status": "saved,applied"}).json()) == 1
    notes_only = client.patch(f"/api/applications/{job.id}", json={"notes": "Call on Monday"}).json()
    assert notes_only["status"] == "applied" and notes_only["notes"] == "Call on Monday"


def test_application_validation(admin, session):
    client = admin
    job = add_job(session)
    assert client.patch(f"/api/applications/{job.id}", json={"status": "hired"}).status_code == 422
    assert client.patch(f"/api/applications/{job.id}", json={}).status_code == 422
    assert client.patch("/api/applications/999999", json={"status": "saved"}).status_code == 404
    assert client.get("/api/applications", params={"status": "maybe"}).status_code == 422


def test_tracker_reads_and_writes_need_the_admin_token(client, session, monkeypatch):
    job = add_job(session)
    assert client.get("/api/applications").status_code == 401
    assert client.get("/api/applications", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert client.patch(f"/api/applications/{job.id}", json={"status": "saved"}).status_code == 401
    assert client.patch("/api/talent-networks/andela", json={"status": "applied"}).status_code == 401
    assert client.patch(f"/api/applications/{job.id}", json={"status": "saved"}, headers=ADMIN).status_code == 200
    assert client.patch("/api/talent-networks/andela", json={"status": "applied"}, headers=ADMIN).status_code == 200
    assert client.get("/api/applications", headers=ADMIN).status_code == 200


def test_without_an_admin_token_configured_private_data_is_closed(client, session, monkeypatch):
    job = add_job(session)
    monkeypatch.setattr(app_settings, "admin_token", "")
    assert client.get("/api/applications").status_code == 401
    assert client.get("/api/applications", headers={"X-Admin-Token": ""}).status_code == 401
    # Without ADMIN_TOKEN nothing is open: every write needs a caller too.
    assert client.patch(f"/api/applications/{job.id}", json={"status": "saved"}).status_code == 401
    assert client.patch("/api/talent-networks/andela", json={"status": "applied"}).status_code == 401


# ----------------------------------------------------------------- talent networks


def test_talent_networks_list_and_update(admin):
    client = admin
    networks = client.get("/api/talent-networks").json()
    assert [n["name"] for n in networks] == ["Andela", "Turing", "Toptal", "micro1", "Mercor", "Arc.dev"]
    assert all(n["status"] == "not_started" and n["url"].startswith("https://") and n["summary"] for n in networks)
    resp = client.patch("/api/talent-networks/turing", json={"status": "vetted"})
    assert resp.status_code == 200 and resp.json()["status"] == "vetted" and resp.json()["updated_at"]
    assert {n["slug"]: n["status"] for n in client.get("/api/talent-networks").json()}["turing"] == "vetted"
    assert client.patch("/api/talent-networks/turing", json={"status": "hired"}).status_code == 422
    assert client.patch("/api/talent-networks/nope", json={"status": "applied"}).status_code == 404


def test_talent_catalog_makes_no_acceptance_claims():
    for network in talent.CATALOG:
        assert "%" not in network["summary"] and "accept" not in network["summary"].lower()


# ----------------------------------------------------------------- webhook


def test_webhook_rejects_missing_or_wrong_secret(client, fake_tg, monkeypatch):
    monkeypatch.setattr(app_settings, "admin_token", "")  # past the write lock: the route's own check answers
    update = message_update("/help")
    assert client.post(HOOK, json=update).status_code == 401
    assert client.post(HOOK, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "nope"}).status_code == 401
    assert fake_tg.requests == []


def test_webhook_is_off_until_configured(client, monkeypatch):
    monkeypatch.setattr(app_settings, "admin_token", "")
    monkeypatch.setattr(telegram_settings, "webhook_secret", "")
    headers = {"X-Telegram-Bot-Api-Secret-Token": ""}
    assert client.post(HOOK, json=message_update("/help"), headers=headers).status_code == 404
    monkeypatch.setattr(telegram_settings, "webhook_secret", SECRET)
    monkeypatch.setattr(telegram_settings, "bot_token", "")
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET}
    assert client.post(HOOK, json=message_update("/help"), headers=headers).status_code == 404


def test_webhook_with_secret_handles_commands_even_when_read_only(client, fake_tg, monkeypatch):
    monkeypatch.setattr(app_settings, "admin_token", "adm")  # no X-Admin-Token: the Telegram secret is enough
    resp = client.post(HOOK, json=message_update("/help"), headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    assert resp.status_code == 200 and resp.json() == {"ok": True, "result": "command:help"}
    assert fake_tg.sent()[0]["chat_id"] == OWNER
    # A wrong secret is refused by the webhook itself (the path needs no login, only its secret).
    bad = client.post(HOOK, json=message_update("/help"), headers={"X-Telegram-Bot-Api-Secret-Token": "x"})
    assert bad.status_code == 401 and len(fake_tg.sent()) == 1
    # The secret only opens the webhook path; anywhere else it's no credential at all.
    other = client.patch(
        "/api/talent-networks/arc", json={"status": "applied"}, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}
    )
    assert other.status_code == 401


def test_webhook_button_tap_updates_tracker(client, session, fake_tg):
    job = add_job(session)
    update = {
        "update_id": 5,
        "callback_query": {
            "id": "q",
            "data": f"s:{job.id}:saved",
            "message": {"message_id": 3, "chat": {"id": int(OWNER)}},
        },
    }
    resp = client.post(HOOK, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    assert resp.json()["result"] == "callback:saved"
    session.expire_all()
    assert tracker.get_application(session, oid(session), job.id).status == "saved"


def test_webhook_errors_are_swallowed_so_telegram_does_not_redeliver(client, fake_tg):
    fake_tg.fail_all = True
    resp = client.post(HOOK, json=message_update("/help"), headers={"X-Telegram-Bot-Api-Secret-Token": SECRET})
    assert resp.status_code == 200 and resp.json() == {"ok": True, "result": "error"}


def test_webhook_rejects_non_json(client):
    headers = {"X-Telegram-Bot-Api-Secret-Token": SECRET, "Content-Type": "application/json"}
    assert client.post(HOOK, content=b"not json", headers=headers).status_code == 422


# ----------------------------------------------------------------- /api/jobs?status=


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_jobs_status_filter_sql():
    from app.models import JobRaw

    sql = _sql(select(JobRaw.id).where(status_filter({"saved", "new"}, 7)))
    assert "applications.user_id = 7 AND applications.status IN ('new', 'saved')" in sql
    assert "jobs_raw.id NOT IN (SELECT applications.job_id" in sql and sql.count("applications.user_id = 7") == 2
    assert "NOT IN" not in _sql(select(JobRaw.id).where(status_filter({"applied"}, 7)))


class _Result:
    def scalar(self):
        return 0

    def scalars(self):
        return self

    def all(self):
        return []


class _Recording:
    def __init__(self):
        self.statements = []

    async def execute(self, stmt, *_):
        self.statements.append(stmt)
        return _Result()


def test_jobs_endpoint_applies_status_filter(engine):
    rec = _Recording()

    async def fake_db():
        yield rec

    def sync_db():
        with Session(engine) as s:
            yield s
            s.commit()

    main.app.dependency_overrides[get_db] = fake_db
    main.app.dependency_overrides[get_sync_db] = sync_db
    try:
        c = TestClient(main.app)
        assert c.get("/api/jobs", params={"status": "new,saved,applied"}).status_code == 401
        assert rec.statements == []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(app_settings, "admin_token", "adm")
            resp = c.get("/api/jobs", params={"status": "new,saved,applied"}, headers=ADMIN)
        assert resp.status_code == 200 and rec.statements
        assert all("applications.user_id = 1 AND applications.status IN" in _sql(s) for s in rec.statements)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(app_settings, "admin_token", "adm")
            assert c.get("/api/jobs", params={"status": "bogus"}, headers=ADMIN).status_code == 422
    finally:
        main.app.dependency_overrides.clear()
