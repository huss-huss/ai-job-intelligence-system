"""One-time login links from the bot (/login, /start login): what the bot sends, what is stored, and
that a link logs in exactly once, only before it expires, only for an invited user. No network."""

import hashlib
from datetime import timedelta
from urllib.parse import urlsplit

from app import users, web_sessions
from app.models import AuthThrottle, LoginLink
from app.telegram import bot
from sqlalchemy import func, select

from tests.test_web_login import CSRF, FRIEND, OTHER, api, rows, two  # noqa: F401 - fixtures
from tests.tracker_fixtures import NOW, message_update, settings

DASH = "https://dash.example.app"


def links(session, user_id=None):
    session.expire_all()
    stmt = select(LoginLink) if user_id is None else select(LoginLink).where(LoginLink.user_id == user_id)
    return list(session.execute(stmt).scalars())


def sent_link(fake_tg) -> str:
    """The URL on the button of the last message the bot sent."""
    method, body = fake_tg.requests[-1]
    assert method == "sendMessage"
    ((button,),) = body["reply_markup"]["inline_keyboard"]
    return button["url"]


def token_of(url: str) -> str:
    parts = urlsplit(url)
    assert parts.path == "/login" and parts.fragment.startswith("link=") and not parts.query
    return parts.fragment.removeprefix("link=")


def redeem(api, token, headers=CSRF):  # noqa: F811
    return api.post("/api/auth/link", json={"token": token}, headers=headers)


def test_login_sends_a_one_time_button_and_stores_only_a_hash(session, two, fake_tg):  # noqa: F811
    _, member, _, _ = two
    s = settings(dashboard_url=DASH)
    assert bot.handle_update(session, fake_tg.client(), s, message_update("/login", FRIEND)) == "command:login"
    url = sent_link(fake_tg)
    assert url.startswith(f"{DASH}/login#link=")
    token = token_of(url)
    assert len(token) >= 43  # 32 random bytes, base64url
    (row,) = links(session, member.id)
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert "don't forward it" in fake_tg.requests[-1][1]["text"]


def test_start_login_is_the_same_as_login(session, two, fake_tg):  # noqa: F811
    _, member, _, _ = two
    s = settings(dashboard_url=DASH)
    assert bot.handle_update(session, fake_tg.client(), s, message_update("/start login", FRIEND)) == "command:start"
    token_of(sent_link(fake_tg))
    assert len(links(session, member.id)) == 1
    bot.handle_update(session, fake_tg.client(), s, message_update("/start", FRIEND))
    assert "reply_markup" not in fake_tg.requests[-1][1] or not fake_tg.requests[-1][1]["reply_markup"]


def test_a_stranger_pressing_the_button_gets_nothing_and_no_invite_attempt(session, two, fake_tg):  # noqa: F811
    s = settings(dashboard_url=DASH)
    before = session.scalar(select(func.count()).select_from(AuthThrottle))
    assert bot.handle_update(session, fake_tg.client(), s, message_update("/start login", "999111")) == (
        "ignored:unknown-chat"
    )
    assert fake_tg.requests == [] and links(session) == []
    assert session.scalar(select(func.count()).select_from(AuthThrottle)) == before


def test_no_dashboard_url_means_no_link(session, two, fake_tg):  # noqa: F811
    bot.handle_update(session, fake_tg.client(), settings(dashboard_url=""), message_update("/login", FRIEND))
    assert "TELEGRAM_DASHBOARD_URL" in fake_tg.requests[-1][1]["text"] and links(session) == []


def test_the_bot_link_logs_in_once(api, session, two, fake_tg):  # noqa: F811
    _, member, _, _ = two
    bot.handle_update(session, fake_tg.client(), settings(dashboard_url=DASH), message_update("/login", FRIEND))
    token = token_of(sent_link(fake_tg))
    assert redeem(api, token, headers={}).status_code == 403  # CSRF header required
    resp = redeem(api, token)
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": True, "role": "member", "display_name": "Alem", "via": "session"}
    assert "httponly" in resp.headers["set-cookie"].lower() and resp.headers["cache-control"] == "no-store"
    assert api.get("/api/auth/session").json()["display_name"] == "Alem"
    assert len(rows(session, member.id)) == 1 and links(session) == []
    api.cookies.clear()
    again = redeem(api, token)
    assert again.status_code == 401 and "set-cookie" not in again.headers


def test_wrong_expired_and_oversized_links_are_refused(api, session, two):  # noqa: F811
    _, member, _, _ = two
    assert redeem(api, "nope").status_code == 401
    assert redeem(api, "x" * 500).status_code == 401
    old = web_sessions.create_login_link(session, member, web_sessions.utcnow() - timedelta(minutes=11))
    session.commit()
    assert redeem(api, old).status_code == 401
    assert links(session) == []  # an expired link is deleted when it's tried
    assert rows(session, member.id) == []


def test_links_expire_after_ten_minutes(session, two):  # noqa: F811
    _, member, _, _ = two
    token = web_sessions.create_login_link(session, member, NOW)
    assert web_sessions.consume_login_link(session, token, NOW + timedelta(minutes=11)) is None
    token = web_sessions.create_login_link(session, member, NOW)
    assert web_sessions.consume_login_link(session, token, NOW + timedelta(minutes=9)).id == member.id


def test_logging_out_everywhere_and_removal_void_unused_links(api, session, two, fake_tg):  # noqa: F811
    owner, member, other, _ = two
    s = settings(dashboard_url=DASH)
    other_token = web_sessions.create_login_link(session, other)
    member_token = web_sessions.create_login_link(session, member)
    session.commit()
    bot.handle_update(session, fake_tg.client(), s, message_update("/dashboard revoke", chat_id=OTHER))
    assert redeem(api, other_token).status_code == 401
    bot.handle_update(session, fake_tg.client(), s, message_update(f"/remove {member.id}"))
    assert users.by_chat(session, FRIEND) is None
    assert redeem(api, member_token).status_code == 401
    assert links(session) == []
