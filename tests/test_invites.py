"""Invite-only membership: /invite, /start <code>, single use, expiry, brute-force lockout, owner-only
commands, /users and /remove (SQLite + fake Bot API)."""

from datetime import timedelta

from app import tracker, users
from app.models import Application, AuthThrottle, Invite, User, UserJobScore
from app.telegram import bot
from sqlalchemy import func, select

from tests.tracker_fixtures import NOW, OWNER, add_job, callback_update, message_update, oid, settings

FRIEND = "555001"
STRANGER = "999999"


def run(session, fake_tg, text, chat_id=OWNER, now=NOW, first_name="Friend"):
    update = message_update(text, chat_id=chat_id)
    update["message"]["from"] = {"id": int(chat_id), "first_name": first_name}
    return bot.handle_update(session, fake_tg.client(), settings(), update, now=now)


def invite(session, fake_tg, now=NOW) -> str:
    assert run(session, fake_tg, "/invite", now=now) == "command:invite"
    text = fake_tg.sent()[-1]["text"]
    return text.split("<code>/start ")[1].split("</code>")[0]


def test_invite_creates_a_hashed_single_use_code_with_a_deep_link(session, fake_tg):
    code = invite(session, fake_tg)
    text = fake_tg.sent()[-1]["text"]
    assert f"https://t.me/FirstFetchTestBot?start={code}" in text and "expires 21 Oct 09:00" in text
    assert len(code) == 32 and users.CODE_RE.fullmatch(code)  # 24 random bytes = 192 bits
    (row,) = session.execute(select(Invite)).scalars()
    assert row.code_hash == users.hash_secret(code) and code not in row.code_hash
    assert row.expires_at.replace(tzinfo=NOW.tzinfo) == NOW + timedelta(hours=48)
    assert invite(session, fake_tg) != code  # every invite is a fresh random code


def test_new_chat_joins_with_the_code_and_gets_a_copy_of_the_owner_profile(session, fake_tg):
    add_job(session)
    code = invite(session, fake_tg)
    fake_tg.requests.clear()
    assert run(session, fake_tg, f"/start {code}", chat_id=FRIEND, first_name="Abebe") == "invite:joined"
    member = users.by_chat(session, FRIEND)
    assert member.role == "member" and member.display_name == "Abebe"
    owner_profile = users.profile_model(users.get_owner(session)).model_dump(mode="json")
    assert member.profile == owner_profile
    welcome, notice = fake_tg.sent()
    assert welcome["chat_id"] == FRIEND and "Welcome to <b>FirstFetch</b>" in welcome["text"]
    assert "/invite" not in welcome["text"]  # owner commands aren't advertised to members
    assert notice["chat_id"] == OWNER and "Abebe joined" in notice["text"]
    # The member is a user now: commands work.
    assert run(session, fake_tg, "/help", chat_id=FRIEND) == "command:help"


def test_a_code_works_once(session, fake_tg):
    code = invite(session, fake_tg)
    assert run(session, fake_tg, f"/start {code}", chat_id=FRIEND) == "invite:joined"
    fake_tg.requests.clear()
    assert run(session, fake_tg, f"/start {code}", chat_id="555002") == "invite:invalid"
    assert fake_tg.sent()[0]["text"] == "This invite code is invalid, already used or expired."
    assert users.by_chat(session, "555002") is None


def test_an_expired_code_is_refused(session, fake_tg):
    code = invite(session, fake_tg)
    late = NOW + timedelta(hours=48, seconds=1)
    assert run(session, fake_tg, f"/start {code}", chat_id=FRIEND, now=late) == "invite:invalid"
    assert users.by_chat(session, FRIEND) is None
    # Just inside the window still works.
    code = invite(session, fake_tg)
    assert run(session, fake_tg, f"/start {code}", chat_id=FRIEND, now=NOW + timedelta(hours=47)) == "invite:joined"


