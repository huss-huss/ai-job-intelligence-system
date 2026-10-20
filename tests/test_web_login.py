"""Log in with Telegram: the widget signature (known vectors made here), who may log in, the session
lifecycle and cookie, CSRF on cookie writes, isolation between users and the ADMIN_TOKEN write lock.
No network: the payloads are signed in the test with a fake bot token."""

import hashlib
import hmac
import time
from datetime import UTC, datetime, timedelta

import main
import pytest
from app import talent, tracker, users, web_sessions
from app.config import settings as app_settings
from app.database import get_db
from app.db_sync import get_sync_db
from app.models import WebSession
from app.telegram import bot
from app.telegram.config import telegram_settings
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.tracker_fixtures import (
    NOW,
    OWNER,
    TOKEN,
    AsyncSessionAdapter,
    add_job,
    add_member,
    message_update,
    owner,
    settings,
)

FRIEND = "555001"
OTHER = "555002"
CSRF = {"X-Requested-With": "firstfetch"}
NOT_ALLOWED = {"detail": "This Telegram account can't log in here. Ask the owner for an invite."}


def signed(fields: dict, bot_token: str = TOKEN) -> dict:
    """What the widget hands the page: the fields plus Telegram's hash, computed independently of the app."""
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(bot_token.encode()).digest()
    return {**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()}


def widget(telegram_id: str | int, auth_date: int | None = None, **extra) -> dict:
    fields = {
        "id": int(telegram_id),
        "first_name": "Alem",
        "username": "alem",
        "auth_date": int(time.time()) if auth_date is None else auth_date,
    }
    return signed({**fields, **extra})


# ----------------------------------------------------------------------- signature


def test_known_vector_matches_the_documented_algorithm():
    fields = {"id": 123456789, "first_name": "Alem", "last_name": "B", "username": "alem", "auth_date": 1790000000}
    check = "auth_date=1790000000\nfirst_name=Alem\nid=123456789\nlast_name=B\nusername=alem"
    assert web_sessions.data_check_string({**fields, "hash": "x"}) == check
    expected = hmac.new(hashlib.sha256(TOKEN.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    assert web_sessions.sign(fields, TOKEN) == expected
    now = datetime.fromtimestamp(1790000000, UTC) + timedelta(minutes=5)
    assert web_sessions.verify_login({**fields, "hash": expected}, TOKEN, now) == 123456789


def test_valid_payload_with_photo_and_string_values():
    payload = widget(FRIEND, photo_url="https://t.me/i/userpic/320/alem.jpg")
    assert web_sessions.verify_login(payload, TOKEN) == int(FRIEND)
    as_strings = {k: str(v) for k, v in payload.items()}  # some clients send every value as a string
    assert web_sessions.verify_login(as_strings, TOKEN) == int(FRIEND)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: {**p, "id": 555002}, id="tampered-id"),
        pytest.param(lambda p: {**p, "first_name": "Mallory"}, id="tampered-name"),
        pytest.param(lambda p: {**p, "auth_date": p["auth_date"] + 1}, id="tampered-auth-date"),
        pytest.param(lambda p: {**p, "extra": "1"}, id="added-field"),
        pytest.param(lambda p: {k: v for k, v in p.items() if k != "username"}, id="removed-field"),
        pytest.param(lambda p: {k: v for k, v in p.items() if k != "hash"}, id="missing-hash"),
        pytest.param(lambda p: {**p, "hash": ""}, id="empty-hash"),
        pytest.param(lambda p: {**p, "hash": p["hash"].upper()}, id="uppercase-hash"),
        pytest.param(lambda p: {**p, "hash": "z" * 64}, id="non-hex-hash"),
        pytest.param(lambda p: {**p, "hash": p["hash"][:-1]}, id="short-hash"),
        pytest.param(lambda p: {**p, "username": True}, id="bool-value"),
        pytest.param(lambda p: {**p, "username": None}, id="null-value"),
        pytest.param(lambda p: {**p, "username": {"a": 1}}, id="object-value"),
        pytest.param(lambda p: {**p, "username": "x" * 600}, id="oversized-value"),
        pytest.param(lambda p: {}, id="empty"),
    ],
)
def test_tampered_or_malformed_payloads_are_rejected(mutate):
    with pytest.raises(web_sessions.InvalidLogin):
        web_sessions.verify_login(mutate(widget(FRIEND)), TOKEN)


