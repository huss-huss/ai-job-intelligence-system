"""The private site: every data endpoint needs a login (session cookie, X-User-Token or X-Admin-Token),
and admin operations need the owner: the admin token, or the owner's own login session with the CSRF
header. A member (session or link) and the owner's /dashboard link get 403. No network: queue dispatches
are faked and the Telegram login payloads are signed in the test (see test_web_login.py)."""

import importlib

import main
import pytest
from app import tracker, users
from app.config import settings as app_settings
from app.models import Run, Source
from app.routes import analyze, scrape, sources
from fastapi.routing import APIRoute
from sqlalchemy import select

from tests import test_web_login as web
from tests.tracker_fixtures import NOW, OWNER, owner

ADMIN = {"X-Admin-Token": "adm"}
CSRF, login = web.CSRF, web.login
api, two = web.api, web.two  # fixtures: the API over a SQLite database, and the owner, two members, three jobs

# Every data GET, with what proves it returned data to this viewer.
DATA_GETS = {
    "/api/jobs": lambda body: body["total"] == 3 and len(body["items"]) == 3,
    "/api/jobs/{job_id}": lambda body: body["title"].startswith("Backend Engineer"),
    "/api/sources": lambda body: [s["name"] for s in body] == ["Fixture"],
    "/api/sources/{source_id}": lambda body: body["name"] == "Fixture",
    "/api/runs": lambda body: [r["type"] for r in body] == ["scrape"],
    "/api/talent-networks": lambda body: len(body) >= 6 and all("status" in n for n in body),
    "/api/applications": lambda body: isinstance(body, list),
    "/api/me": lambda body: body["role"] in ("owner", "member"),
}

# Every non-GET route: who may call it.
ADMIN_OPS = [
    ("POST", "/api/scrape", {}),
    ("POST", "/api/analyze", {}),
    ("DELETE", "/api/jobs", None),
    ("DELETE", "/api/runs", None),
    ("POST", "/api/sources", {"name": "New", "base_url": "https://new.example.com"}),
    ("PATCH", "/api/sources/{source_id}", {"enabled": False}),
    ("POST", "/api/sources/{source_id}/scrape", None),
]
USER_WRITES = {"/api/applications/{job_id}", "/api/talent-networks/{slug}"}
OWN_SECRET_WRITES = {  # each checks its own secret or credentials, see main.PUBLIC_PATHS
    "/api/auth/telegram",
    "/api/auth/token-session",
    "/api/auth/logout",
    "/api/telegram/webhook",
}


@pytest.fixture
def site(api, session, two, monkeypatch):
    """The API with three jobs, one run and fake queue dispatches (recorded)."""
    me, member, other, jobs = two
    session.add(Run(type="scrape", status="completed", started_at=NOW, stats={"inserted": 3}))
    session.commit()
    calls = []

    async def fake_dispatch(*args, **_):
        calls.append(args)
        return "job-1"

    for module in (scrape, sources):
        monkeypatch.setattr(module, "dispatch_scrape", fake_dispatch)
    monkeypatch.setattr(analyze, "dispatch_analyze", fake_dispatch)
    source_id = session.execute(select(Source.id)).scalar_one()
    return {"api": api, "member": member, "jobs": jobs, "source_id": source_id, "calls": calls}


def _url(path: str, site) -> str:
    return path.format(job_id=site["jobs"][0].id, source_id=site["source_id"], slug="arc")


def _check_data_gets(site, headers=None):
    for path, ok in DATA_GETS.items():
        resp = site["api"].get(_url(path, site), headers=headers or {})
        assert resp.status_code == 200, (path, resp.text)
        assert ok(resp.json()), path


def test_every_data_get_route_is_listed_here():
    gets = {r.path for r in main.app.routes if isinstance(r, APIRoute) and "GET" in r.methods}
    assert gets - {"/api/health", "/api/auth/session", "/api/cron/discover"} == set(DATA_GETS)


def test_every_write_route_is_classified():
    writes = {
        (method, r.path)
        for r in main.app.routes
        if isinstance(r, APIRoute)
        for method in r.methods - {"GET", "HEAD"}
    }
    classified = {(m, p) for m, p, _ in ADMIN_OPS}
    assert {p for _, p in writes} == {p for _, p in classified} | USER_WRITES | OWN_SECRET_WRITES
    assert {w for w in writes if w[1] not in USER_WRITES | OWN_SECRET_WRITES} == classified


