"""Owner-only bot: setup mode, every command and the job-card buttons (SQLite + fake Bot API)."""

from datetime import timedelta

from app import tracker
from app.telegram import bot

from tests.tracker_fixtures import NOW, OWNER, add_job, callback_update, message_update, settings


def run(session, fake_tg, update, **overrides):
    return bot.handle_update(session, fake_tg.client(), settings(**overrides), update, now=NOW)


def texts(fake_tg):
    return [b["text"] for b in fake_tg.sent()]


def test_setup_mode_replies_to_start_with_the_chat_id_only(session, fake_tg):
    assert run(session, fake_tg, message_update("/start", chat_id="777"), chat_id="") == "setup:chat-id"
    (body,) = fake_tg.sent()
    assert body["chat_id"] == "777" and "<code>777</code>" in body["text"] and "TELEGRAM_CHAT_ID" in body["text"]
    fake_tg.requests.clear()
    add_job(session)
    for text in ("/top", "/stats", "hello"):
        assert run(session, fake_tg, message_update(text, chat_id="777"), chat_id="") == "ignored:setup"
    assert run(session, fake_tg, callback_update("s:1:applied", chat_id="777"), chat_id="") == "ignored:setup"
    assert fake_tg.requests == []


def test_other_chats_are_ignored(session, fake_tg):
    job = add_job(session)
    assert run(session, fake_tg, message_update("/top", chat_id="999")) == "ignored:not-owner"
    assert run(session, fake_tg, callback_update(f"s:{job.id}:applied", chat_id="999")) == "ignored:not-owner"
    assert fake_tg.requests == []
    assert tracker.get_application(session, job.id) is None


def test_start_and_help(session, fake_tg):
    assert run(session, fake_tg, message_update("/start")) == "command:start"
    assert run(session, fake_tg, message_update("/help@FirstFetchBot")) == "command:help"
    start, help_ = texts(fake_tg)
    assert start == help_ and "/top" in help_ and "/pause" in help_ and "scoring ≥ 60" in help_


def test_unknown_command_and_plain_text(session, fake_tg):
    assert run(session, fake_tg, message_update("/nope")) == "command:unknown"
    assert run(session, fake_tg, message_update("just chatting")) == "ignored:not-a-command"
    assert texts(fake_tg) == ["Unknown command. /help lists what I can do."]


