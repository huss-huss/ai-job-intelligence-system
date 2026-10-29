"""/fresh <n>: the newest open matches as cards; and the Applied button keeps a job out of every Telegram list
(/top, /today, /fresh, the digest) until Undo."""

from datetime import timedelta

import pytest
from app.telegram import bot, digest

from tests.tracker_fixtures import NOW, add_job, callback_update, message_update, owner, settings


def run(session, fake_tg, text_or_update, **overrides):
    update = message_update(text_or_update) if isinstance(text_or_update, str) else text_or_update
    return bot.handle_update(session, fake_tg.client(), settings(**overrides), update, now=NOW)


def cards(fake_tg) -> list[str]:
    """Titles of the job cards (messages with buttons) sent so far, in order."""
    return [b["text"].split("\n")[0] for m, b in fake_tg.requests if m == "sendMessage" and b.get("reply_markup")]


def texts(fake_tg) -> list[str]:
    return [b["text"] for m, b in fake_tg.requests if m == "sendMessage"]


def jobs_by_age(session, hours: list[int]):
    def one(h: int):
        when = NOW - timedelta(hours=h)
        return add_job(session, f"Backend Engineer {h}h", company=f"Co{h}", score=70, created_at=when)

    return [one(h) for h in hours]


# ----------------------------------------------------------------------- /fresh


def test_fresh_sends_the_newest_open_matches_as_cards(session, fake_tg):
    jobs_by_age(session, [30, 2, 10, 50, 5, 1])
    assert run(session, fake_tg, "/fresh 3") == "command:fresh"
    assert texts(fake_tg)[0] == "<b>3 freshest open matches</b> (newest first, not yet acted on)"
    assert cards(fake_tg) == ["<b>Backend Engineer 1h</b>", "<b>Backend Engineer 2h</b>", "<b>Backend Engineer 5h</b>"]
    first_card = next(b for m, b in fake_tg.requests if b.get("reply_markup"))
    labels = [btn["text"] for row in first_card["reply_markup"]["inline_keyboard"] for btn in row]
    assert any("Applied" in label for label in labels)  # same buttons as /top


def test_fresh_prefers_the_boards_posting_time(session, fake_tg):
    old_post, new_post = jobs_by_age(session, [1, 2])
    old_post.posted_at = NOW - timedelta(days=3)  # stored an hour ago, but posted 3 days ago
    session.commit()
    run(session, fake_tg, "/fresh 2")
    assert cards(fake_tg) == ["<b>Backend Engineer 2h</b>", "<b>Backend Engineer 1h</b>"]


def test_fresh_skips_acted_on_weak_unknown_and_old_jobs(session, fake_tg):
    me = owner(session)
    keep = add_job(session, "Keep me", company="K", score=70)
    for status in ("applied", "saved", "not_interested"):
        job = add_job(session, f"Already {status}", company=status, score=90)
        run(session, fake_tg, callback_update(f"s:{job.id}:{status}"))
    add_job(session, "Weak match", company="W", score=30)
    add_job(session, "Unknown location", company="U", score=90, eligible=None)  # strict Telegram
    add_job(session, "Too old", company="O", score=90, created_at=NOW - timedelta(days=40))
    fake_tg.requests.clear()
    run(session, fake_tg, "/fresh 15")
    assert cards(fake_tg) == ["<b>Keep me</b>"]
    assert keep.id and me.id


def test_fresh_defaults_to_five_and_rejects_bad_input(session, fake_tg):
    jobs_by_age(session, list(range(1, 9)))
    run(session, fake_tg, "/fresh")
    assert len(cards(fake_tg)) == 5
    for bad in ("/fresh 0", "/fresh 16", "/fresh ten", "/fresh 3 4"):
        fake_tg.requests.clear()
        run(session, fake_tg, bad)
        assert texts(fake_tg) == ["Usage: /fresh &lt;n&gt; (1-15), e.g. /fresh 10"] and cards(fake_tg) == []


def test_fresh_with_nothing_to_show(session, fake_tg):
    run(session, fake_tg, "/fresh 5")
    assert texts(fake_tg) == ["No fresh open matches you haven't acted on yet."]


# ----------------------------------------------------------------------- Applied keeps a job away


@pytest.mark.parametrize("command", ["/top", "/today", "/fresh 10"])
def test_applied_job_never_comes_back_until_undo(session, fake_tg, command):
    applied = add_job(session, "Applied Engineer", company="A", score=95)
    add_job(session, "Other Engineer", company="B", score=80)
    run(session, fake_tg, callback_update(f"s:{applied.id}:applied"))
    fake_tg.requests.clear()
    run(session, fake_tg, command)
    sent = " ".join(texts(fake_tg))
    assert "Other Engineer" in sent and "Applied Engineer" not in sent
    run(session, fake_tg, callback_update(f"s:{applied.id}:new"))  # Undo
    fake_tg.requests.clear()
    run(session, fake_tg, command)
    assert "Applied Engineer" in " ".join(texts(fake_tg))


def test_applied_job_is_not_in_the_digest(session, fake_tg):
    applied = add_job(session, "Applied Engineer", company="A", score=95)
    add_job(session, "Other Engineer", company="B", score=80)
    run(session, fake_tg, callback_update(f"s:{applied.id}:applied"))
    fake_tg.requests.clear()
    digest.send_digest(session, fake_tg.client(), settings(), NOW, owner(session))
    assert cards(fake_tg) == ["<b>Other Engineer</b>"]
