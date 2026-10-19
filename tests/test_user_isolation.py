"""Per-user isolation: statuses, notes, feedback, talent networks, scores and digests of one user never
show up for another, over Telegram or the API."""

from contextlib import contextmanager
from datetime import timedelta

import httpx
import main
import pytest
from app import talent, tracker, users
from app.config import settings as app_settings
from app.database import get_db
from app.db_sync import get_sync_db
from app.models import Run
from app.telegram import bot, digest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.tracker_fixtures import (
    NOW,
    OWNER,
    AsyncSessionAdapter,
    add_job,
    add_member,
    callback_update,
    message_update,
    owner,
    settings,
    tg_error,
)
from worker.user_scoring import rescore_user

FRIEND = "555001"


def run(session, fake_tg, text, chat_id=OWNER):
    return bot.handle_update(session, fake_tg.client(), settings(), message_update(text, chat_id=chat_id), now=NOW)


def sent_to(fake_tg, chat_id):
    return [b["text"] for b in fake_tg.sent() if str(b["chat_id"]) == chat_id]


@pytest.fixture
def two(session):
    """The owner and a member, three jobs both can see."""
    member = add_member(session, FRIEND)
    jobs = [add_job(session, f"Backend Engineer {i}", company=f"Co{i}", score=70 + i) for i in range(3)]
    return owner(session), member, jobs


# ----------------------------------------------------------------------- tracker


def test_statuses_and_notes_are_per_user(session, fake_tg, two):
    me, friend, jobs = two
    tracker.set_status(session, me.id, jobs[0].id, "applied", notes="owner secret", now=NOW)
    session.commit()
    run(session, fake_tg, "/applied", chat_id=FRIEND)
    assert sent_to(fake_tg, FRIEND) == ["Nothing tracked yet. Tap ⭐ Save or ✅ Applied under a job."]
    # The owner acted on job 0; for the member it is still an open match.
    fake_tg.requests.clear()
    run(session, fake_tg, "/top", chat_id=FRIEND)
    assert any("Backend Engineer 0" in t for t in sent_to(fake_tg, FRIEND))
    assert not any("owner secret" in t for t in sent_to(fake_tg, FRIEND))


def test_buttons_change_only_the_tapping_users_tracker(session, fake_tg, two):
    me, friend, jobs = two
    update = callback_update(f"s:{jobs[1].id}:not_interested", chat_id=FRIEND)
    assert bot.handle_update(session, fake_tg.client(), settings(), update, now=NOW) == "callback:not_interested"
    assert tracker.get_application(session, friend.id, jobs[1].id).status == "not_interested"
    assert tracker.get_application(session, me.id, jobs[1].id) is None
    run(session, fake_tg, f"/status {jobs[2].id} offer")
    assert tracker.get_application(session, me.id, jobs[2].id).status == "offer"
    assert tracker.get_application(session, friend.id, jobs[2].id) is None


def test_feedback_only_moves_your_own_ranking(session, two):
    me, friend, _ = two
    disliked = add_job(session, "Senior Backend Engineer", company="Acme", score=50)
    tracker.set_status(session, me.id, disliked.id, "not_interested", now=NOW)
    session.commit()
    target = add_job(session, "Backend Engineer", company="Acme", score=75)
    mine = {c.job.id: c for c in tracker.candidates(session, me.id)}
    theirs = {c.job.id: c for c in tracker.candidates(session, friend.id)}
    assert mine[target.id].points == 55 and mine[target.id].adjustment.reasons
    assert theirs[target.id].points == 75 and not theirs[target.id].adjustment.reasons
    assert disliked.id in theirs  # the owner's 👎 doesn't hide it from the member


def test_talent_network_progress_is_per_user(session, two):
    me, friend, _ = two
    talent.set_status(session, me.id, "turing", "vetted", NOW)
    assert {n["slug"]: n["status"] for n in talent.list_networks(session, friend.id)}["turing"] == "not_started"
    talent.set_status(session, friend.id, "turing", "applied", NOW)
    assert {n["slug"]: n["status"] for n in talent.list_networks(session, me.id)}["turing"] == "vetted"


def test_stats_count_your_own_tracker(session, fake_tg, two):
    me, friend, jobs = two
    tracker.set_status(session, me.id, jobs[0].id, "applied", now=NOW)
    session.commit()
    run(session, fake_tg, "/stats", chat_id=FRIEND)
    (text,) = sent_to(fake_tg, FRIEND)
    assert "Tracker:" not in text and "Sent to you: 0" in text


# ----------------------------------------------------------------------- API with personal tokens