def test_top_sends_five_best_unacted_open_matches_with_buttons(session, fake_tg):
    jobs = [add_job(session, f"Backend Engineer {i}", company=f"Co{i}", score=60 + i) for i in range(7)]
    tracker.set_status(session, jobs[6].id, "saved", now=NOW)  # acted on: skipped
    add_job(session, "Ineligible", company="X", score=99, eligible=False)
    assert run(session, fake_tg, message_update("/top")) == "command:top"
    header, *cards = fake_tg.sent()
    assert header["text"].startswith("<b>Top 5 open matches</b>")
    assert [c["text"].split("\n")[0] for c in cards] == [f"<b>Backend Engineer {i}</b>" for i in (5, 4, 3, 2, 1)]
    assert all(c["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("s:") for c in cards)
    # Shown jobs count as notified, so the digest won't repeat them.
    assert tracker.get_application(session, jobs[5].id).notified_at is not None


def test_top_with_nothing_to_show(session, fake_tg):
    run(session, fake_tg, message_update("/top"))
    assert texts(fake_tg) == ["No open matches you haven't acted on yet."]


def test_today_lists_jobs_since_yesterday(session, fake_tg):
    add_job(session, "Fresh <Backend>", company="A&B", score=70)
    add_job(session, "Weak", company="C", score=30)
    add_job(session, "Yesterday's news", score=90, created_at=NOW - timedelta(days=2))
    assert run(session, fake_tg, message_update("/today")) == "command:today"
    (text,) = texts(fake_tg)
    assert text.startswith("<b>2 new open jobs since yesterday</b> · 1 scoring ≥ 60")
    assert "Fresh &lt;Backend&gt;</a> · A&amp;B" in text and "Weak" in text and "Yesterday" not in text


def test_today_chunks_long_lists(session, fake_tg, monkeypatch):
    monkeypatch.setattr(bot, "TODAY_LIMIT", 500)
    for i in range(120):
        add_job(session, f"Backend Engineer with a fairly long title number {i}" + " x" * 20, company=f"Co {i}")
    run(session, fake_tg, message_update("/today"))
    messages = texts(fake_tg)
    assert len(messages) > 1 and all(len(m) <= 4096 for m in messages)
    assert sum(m.count("<code>#") for m in messages) == 120


def test_applied_lists_tracked_jobs_by_status(session, fake_tg):
    a = add_job(session, "Backend A", company="A")
    b = add_job(session, "Backend B", company="B")
    c = add_job(session, "Backend C", company="C")
    tracker.set_status(session, a.id, "interviewing", now=NOW)
    tracker.set_status(session, b.id, "saved", notes="Ask about <timezone>", now=NOW)
    tracker.set_status(session, c.id, "not_interested", now=NOW)
    session.commit()
    run(session, fake_tg, message_update("/applied"))
    (text,) = texts(fake_tg)
    assert text.startswith("<b>2 tracked jobs</b>")
    assert text.index("⭐ Saved") < text.index("🗣 Interviewing")
    assert "Ask about &lt;timezone&gt;" in text and "Backend C" not in text


def test_applied_when_empty(session, fake_tg):
    run(session, fake_tg, message_update("/applied"))
    assert "Nothing tracked yet" in texts(fake_tg)[0]


def test_stats(session, fake_tg):
    job = add_job(session, score=80)
    add_job(session, "Ops", score=90, eligible=False)
    tracker.set_status(session, job.id, "applied", now=NOW)
    session.commit()
    run(session, fake_tg, message_update("/stats"))
    (text,) = texts(fake_tg)
    assert "Jobs stored: 2" in text and "not open to you 1" in text
    assert "✅ Applied 1" in text and "Digest: on" in text


def test_pause_and_resume(session, fake_tg):
    run(session, fake_tg, message_update("/pause"))
    assert bot.is_paused(session)
    run(session, fake_tg, message_update("/stats"))
    assert "Digest: paused" in texts(fake_tg)[-1]
    run(session, fake_tg, message_update("/resume"))
    assert not bot.is_paused(session)
    assert texts(fake_tg)[0].startswith("Daily digest paused") and texts(fake_tg)[-1] == "Daily digest resumed."


def test_status_command(session, fake_tg):
    job = add_job(session)
    run(session, fake_tg, message_update(f"/status {job.id} interviewing"))
    assert tracker.get_application(session, job.id).status == "interviewing"
    run(session, fake_tg, message_update("/status 999999 offer"))
    run(session, fake_tg, message_update("/status nope"))
    assert texts(fake_tg)[1:] == ["No job #999999.", texts(fake_tg)[2]]
    assert texts(fake_tg)[2].startswith("Usage: /status &lt;job id&gt;")


def test_buttons_update_the_tracker_and_redraw_the_card(session, fake_tg):
    job = add_job(session, "Backend Engineer", company="Acme")
    assert run(session, fake_tg, callback_update(f"s:{job.id}:applied")) == "callback:applied"
    app = tracker.get_application(session, job.id)
    assert app.status == "applied" and app.applied_at is not None
    (answer,) = fake_tg.sent("answerCallbackQuery")
    assert answer == {"callback_query_id": "cbq-1", "text": "Marked as applied"}
    (edit,) = fake_tg.sent("editMessageText")
    assert edit["chat_id"] == int(OWNER) and edit["message_id"] == 77
    assert "<i>Status: ✅ Applied · 19 Oct 09:00</i>" in edit["text"]
    assert edit["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ Applied ✓"

    run(session, fake_tg, callback_update(f"s:{job.id}:not_interested"))
    assert tracker.get_application(session, job.id).status == "not_interested"
    run(session, fake_tg, callback_update(f"s:{job.id}:new"))
    assert tracker.get_application(session, job.id).status == "new"
    assert "Status:" not in fake_tg.sent("editMessageText")[-1]["text"]


def test_bad_or_stale_buttons(session, fake_tg):
    assert run(session, fake_tg, callback_update("s:1:hired")) == "callback:invalid"
    assert run(session, fake_tg, callback_update("garbage")) == "callback:invalid"
    assert run(session, fake_tg, callback_update("s:424242:saved")) == "callback:unknown-job"
    assert [b["text"] for b in fake_tg.sent("answerCallbackQuery")] == [
        "Unknown button",
        "Unknown button",
        "This job is no longer stored",
    ]
    assert fake_tg.sent("editMessageText") == []
