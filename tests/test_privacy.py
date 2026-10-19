"""The privacy lock: no tracker status, note, feedback or talent-network progress leaks through any
endpoint a caller without a token can reach. Seeds every kind of private data, then calls every GET
route the app has."""

import json
from datetime import timedelta

import main
import pytest
from app import talent, tracker
from app.config import settings as app_settings
from app.database import get_db
from app.db_sync import get_sync_db
from app.models import Run
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.tracker_fixtures import NOW, AsyncSessionAdapter, add_job, add_member, oid, owner

NOTE = "PRIVATE-NOTE-7f3a"
RESUME = "PRIVATE-RESUME-91c2"
# JSON values that only exist in private data (job statuses other than "new", talent progress).
PRIVATE_WORDS = ('"interviewing"', '"not_interested"', '"vetted"', '"active"', '"offer"', NOTE, RESUME)
PRIVATE_KEYS = {"notes", "applied_at", "notified_at", "status_changed_at", "application", "applications"}
ADMIN = {"X-Admin-Token": "adm"}


@pytest.fixture
def seeded(session):
    jobs = [add_job(session, f"Backend Engineer {i}", company=f"Co{i}", score=70 + i) for i in range(4)]
    tracker.set_status(session, oid(session), jobs[0].id, "interviewing", notes=NOTE, now=NOW)
    tracker.set_status(session, oid(session), jobs[1].id, "not_interested", now=NOW)
    tracker.set_status(session, oid(session), jobs[2].id, "offer", now=NOW)
    tracker.mark_notified(session, oid(session), jobs[3].id, NOW)
    member = add_member(session, "555001")  # a second user's data must not leak either
    tracker.set_status(session, member.id, jobs[3].id, "interviewing", notes=NOTE, now=NOW)
    talent.set_status(session, member.id, "arc", "vetted", NOW)
    talent.set_status(session, oid(session), "turing", "vetted", NOW)
    talent.set_status(session, oid(session), "andela", "active", NOW)
    member.resume_text = f"{RESUME} member resume"  # cover-letter resumes (/cv) are private too
    owner(session).resume_text = f"{RESUME} owner resume"
    session.add(
        Run(
            type="telegram",
            status="completed_with_errors",
            started_at=NOW,
            ended_at=NOW + timedelta(seconds=3),
            stats={"sent": 1, "failed": 1, "errors": [{"job_id": jobs[1].id, "error": "x"}], "candidates": 2},
        )
    )
    session.commit()
    return jobs


@pytest.fixture
def client(engine, monkeypatch):
    def sync_db():
        with Session(engine, expire_on_commit=False) as s:
            yield s
            s.commit()

    async def async_db():
        with Session(engine, expire_on_commit=False) as s:
            yield AsyncSessionAdapter(s)

    monkeypatch.setattr(app_settings, "admin_token", "adm")
    main.app.dependency_overrides[get_sync_db] = sync_db
    main.app.dependency_overrides[get_db] = async_db
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value)) if value else set()
    return set()


def _public_gets(job_id: int, source_id: int) -> list[str]:
    """Every GET route, with path parameters filled in. A new GET route fails this test until it's listed."""
    known = {
        "/api/health": ["/api/health"],
        "/api/sources": ["/api/sources"],
        "/api/sources/{source_id}": [f"/api/sources/{source_id}"],
        "/api/jobs": [
            "/api/jobs",
            "/api/jobs?sort=recent",
            "/api/jobs?page_size=100",
            "/api/jobs?q=Backend",
        ],
        "/api/jobs/{job_id}": [f"/api/jobs/{job_id}"],
        "/api/runs": ["/api/runs", "/api/runs?type=telegram"],
        "/api/applications": ["/api/applications", "/api/applications?status=interviewing"],
        "/api/talent-networks": ["/api/talent-networks"],
        "/api/me": ["/api/me"],
        "/api/cron/discover": [],  # CRON_SECRET; covered in test_telegram_digest
    }
    routes = {r.path for r in main.app.routes if isinstance(r, APIRoute) and "GET" in r.methods}
    assert routes == set(known), f"review new GET routes for private data: {routes ^ set(known)}"
    return [url for urls in known.values() for url in urls]


@pytest.mark.parametrize("token", [None, "wrong-token", ""])
def test_no_private_data_on_any_public_endpoint(client, seeded, token):
    headers = {"X-Admin-Token": token} if token is not None else {}
    for url in _public_gets(seeded[0].id, seeded[0].source_id):
        resp = client.get(url, headers=headers)
        body = resp.text
        for word in PRIVATE_WORDS:
            assert word not in body, f"{url} leaks {word!r}"
        if resp.status_code == 200:
            assert not PRIVATE_KEYS & _keys(resp.json()), url


def test_private_endpoints_answer_401_to_the_public(client, seeded):
    for url in ("/api/applications", "/api/applications?status=interviewing", "/api/me"):
        assert client.get(url).status_code == 401
    # The status filter on /api/jobs would reveal which jobs someone acted on.
    for status in ("interviewing", "new", "applied,offer", "not_interested"):
        assert client.get("/api/jobs", params={"status": status}).status_code == 401
    assert client.get("/api/jobs", params={"status": "any"}).status_code == 200  # "any" is no filter


def test_public_talent_networks_are_the_catalog_only(client, seeded):
    networks = client.get("/api/talent-networks").json()
    assert [n["slug"] for n in networks] == [n["slug"] for n in talent.CATALOG]
    assert all(set(n) == {"slug", "name", "url", "summary"} for n in networks)


def test_public_jobs_have_job_data_and_the_public_score(client, seeded):
    items = client.get("/api/jobs").json()["items"]
    assert len(items) == 4
    assert all(i["analysis"]["score"] > 0 and "match" in i["analysis"]["extracted_json"] for i in items)
    assert all("status" not in i for i in items)


def test_public_runs_show_only_telegram_totals(client, seeded):
    (run,) = [r for r in client.get("/api/runs").json() if r["type"] == "telegram"]
    assert run["stats"] == {"sent": 1, "failed": 1}
    (private,) = [r for r in client.get("/api/runs", headers=ADMIN).json() if r["type"] == "telegram"]
    assert private["stats"]["errors"][0]["job_id"] == seeded[1].id


def test_me_names_the_viewer(client, seeded):
    assert client.get("/api/me", headers=ADMIN).json() == {"role": "owner", "display_name": "Owner"}


def test_the_owner_sees_everything_with_the_token(client, seeded):
    apps = client.get("/api/applications", headers=ADMIN).json()
    assert {a["status"] for a in apps} == {"interviewing", "not_interested", "offer", "new"}
    assert NOTE in json.dumps(apps)
    networks = {n["slug"]: n["status"] for n in client.get("/api/talent-networks", headers=ADMIN).json()}
    assert networks["turing"] == "vetted" and networks["arc"] == "not_started"
    resp = client.get("/api/jobs", params={"status": "interviewing"}, headers=ADMIN)
    assert resp.status_code == 200 and [i["id"] for i in resp.json()["items"]] == [seeded[0].id]