@pytest.fixture
def api(engine, monkeypatch):
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


def test_a_dashboard_token_opens_only_its_owners_tracker(api, session, two):
    me, friend, jobs = two
    tracker.set_status(session, me.id, jobs[0].id, "interviewing", notes="owner note", now=NOW)
    tracker.set_status(session, friend.id, jobs[1].id, "saved", notes="friend note", now=NOW)
    talent.set_status(session, friend.id, "andela", "applied", NOW)
    token = users.issue_dashboard_token(session, friend)
    session.commit()
    mine = {"X-User-Token": token}

    apps = api.get("/api/applications", headers=mine).json()
    assert [(a["job_id"], a["status"], a["notes"]) for a in apps] == [(jobs[1].id, "saved", "friend note")]
    owner_apps = api.get("/api/applications", headers={"X-Admin-Token": "adm"}).json()
    assert [(a["job_id"], a["notes"]) for a in owner_apps] == [(jobs[0].id, "owner note")]
    nets = {n["slug"]: n["status"] for n in api.get("/api/talent-networks", headers=mine).json()}
    assert nets["andela"] == "applied" and nets["turing"] == "not_started"
    filtered = api.get("/api/jobs", params={"status": "interviewing"}, headers=mine).json()
    assert filtered["total"] == 0  # the owner's interviewing job isn't the member's
    saved = api.get("/api/jobs", params={"status": "saved"}, headers=mine).json()
    assert [i["id"] for i in saved["items"]] == [jobs[1].id]

    # Writes go through the read-only lock with the personal token, to the member's own rows only.
    resp = api.patch(f"/api/applications/{jobs[2].id}", json={"status": "applied"}, headers=mine)
    assert resp.status_code == 200
    session.expire_all()
    assert tracker.get_application(session, friend.id, jobs[2].id).status == "applied"
    assert tracker.get_application(session, me.id, jobs[2].id) is None
    assert api.patch("/api/talent-networks/arc", json={"status": "vetted"}, headers=mine).status_code == 200
    # ...and nowhere else.
    assert api.patch("/api/sources/1", json={"enabled": False}, headers=mine).status_code == 403
    assert api.delete("/api/jobs", headers=mine).status_code == 403
    assert api.post("/api/analyze", json={}, headers=mine).status_code == 403


def test_bad_revoked_or_missing_user_tokens_are_the_public(api, session, two):
    _, friend, _ = two
    token = users.issue_dashboard_token(session, friend)
    session.commit()
    for headers in ({"X-User-Token": "nope"}, {"X-User-Token": token[:-1]}, {"X-User-Token": "x" * 500}, {}):
        assert api.get("/api/applications", headers=headers).status_code == 401
        assert api.patch("/api/applications/1", json={"status": "saved"}, headers=headers).status_code in (401, 403)
    users.revoke_dashboard_token(session, friend)
    session.commit()
    assert api.get("/api/applications", headers={"X-User-Token": token}).status_code == 401


def test_dashboard_command_issues_a_hashed_link_once(session, fake_tg, two, monkeypatch):
    _, friend, _ = two
    s = settings(dashboard_url="https://dash.example.app")
    update = message_update("/dashboard", chat_id=FRIEND)
    assert bot.handle_update(session, fake_tg.client(), s, update, now=NOW) == "command:dashboard"
    text = fake_tg.sent()[-1]["text"]
    token = text.split("#token=")[1].split('"')[0]
    session.refresh(friend)
    assert friend.dashboard_token_hash == users.hash_secret(token) and token not in friend.dashboard_token_hash
    assert users.user_by_dashboard_token(session, token).id == friend.id
    bot.handle_update(session, fake_tg.client(), s, update, now=NOW)  # a new link replaces the old
    assert users.user_by_dashboard_token(session, token) is None
    bot.handle_update(session, fake_tg.client(), s, message_update("/dashboard revoke", chat_id=FRIEND), now=NOW)
    session.refresh(friend)
    assert friend.dashboard_token_hash is None
    bot.handle_update(session, fake_tg.client(), settings(), update, now=NOW)
    assert "isn't configured" in fake_tg.sent()[-1]["text"]


# ----------------------------------------------------------------------- digests


BACKEND = "Senior Backend Engineer (Python, FastAPI)"
DATA = "Senior Data Engineer (Spark, Airflow)"
DATA_PROFILE = {
    "name": "Data engineer",
    "target_titles": ["data engineer"],
    "skills": {"spark": 3, "airflow": 3, "sql": 2},
    "seniority": ["senior"],
    "location": {"country": "Ethiopia", "eligible_terms": ["worldwide", "anywhere", "ethiopia"]},
}