def test_logged_out_every_data_endpoint_is_401_with_nothing_in_it(site):
    api = site["api"]
    for path in DATA_GETS:
        resp = api.get(_url(path, site))
        assert resp.status_code == 401, path
        assert resp.json() == {"detail": "Log in to see this", "authenticated": False}, path
        assert resp.headers["www-authenticate"] == "Token"
    assert api.get("/api/jobs", params={"page_size": 1}).status_code == 401  # not even the count
    assert api.get("/api/health").status_code == 200
    assert api.get("/api/auth/session").json() == {"authenticated": False}


def test_logged_out_writes_are_refused_and_change_nothing(site, session):
    api = site["api"]
    for method, path, body in ADMIN_OPS:
        assert api.request(method, _url(path, site), json=body).status_code == 401, path
        assert api.request(method, _url(path, site), json=body, headers=CSRF).status_code == 401, path
    assert site["calls"] == []
    session.expire_all()
    assert session.execute(select(Source.enabled)).scalar_one() is True


def test_a_member_session_reads_everything_of_theirs(site):
    login(site["api"])
    _check_data_gets(site)
    assert site["api"].get("/api/me").json() == {"role": "member", "display_name": "Alem"}


def test_a_user_token_reads_everything_of_theirs(site, session):
    token = users.issue_dashboard_token(session, site["member"])
    session.commit()
    _check_data_gets(site, {"X-User-Token": token})


def test_the_admin_token_reads_everything(site):
    _check_data_gets(site, ADMIN)


def test_an_owner_session_reads_everything(site):
    login(site["api"], OWNER)
    _check_data_gets(site)


def test_scoping_stays_per_user(site, session):
    """Logged in is not "sees everything": statuses stay the viewer's own."""
    jobs = site["jobs"]
    tracker.set_status(session, owner(session).id, jobs[0].id, "interviewing", notes="OWNER-NOTE", now=NOW)
    session.commit()
    login(site["api"])
    assert site["api"].get("/api/applications").json() == []
    assert site["api"].get("/api/jobs", params={"status": "interviewing"}).json()["total"] == 0
    assert "OWNER-NOTE" not in site["api"].get(f"/api/jobs/{jobs[0].id}").text


@pytest.mark.parametrize("with_csrf", [True, False])
def test_a_member_session_cannot_run_admin_operations(site, session, with_csrf):
    api = site["api"]
    login(api)
    for method, path, body in ADMIN_OPS:
        resp = api.request(method, _url(path, site), json=body, headers=CSRF if with_csrf else {})
        assert resp.status_code == 403, (path, resp.text)
    assert site["calls"] == []
    session.expire_all()
    assert session.execute(select(Source.enabled)).scalar_one() is True
    assert len(site["jobs"]) == 3 and api.get("/api/jobs").json()["total"] == 3


def test_a_member_link_token_cannot_run_admin_operations(site, session):
    token = users.issue_dashboard_token(session, site["member"])
    session.commit()
    for method, path, body in ADMIN_OPS:
        resp = site["api"].request(method, _url(path, site), json=body, headers={"X-User-Token": token})
        assert resp.status_code == 403, path
    assert site["calls"] == []


def test_the_owners_link_token_is_not_an_admin_credential(site, session):
    token = users.issue_dashboard_token(session, owner(session))
    session.commit()
    assert site["api"].get("/api/jobs", headers={"X-User-Token": token}).status_code == 200
    for method, path, body in ADMIN_OPS:
        headers = {"X-User-Token": token, **CSRF}
        assert site["api"].request(method, _url(path, site), json=body, headers=headers).status_code == 403, path
    assert site["calls"] == []