def test_unknown_chats_get_no_reply_at_all(session, fake_tg):
    job = add_job(session)
    for text in ("/start", "/top", "hello", "/help", "/start a b", "/invite"):
        assert run(session, fake_tg, text, chat_id=STRANGER) == "ignored:unknown-chat"
    assert (
        bot.handle_update(
            session, fake_tg.client(), settings(), callback_update(f"s:{job.id}:applied", chat_id=STRANGER), now=NOW
        )
        == "ignored:unknown-chat"
    )
    group = message_update("/start whatever-code-here-1234", chat_id="-100123")
    group["message"]["chat"]["type"] = "group"
    assert bot.handle_update(session, fake_tg.client(), settings(), group, now=NOW) == "ignored:unknown-chat"
    assert fake_tg.requests == []
    assert session.scalar(select(func.count()).select_from(Application)) == 0


def test_brute_force_locks_the_chat_out_silently(session, fake_tg):
    real = invite(session, fake_tg)
    fake_tg.requests.clear()
    for i in range(users.CHAT_MAX_FAILURES):
        assert run(session, fake_tg, f"/start guess{i:028d}", chat_id=STRANGER) == "invite:invalid"
    assert len(fake_tg.sent()) == users.CHAT_MAX_FAILURES
    fake_tg.requests.clear()
    # Locked: even the right code is refused, and nothing is sent back.
    assert run(session, fake_tg, f"/start {real}", chat_id=STRANGER) == "invite:locked"
    assert fake_tg.requests == [] and users.by_chat(session, STRANGER) is None
    # Other chats aren't affected, and the lock ends after an hour.
    assert run(session, fake_tg, f"/start {real}", chat_id=FRIEND) == "invite:joined"
    code = invite(session, fake_tg)
    later = NOW + users.CHAT_LOCKOUT + timedelta(seconds=1)
    assert run(session, fake_tg, f"/start {code}", chat_id=STRANGER, now=later) == "invite:joined"
    # The throttle stores a hash of the chat id, never the id.
    keys = [r.key for r in session.execute(select(AuthThrottle)).scalars()]
    assert keys and not any(STRANGER in k for k in keys)


def test_failures_spread_over_time_do_not_lock(session, fake_tg):
    code = invite(session, fake_tg)
    for start in (NOW, NOW + users.CHAT_WINDOW + timedelta(seconds=1)):
        for i in range(users.CHAT_MAX_FAILURES - 1):
            assert run(session, fake_tg, f"/start nope{i:028d}", chat_id=STRANGER, now=start) == "invite:invalid"
    later = NOW + users.CHAT_WINDOW + timedelta(seconds=2)
    assert run(session, fake_tg, f"/start {code}", chat_id=STRANGER, now=later) == "invite:joined"


def test_global_lockout_stops_many_chats_guessing(session, fake_tg):
    code = invite(session, fake_tg)
    for i in range(users.GLOBAL_MAX_FAILURES):
        run(session, fake_tg, f"/start nope{i:028d}", chat_id=str(800000 + i))
    assert run(session, fake_tg, f"/start {code}", chat_id=FRIEND) == "invite:locked"
    later = NOW + users.GLOBAL_LOCKOUT + timedelta(seconds=1)
    assert run(session, fake_tg, f"/start {code}", chat_id=FRIEND, now=later) == "invite:joined"


def test_malformed_codes_count_as_failures(session, fake_tg):
    for text in ("/start x", "/start " + "a" * 80, "/start <script>aaaaaaaaaaaaaaaa"):
        assert run(session, fake_tg, text, chat_id=STRANGER) == "invite:invalid"
    row = session.get(AuthThrottle, users._chat_key(STRANGER))
    assert row.failures == 3


def test_owner_only_commands_are_unknown_to_members(session, fake_tg):
    code = invite(session, fake_tg)
    run(session, fake_tg, f"/start {code}", chat_id=FRIEND)
    fake_tg.requests.clear()
    member_id = users.by_chat(session, FRIEND).id
    for text in ("/invite", "/users", f"/remove {oid(session)}", f"/remove {member_id}"):
        assert run(session, fake_tg, text, chat_id=FRIEND) == "command:unknown"
    assert all(b["text"] == "Unknown command. /help lists what I can do." for b in fake_tg.sent())
    assert session.scalar(select(func.count()).select_from(Invite)) == 1  # no new invite
    assert users.by_chat(session, FRIEND) is not None