def _real_jobs(session, *users_):
    """Two jobs scored for real with each user's own profile (worker/user_scoring.py)."""
    add_job(
        session, BACKEND, company="B", created_at=NOW - timedelta(hours=1),
        description="Build APIs in Python and FastAPI with Postgres, Redis and Docker. LLM agents. Remote worldwide.",
    )  # fmt: skip
    add_job(
        session, DATA, company="D", created_at=NOW - timedelta(hours=1),
        description="Build data pipelines with Spark, Airflow and SQL. Remote worldwide.",
    )  # fmt: skip
    for u in users_:
        rescore_user(session, u, since=NOW - timedelta(days=2), now=NOW)
    session.commit()


def test_digests_differ_by_profile(session, fake_tg):
    me = owner(session)
    friend = add_member(session, FRIEND, profile=DATA_PROFILE)
    _real_jobs(session, me, friend)
    s = settings(digest_min_score=0, digest_size=1)
    with fake_tg.client() as client:
        digest.send_all(session, client, s, NOW)
    owner_cards = [t for t in sent_to(fake_tg, OWNER) if not t.startswith("<b>FirstFetch")]
    friend_cards = [t for t in sent_to(fake_tg, FRIEND) if not t.startswith("<b>FirstFetch")]
    assert owner_cards[0].startswith(f"<b>{BACKEND}</b>")
    assert friend_cards[0].startswith(f"<b>{DATA}</b>")


def _factory(engine):
    @contextmanager
    def factory():
        with Session(engine, expire_on_commit=False) as s:
            yield s
            s.commit()

    return factory


def test_cron_digest_with_two_users_isolates_failures_and_never_repeats(engine, session, fake_tg):
    me = owner(session)
    friend = add_member(session, FRIEND)
    for i in range(3):
        add_job(session, f"Backend Engineer {i}", company=f"Co{i}", score=80 + i)
    paused = add_member(session, "555003", name="Paused")
    paused.paused = True
    session.commit()

    def handler(request):
        body = request.read()
        if b'"chat_id":"555001"' in body or b'"chat_id": "555001"' in body:
            return tg_error(403, "Forbidden: bot was blocked by the user")
        return None

    original = fake_tg.handler

    def blocking(request: httpx.Request):
        return handler(request) or original(request)

    fake_tg.handler = blocking
    result = digest.run_digest(
        settings=settings(), session_factory=_factory(engine), client_factory=lambda s: fake_tg.client(), now=NOW
    )
    assert result["users"] == 2  # the paused member isn't a recipient
    per = result["per_user"]
    assert per[str(me.id)]["sent"] == 3
    assert "error" in per[str(friend.id)] and "blocked" in per[str(friend.id)]["error"]
    run = session.get(Run, result["run_id"])
    session.refresh(run)
    assert run.status == "completed_with_errors"
    assert not [b for b in fake_tg.sent() if b["chat_id"] == "555003"]
    # The friend unblocks the bot: the next run sends them everything, the owner nothing new.
    fake_tg.handler = original
    fake_tg.requests.clear()
    again = digest.run_digest(
        settings=settings(), session_factory=_factory(engine), client_factory=lambda s: fake_tg.client(), now=NOW
    )
    assert again["per_user"][str(friend.id)]["sent"] == 3
    assert again["per_user"][str(me.id)]["sent"] == 0 and again["per_user"][str(me.id)]["empty_notice"]


def test_digest_time_budget_skips_the_remaining_users(session, fake_tg):
    owner(session)
    add_member(session, FRIEND)
    add_job(session, score=90)
    ticks = iter([0.0, 0.0, 1000.0])
    with fake_tg.client() as client:
        result = digest.send_all(session, client, settings(digest_max_seconds=10), NOW, clock=lambda: next(ticks))
    assert result["stopped_early"] and list(result["per_user"].values())[1] == {"skipped": "time budget"}


def test_per_user_digest_settings(session, fake_tg):
    me = owner(session)
    friend = add_member(session, FRIEND)
    friend.digest_size, friend.digest_min_score = 1, 85
    session.commit()
    for i in range(4):
        add_job(session, f"Backend Engineer {i}", company=f"Co{i}", score=80 + i * 3)  # 80, 83, 86, 89
    with fake_tg.client() as client:
        result = digest.send_all(session, client, settings(), NOW)
    assert result["per_user"][str(me.id)]["sent"] == 4
    assert result["per_user"][str(friend.id)]["sent"] == 1
    assert "score ≥ 85" in sent_to(fake_tg, FRIEND)[0]