def test_an_owner_session_runs_admin_operations_with_the_csrf_header(site, session):
    api = site["api"]
    login(api, OWNER)
    # Without the header: refused (CSRF), nothing dispatched.
    for method, path, body in ADMIN_OPS:
        resp = api.request(method, _url(path, site), json=body)
        assert resp.status_code == 403 and "X-Requested-With" in resp.json()["detail"], path
    assert site["calls"] == []
    # With it: each one runs.
    sid = site["source_id"]
    assert api.post("/api/scrape", json={}, headers=CSRF).json()["message"] == "Enqueued 1 scrape task(s)"
    assert api.post("/api/analyze", json={}, headers=CSRF).json()["job_count"] == 3
    assert api.post(f"/api/sources/{sid}/scrape", headers=CSRF).json()["job_id"] == "job-1"
    assert api.patch(f"/api/sources/{sid}", json={"enabled": False}, headers=CSRF).json()["enabled"] is False
    created = api.post("/api/sources", json={"name": "New", "base_url": "https://n.example.com"}, headers=CSRF)
    assert created.status_code == 201
    assert len(site["calls"]) == 3
    assert api.delete("/api/runs", headers=CSRF).json()["deleted"] == 1
    assert api.delete("/api/jobs", headers=CSRF).json()["deleted_jobs"] == 3
    assert api.get("/api/jobs").json()["total"] == 0


def test_the_admin_token_runs_admin_operations(site):
    api = site["api"]
    assert api.post("/api/scrape", json={}, headers=ADMIN).status_code == 200
    assert api.post("/api/analyze", json={}, headers=ADMIN).status_code == 200
    assert api.delete("/api/runs", headers=ADMIN).json()["deleted"] == 1
    assert len(site["calls"]) == 2


def test_without_an_admin_token_configured_only_the_owner_session_is_admin(site, monkeypatch):
    monkeypatch.setattr(app_settings, "admin_token", "")
    api = site["api"]
    assert api.post("/api/scrape", json={}).status_code == 401  # no longer open on an unlocked deployment
    assert api.post("/api/scrape", json={}, headers={"X-Admin-Token": ""}).status_code == 401
    assert api.get("/api/jobs", headers={"X-Admin-Token": ""}).status_code == 401
    login(api, OWNER)
    assert api.post("/api/scrape", json={}, headers=CSRF).status_code == 200


def test_a_logged_out_session_is_401_again(site):
    api = site["api"]
    login(api, OWNER)
    assert api.get("/api/jobs").status_code == 200
    assert api.post("/api/auth/logout", headers=CSRF).status_code == 204
    assert api.get("/api/jobs").status_code == 401
    assert api.post("/api/scrape", json={}, headers=CSRF).status_code == 401


def test_cors_preflight_and_401s_carry_cors_headers(site):
    api = site["api"]
    origin = {"Origin": "http://localhost:3000"}
    preflight = {**origin, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "x-user-token"}
    pre = api.options("/api/jobs", headers=preflight)
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == "http://localhost:3000"
    resp = api.get("/api/jobs", headers=origin)
    assert resp.status_code == 401 and resp.headers["access-control-allow-origin"] == "http://localhost:3000"


# ------------------------------------------------------------------------ API docs


@pytest.fixture
def reload_main(monkeypatch):
    """Re-import main with other settings; restores the original app afterwards."""
    def load(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(app_settings, "api_docs", None)
        if "API_DOCS" in env:
            monkeypatch.setattr(app_settings, "api_docs", env["API_DOCS"] == "true")
        import app.database

        monkeypatch.setattr(app.database, "SERVERLESS", bool(env.get("VERCEL")))
        return importlib.reload(main)

    yield load
    monkeypatch.undo()
    importlib.reload(main)


def _docs_status(module) -> dict[str, int]:
    from fastapi.testclient import TestClient

    client = TestClient(module.app)
    return {path: client.get(path).status_code for path in ("/api/docs", "/api/redoc", "/api/openapi.json")}


def test_docs_are_off_in_production(reload_main):
    assert set(_docs_status(reload_main(VERCEL="1")).values()) == {401}  # not public, and no such route


def test_docs_can_be_turned_on_in_production(reload_main):
    assert set(_docs_status(reload_main(VERCEL="1", API_DOCS="true")).values()) == {200}


def test_docs_are_on_for_local_development(reload_main):
    module = reload_main()
    assert set(_docs_status(module).values()) == {200}
    assert "/api/docs" not in module.PUBLIC_PATHS  # only public while DOCS is on (main.is_public)