def test_wrong_bot_token_and_no_bot_token():
    payload = signed({"id": int(FRIEND), "first_name": "A", "auth_date": int(time.time())}, "999:OTHER-bot")
    with pytest.raises(web_sessions.InvalidLogin):
        web_sessions.verify_login(payload, TOKEN)
    with pytest.raises(web_sessions.InvalidLogin):
        web_sessions.verify_login(widget(FRIEND), "")


def test_stale_and_future_auth_date():
    now = int(time.time())
    assert web_sessions.verify_login(widget(FRIEND, now - 9 * 60), TOKEN)
    for auth_date in (now - 11 * 60, now - 86400, now + 5 * 60, 0):
        with pytest.raises(web_sessions.InvalidLogin):
            web_sessions.verify_login(widget(FRIEND, auth_date), TOKEN)


def test_a_group_id_is_never_a_user():
    with pytest.raises(web_sessions.InvalidLogin):
        web_sessions.verify_login(widget(-1001234567890), TOKEN)


# ----------------------------------------------------------------------- API


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
    monkeypatch.setattr(telegram_settings, "bot_token", TOKEN)
    monkeypatch.setattr(telegram_settings, "chat_id", OWNER)
    main.app.dependency_overrides[get_sync_db] = sync_db
    main.app.dependency_overrides[get_db] = async_db
    # https, so the client sends the Secure cookie back like a browser would.
    yield TestClient(main.app, base_url="https://testserver")
    main.app.dependency_overrides.clear()


@pytest.fixture
def two(session):
    member = add_member(session, FRIEND, name="Alem")
    other = add_member(session, OTHER, name="Sara")
    jobs = [add_job(session, f"Backend Engineer {i}", company=f"Co{i}", score=70 + i) for i in range(3)]
    return owner(session), member, other, jobs


def login(api, telegram_id=FRIEND, **kw):
    return api.post("/api/auth/telegram", json=widget(telegram_id, **kw), headers=CSRF)


def rows(session, user_id=None):
    session.expire_all()
    stmt = select(WebSession) if user_id is None else select(WebSession).where(WebSession.user_id == user_id)
    return list(session.execute(stmt).scalars())


def test_member_login_sets_a_hardened_cookie_and_stores_only_a_hash(api, session, two):
    _, member, _, _ = two
    resp = login(api)
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": True, "role": "member", "display_name": "Alem", "via": "session"}
    assert resp.headers["cache-control"] == "no-store"
    cookie = resp.headers["set-cookie"]
    attrs = [a.strip().lower() for a in cookie.split(";")]
    name, value = cookie.split(";")[0].split("=", 1)
    assert name == "ff_session" and len(value) >= 43  # 32 random bytes, base64url
    assert {"httponly", "secure", "samesite=lax", "path=/", f"max-age={30 * 86400}"} <= set(attrs)
    assert not any(a.startswith("domain=") for a in attrs)  # host-only: the dashboard's own domain
    (row,) = rows(session, member.id)
    assert row.token_hash == hashlib.sha256(value.encode()).hexdigest() and value not in row.token_hash
    assert api.get("/api/auth/session").json()["display_name"] == "Alem"


def test_owner_logs_in_as_owner(api, session, two):
    assert login(api, OWNER).json()["role"] == "owner"
    assert api.get("/api/me").json() == {"role": "owner", "display_name": "Owner"}


def test_strangers_and_bad_payloads_get_no_session_and_learn_nothing(api, session, two, monkeypatch):
    stranger = login(api, "999111")
    assert (stranger.status_code, stranger.json()) == (403, NOT_ALLOWED)
    users.remove_member(session, users.by_chat(session, FRIEND).id)
    session.commit()
    removed = login(api, FRIEND)
    assert (removed.status_code, removed.json()) == (403, NOT_ALLOWED)  # same answer: no hint who exists
    assert "set-cookie" not in stranger.headers and "set-cookie" not in removed.headers
    tampered = {**widget(OTHER), "id": 999111}
    assert api.post("/api/auth/telegram", json=tampered, headers=CSRF).status_code == 401
    stale = api.post("/api/auth/telegram", json=widget(OTHER, int(time.time()) - 3600), headers=CSRF)
    assert stale.status_code == 401
    assert api.post("/api/auth/telegram", json=["x"], headers=CSRF).status_code == 422
    assert rows(session) == []
    monkeypatch.setattr(telegram_settings, "bot_token", "")
    assert login(api, OTHER).status_code == 503


