"""The privacy lock: the whole dashboard is private. Every GET route is classified as public (an explicit
allowlist) or private; logged out, every private one answers 401 with no data or counts, and the public
ones carry nothing private. Logged in, nobody sees another user's statuses, notes or progress. Seeds every
kind of private data, then calls every GET route the app has."""

import json
from datetime import timedelta

import main
import pytest
from app import talent, tracker, users
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


# The public allowlist, spelled out: the only API paths a logged-out visitor reaches (main.PUBLIC_PATHS).
PUBLIC_PATHS = {
    "/api/health",
    "/api/auth/session",
    "/api/auth/telegram",
    "/api/auth/link",
    "/api/auth/token-session",
    "/api/auth/logout",
    "/api/telegram/webhook",
    "/api/cron/discover",
}
PUBLIC_GETS = {"/api/health", "/api/auth/session", "/api/cron/discover"}  # cron: CRON_SECRET (test_telegram_digest)


def _private_gets(job_id: int, source_id: int) -> dict[str, list[str]]:
    return {
        "/api/sources": ["/api/sources"],
        "/api/sources/{source_id}": [f"/api/sources/{source_id}"],
        "/api/jobs": [
            "/api/jobs",
            "/api/jobs?sort=recent",
            "/api/jobs?page_size=100",
            "/api/jobs?q=Backend",
            "/api/jobs?status=interviewing",
        ],
        "/api/jobs/{job_id}": [f"/api/jobs/{job_id}", "/api/jobs/999999"],
        "/api/runs": ["/api/runs", "/api/runs?type=telegram"],
        "/api/applications": ["/api/applications", "/api/applications?status=interviewing"],
        "/api/talent-networks": ["/api/talent-networks"],
        "/api/me": ["/api/me"],
    }


def _classified_gets(job_id: int, source_id: int) -> tuple[list[str], list[str]]:
    """(public URLs, private URLs) for every GET route. A new GET route fails this until it's classified."""
    private = _private_gets(job_id, source_id)
    assert not PUBLIC_GETS & set(private)
    routes = {r.path for r in main.app.routes if isinstance(r, APIRoute) and "GET" in r.methods}
    assert routes == PUBLIC_GETS | set(private), f"classify new GET routes: {routes ^ (PUBLIC_GETS | set(private))}"
    public = [p for p in sorted(PUBLIC_GETS) if p != "/api/cron/discover"]
    return public, [url for urls in private.values() for url in urls]


def test_the_public_allowlist_is_exactly_these_paths():
    assert set(main.PUBLIC_PATHS) == PUBLIC_PATHS
    assert PUBLIC_GETS <= PUBLIC_PATHS
    every_path = {r.path for r in main.app.routes if isinstance(r, APIRoute)}
    assert PUBLIC_PATHS <= every_path  # no stale entries


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-credentials"),
        pytest.param({"X-Admin-Token": "wrong-token"}, id="wrong-admin-token"),
        pytest.param({"X-Admin-Token": ""}, id="empty-admin-token"),
        pytest.param({"X-User-Token": "not-a-real-token-123456"}, id="wrong-user-token"),
        pytest.param({"Cookie": "ff_session=forged-" + "x" * 40}, id="forged-cookie"),
    ],
)
def test_every_private_get_is_401_without_a_valid_login(client, seeded, headers):
    public, private = _classified_gets(seeded[0].id, seeded[0].source_id)
    titles = [job.title for job in seeded] + [job.company for job in seeded]
    for url in private:
        resp = client.get(url, headers=headers)
        assert resp.status_code == 401, url
        body = resp.text
        for word in (*PRIVATE_WORDS, *titles, '"total"', '"items"', '"stats"', '"slug"'):
            assert word not in body, f"{url} leaks {word!r}"
        assert set(resp.json()) <= {"detail", "authenticated"}, url
    for url in public:
        resp = client.get(url, headers=headers)
        assert resp.status_code == 200, url
        assert not any(word in resp.text for word in (*PRIVATE_WORDS, *titles)), url
    assert client.get("/api/auth/session", headers=headers).json() == {"authenticated": False}


def test_a_user_without_data_of_their_own_sees_nobody_elses(client, seeded, session):
    loner = add_member(session, "555009", name="Loner")
    token = users.issue_dashboard_token(session, loner)
    session.commit()
    _, private = _classified_gets(seeded[0].id, seeded[0].source_id)
    for url in private:
        resp = client.get(url, headers={"X-User-Token": token})
        assert resp.status_code in (200, 404), url
        for word in PRIVATE_WORDS:
            assert word not in resp.text, f"{url} leaks {word!r} to another user"
        if resp.status_code == 200:
            assert not PRIVATE_KEYS & _keys(resp.json()), url
    assert client.get("/api/applications", headers={"X-User-Token": token}).json() == []


def test_logged_in_jobs_have_job_data_and_a_score(client, seeded):
    items = client.get("/api/jobs", headers=ADMIN).json()["items"]
    assert len(items) == 4
    assert all(i["analysis"]["score"] > 0 and "match" in i["analysis"]["extracted_json"] for i in items)
    assert all("status" not in i for i in items)


def test_members_see_only_telegram_totals_and_the_owner_everything(client, seeded, session):
    token = users.issue_dashboard_token(session, users.by_chat(session, "555001"))
    session.commit()
    (run,) = [r for r in client.get("/api/runs", headers={"X-User-Token": token}).json() if r["type"] == "telegram"]
    assert run["stats"] == {"sent": 1, "failed": 1}  # a digest run covers every user: details are the owner's
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