def test_users_lists_everyone_without_chat_ids(session, fake_tg):
    code = invite(session, fake_tg)
    run(session, fake_tg, f"/start {code}", chat_id=FRIEND, first_name="Abebe <b>")
    assert run(session, fake_tg, "/users") == "command:users"
    text = fake_tg.sent()[-1]["text"]
    assert "Owner · owner" in text and "Abebe &lt;b&gt; · member" in text
    assert FRIEND not in text and OWNER not in text


def test_remove_deletes_the_member_and_all_their_data(session, fake_tg):
    job = add_job(session)
    code = invite(session, fake_tg)
    run(session, fake_tg, f"/start {code}", chat_id=FRIEND)
    member = users.by_chat(session, FRIEND)
    tracker.set_status(session, member.id, job.id, "applied", notes="their note", now=NOW)
    tracker.set_status(session, oid(session), job.id, "saved", now=NOW)
    users.issue_dashboard_token(session, member)
    session.commit()
    assert run(session, fake_tg, f"/remove {member.id}") == "command:remove"
    assert "Removed Friend" in fake_tg.sent()[-1]["text"]
    session.expire_all()
    assert session.get(User, member.id) is None
    assert not session.scalars(select(Application).where(Application.user_id == member.id)).all()
    assert not session.scalars(select(UserJobScore).where(UserJobScore.user_id == member.id)).all()
    assert tracker.get_application(session, oid(session), job.id).status == "saved"  # the owner's stays
    (used,) = session.execute(select(Invite)).scalars()
    assert used.used_by is None and used.used_at is not None
    # Their chat is a stranger again: ignored, and the old code doesn't let them back in.
    fake_tg.requests.clear()
    assert run(session, fake_tg, "/top", chat_id=FRIEND) == "ignored:unknown-chat"
    assert run(session, fake_tg, f"/start {code}", chat_id=FRIEND) == "invite:invalid"


def test_remove_refuses_the_owner_and_unknown_ids(session, fake_tg):
    run(session, fake_tg, f"/remove {oid(session)}")
    run(session, fake_tg, "/remove 4242")
    run(session, fake_tg, "/remove")
    texts = [b["text"] for b in fake_tg.sent()]
    assert texts == ["The owner can&#x27;t be removed", "No user #4242.", "Usage: /remove &lt;user id&gt; (see /users)"]


def test_owner_chat_follows_telegram_chat_id(session, fake_tg):
    owner = users.get_owner(session)
    assert owner.telegram_chat_id in (None, OWNER)
    run(session, fake_tg, "/help")
    assert users.get_owner(session).telegram_chat_id == OWNER
    # A new TELEGRAM_CHAT_ID moves the owner; the old chat becomes a stranger.
    update = message_update("/help", chat_id="111")
    assert bot.handle_update(session, fake_tg.client(), settings(chat_id="111"), update, now=NOW) == "command:help"
    assert bot.handle_update(session, fake_tg.client(), settings(chat_id="111"), message_update("/help"), now=NOW) == (
        "ignored:unknown-chat"
    )


def test_members_are_capped(session, fake_tg, monkeypatch):
    monkeypatch.setattr(users, "MAX_MEMBERS", 1)
    run(session, fake_tg, f"/start {invite(session, fake_tg)}", chat_id=FRIEND)
    assert run(session, fake_tg, f"/start {invite(session, fake_tg)}", chat_id="555002") == "invite:full"


def test_no_codes_chat_ids_or_names_in_the_logs(session, fake_tg, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    code = invite(session, fake_tg)
    run(session, fake_tg, "/start wrong-code-0000000000000000", chat_id=STRANGER, first_name="Mallory")
    run(session, fake_tg, f"/start {code}", chat_id=FRIEND, first_name="Abebe")
    run(session, fake_tg, "/users")
    member = users.by_chat(session, FRIEND)
    run(session, fake_tg, f"/remove {member.id}")
    for secret in (code, FRIEND, STRANGER, OWNER, "Abebe", "Mallory"):
        assert secret not in caplog.text