def test_owner_with_a_group_chat_cannot_web_login(api, session, monkeypatch):
    monkeypatch.setattr(telegram_settings, "chat_id", "-1001234567890")
    users.ensure_owner(session, "-1001234567890")
    session.commit()
    resp = api.post("/api/auth/telegram", json=signed({"id": -1001234567890, "auth_date": int(time.time())}))
    assert resp.status_code == 403  # no CSRF header
    resp = api.post(
        "/api/auth/telegram", json=signed({"id": -1001234567890, "auth_date": int(time.time())}), headers=CSRF
    )
    assert resp.status_code == 401


def test_the_session_is_a_third_way_to_be_the_viewer(api, session, two):
    me, member, other, jobs = two
    tracker.set_status(session, member.id, jobs[0].id, "saved", notes="mine", now=NOW)
    tracker.set_status(session, other.id, jobs[1].id, "interviewing", notes="not yours", now=NOW)
    tracker.set_status(session, me.id, jobs[2].id, "offer", notes="owner's", now=NOW)
    talent.set_status(session, member.id, "andela", "applied", NOW)
    session.commit()
    assert api.get("/api/applications").status_code == 401
    login(api)
    apps = api.get("/api/applications").json()
    assert [(a["job_id"], a["notes"]) for a in apps] == [(jobs[0].id, "mine")]
    assert api.get("/api/me").json() == {"role": "member", "display_name": "Alem"}
    nets = {n["slug"]: n.get("status") for n in api.get("/api/talent-networks").json()}
    assert nets["andela"] == "applied"
    items = api.get("/api/jobs").json()["items"]
    assert {i["analysis"]["extracted_json"]["score_scope"] for i in items} == {"user"}  # ranked for them
    assert api.get("/api/jobs", params={"status": "interviewing"}).json()["total"] == 0
    assert api.get(f"/api/jobs/{jobs[0].id}").json()["analysis"]["extracted_json"]["score_scope"] == "user"


def test_cookie_writes_need_the_csrf_header(api, session, two, monkeypatch):
    _, member, _, jobs = two
    login(api)
    url = f"/api/applications/{jobs[0].id}"
    assert api.patch(url, json={"status": "applied"}).status_code == 403
    assert api.patch(url, json={"status": "applied"}, headers={"X-Requested-With": "XMLHttpRequest"}).status_code == 403
    assert api.patch("/api/talent-networks/arc", json={"status": "vetted"}).status_code == 403
    session.expire_all()
    assert tracker.get_application(session, member.id, jobs[0].id) is None
    # Without ADMIN_TOKEN the middleware lets everything through; the dependency still refuses.
    monkeypatch.setattr(app_settings, "admin_token", "")
    resp = api.patch(url, json={"status": "applied"})
    assert resp.status_code == 403 and "X-Requested-With" in resp.json()["detail"]
    assert api.patch(url, json={"status": "applied"}, headers=CSRF).status_code == 200
    session.expire_all()
    assert tracker.get_application(session, member.id, jobs[0].id).status == "applied"
    # Reads need no header.
    assert api.get("/api/applications").status_code == 200


def test_login_logout_and_conversion_need_the_csrf_header(api, session, two):
    assert api.post("/api/auth/telegram", json=widget(FRIEND)).status_code == 403
    assert rows(session) == []
    login(api)
    assert api.post("/api/auth/logout").status_code == 403
    assert api.get("/api/auth/session").json()["authenticated"] is True
    token = users.issue_dashboard_token(session, two[2])
    session.commit()
    assert api.post("/api/auth/token-session", headers={"X-User-Token": token}).status_code == 403


def test_cookie_writes_go_only_to_the_users_own_tracker(api, session, two):
    me, member, other, jobs = two
    login(api)
    assert api.patch(f"/api/applications/{jobs[1].id}", json={"status": "offer"}, headers=CSRF).status_code == 200
    assert api.patch("/api/talent-networks/arc", json={"status": "vetted"}, headers=CSRF).status_code == 200
    session.expire_all()
    assert tracker.get_application(session, member.id, jobs[1].id).status == "offer"
    for uid in (me.id, other.id):
        assert tracker.get_application(session, uid, jobs[1].id) is None
        assert {n["slug"]: n["status"] for n in talent.list_networks(session, uid)}["arc"] == "not_started"
    # Another user's session sees none of it.
    api.cookies.clear()
    login(api, OTHER)
    assert api.get("/api/applications").json() == []
    assert api.get("/api/jobs", params={"status": "offer"}).json()["total"] == 0


def test_headers_win_over_the_cookie(api, session, two):
    _, member, other, jobs = two
    tracker.set_status(session, other.id, jobs[2].id, "saved", now=NOW)
    token = users.issue_dashboard_token(session, other)
    session.commit()
    login(api)
    assert api.get("/api/me", headers={"X-User-Token": token}).json()["display_name"] == "Sara"
    assert api.get("/api/me", headers={"X-User-Token": "wrong"}).status_code == 401  # no fallback
    assert api.get("/api/me", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert api.get("/api/me", headers={"X-Admin-Token": "adm"}).json()["role"] == "owner"
    assert api.get("/api/me").json()["display_name"] == "Alem"


def test_owner_session_is_not_the_admin_token(api, session, two):
    me, _, _, jobs = two
    login(api, OWNER)
    assert api.patch(f"/api/applications/{jobs[0].id}", json={"status": "saved"}, headers=CSRF).status_code == 200
    assert api.patch("/api/talent-networks/turing", json={"status": "vetted"}, headers=CSRF).status_code == 200
    for method, url, body in (
        ("post", "/api/analyze", {}),
        ("post", "/api/scrape", {}),
        ("delete", "/api/jobs", None),
        ("delete", "/api/runs", None),
        ("patch", "/api/sources/1", {"enabled": False}),
        ("post", "/api/jobs/test", None),
    ):
        resp = api.request(method.upper(), url, json=body, headers=CSRF)
        assert resp.status_code == 403 and resp.json() == {"detail": "This demo is read-only"}, url
    session.expire_all()
    assert {n["slug"]: n["status"] for n in talent.list_networks(session, me.id)}["turing"] == "vetted"


def test_expiry_and_sliding_refresh(api, session, two, monkeypatch):
    _, member, _, _ = two
    t0 = datetime.now(UTC)
    monkeypatch.setattr(web_sessions, "utcnow", lambda: t0)
    login(api)
    (row,) = rows(session, member.id)
    assert abs((web_sessions._aware(row.expires_at) - (t0 + timedelta(days=30))).total_seconds()) < 1

    # Within the hour: no write, no new cookie.
    monkeypatch.setattr(web_sessions, "utcnow", lambda: t0 + timedelta(minutes=30))
    resp = api.get("/api/me")
    assert resp.status_code == 200 and "set-cookie" not in resp.headers
    (row,) = rows(session, member.id)
    assert abs((web_sessions._aware(row.last_seen) - t0).total_seconds()) < 1

    # 29 days later: still valid; the expiry slides to 30 days from now and the cookie is sent again.
    later = t0 + timedelta(days=29)
    monkeypatch.setattr(web_sessions, "utcnow", lambda: later)
    resp = api.get("/api/me")
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"].lower()
    assert "max-age=2592000" in cookie and "httponly" in cookie and "secure" in cookie
    (row,) = rows(session, member.id)
    assert abs((web_sessions._aware(row.expires_at) - (later + timedelta(days=30))).total_seconds()) < 1

    # Refreshed through the jobs list too (an async route).
    monkeypatch.setattr(web_sessions, "utcnow", lambda: later + timedelta(days=2))
    assert "set-cookie" in api.get("/api/jobs").headers

    # Unused for more than 30 days: gone.
    monkeypatch.setattr(web_sessions, "utcnow", lambda: later + timedelta(days=2, seconds=30 * 86400 + 1))
    assert api.get("/api/me").status_code == 401
    assert rows(session, member.id) == []
    resp = api.get("/api/auth/session")
    assert resp.json() == {"authenticated": False}
    assert 'ff_session=""' in resp.headers["set-cookie"] or "max-age=0" in resp.headers["set-cookie"].lower()


def test_logout_deletes_the_session_and_clears_the_cookie(api, session, two):
    _, member, _, _ = two
    login(api)
    value = api.cookies.get("ff_session")
    resp = api.post("/api/auth/logout", headers=CSRF)
    assert resp.status_code == 204
    set_cookie = resp.headers["set-cookie"].lower()
    assert set_cookie.startswith("ff_session=") and "max-age=0" in set_cookie and "secure" in set_cookie
    assert rows(session, member.id) == []
    api.cookies.set("ff_session", value)  # a copy kept elsewhere doesn't work either
    assert api.get("/api/me").status_code == 401
    assert api.post("/api/auth/logout", headers=CSRF).status_code == 204  # idempotent


def test_login_again_replaces_this_browsers_session(api, session, two):
    _, member, _, _ = two
    login(api)
    first = api.cookies.get("ff_session")
    login(api)
    assert api.cookies.get("ff_session") != first
    assert len(rows(session, member.id)) == 1


def test_remove_and_token_rotation_revoke_sessions(api, session, two, fake_tg):
    _, member, other, _ = two
    login(api)
    member_cookie = api.cookies.get("ff_session")
    api.cookies.clear()
    login(api, OTHER)
    other_cookie = api.cookies.get("ff_session")
    api.cookies.clear()

    def me(cookie):
        return api.get("/api/me", cookies={"ff_session": cookie}).status_code

    assert me(member_cookie) == me(other_cookie) == 200
    # A new /dashboard link logs that user out everywhere (and nobody else).
    s = settings(dashboard_url="https://dash.example.app")
    bot.handle_update(session, fake_tg.client(), s, message_update("/dashboard", chat_id=OTHER), now=NOW)
    assert me(other_cookie) == 401 and me(member_cookie) == 200
    api.cookies.clear()
    login(api, OTHER)
    other_cookie = api.cookies.get("ff_session")
    api.cookies.clear()
    bot.handle_update(session, fake_tg.client(), s, message_update("/dashboard revoke", chat_id=OTHER), now=NOW)
    assert me(other_cookie) == 401
    # /remove by the owner ends the member's sessions with the rest of their data.
    bot.handle_update(session, fake_tg.client(), s, message_update(f"/remove {member.id}"), now=NOW)
    assert me(member_cookie) == 401
    assert rows(session, member.id) == []


def test_a_dashboard_link_converts_to_a_session(api, session, two):
    _, member, _, _ = two
    token = users.issue_dashboard_token(session, member)
    session.commit()
    assert api.post("/api/auth/token-session", headers={**CSRF, "X-User-Token": "nope"}).status_code == 401
    resp = api.post("/api/auth/token-session", headers={**CSRF, "X-User-Token": token})
    assert resp.status_code == 200 and resp.json()["display_name"] == "Alem"
    assert "httponly" in resp.headers["set-cookie"].lower()
    assert api.get("/api/me").json()["display_name"] == "Alem"
    assert api.get("/api/auth/session").json()["via"] == "session"
    # The link keeps working on its own too.
    assert api.get("/api/me", headers={"X-User-Token": token}, cookies={}).status_code == 200


def test_public_session_probe_is_quiet(api, session, two):
    resp = api.get("/api/auth/session")
    assert resp.status_code == 200 and resp.json() == {"authenticated": False}
    assert "set-cookie" not in resp.headers and resp.headers["cache-control"] == "no-store"
    api.cookies.set("ff_session", "forged-" + "x" * 40)
    assert api.get("/api/auth/session").json() == {"authenticated": False}
    assert api.get("/api/applications").status_code == 401
    assert api.patch("/api/applications/1", json={"status": "saved"}, headers=CSRF).status_code == 401
