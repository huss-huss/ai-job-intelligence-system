"""Daily digest: selection, dedupe, limits, empty notice, pause, feedback and failure isolation."""

from contextlib import contextmanager
from datetime import timedelta

import httpx
import main
from app import tracker
from app.config import settings as app_settings
from app.database import get_db
from app.models import Run
from app.telegram import bot, digest
from app.telegram.config import telegram_settings
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.tracker_fixtures import NOW, add_job, settings, tg_error


def send(session, fake_tg, **overrides):
    return digest.send_digest(session, fake_tg.client(), settings(**overrides), NOW)


def card_titles(fake_tg):
    return [b["text"].split("\n")[0] for b in fake_tg.sent()[1:]]


def test_digest_sends_header_and_top_n_cards_above_threshold(session, fake_tg):
    for i in range(8):
        add_job(session, f"Backend Engineer {i}", company=f"Co{i}", score=55 + i * 5)  # 55..90
    stats = send(session, fake_tg, digest_size=3)
    assert stats == {"candidates": 3, "sent": 3, "failed": 0, "errors": []}
    header = fake_tg.sent()[0]["text"]
    assert header.startswith("<b>FirstFetch: 3 new matches</b> · score ≥ 60")
    assert card_titles(fake_tg) == [
        "<b>Backend Engineer 7</b>",
        "<b>Backend Engineer 6</b>",
        "<b>Backend Engineer 5</b>",
    ]
    card = fake_tg.sent()[1]
    buttons = [b["text"] for row in card["reply_markup"]["inline_keyboard"] for b in row]
    assert buttons == ["✅ Applied", "⭐ Save", "👎 Not for me", "🔗 Open"]


def test_digest_never_sends_the_same_job_twice(session, fake_tg):
    for i in range(3):
        add_job(session, f"Backend Engineer {i}", company=f"Co{i}", score=80)
    assert send(session, fake_tg)["sent"] == 3
    fake_tg.requests.clear()
    second = send(session, fake_tg)
    assert second["sent"] == 0 and second["empty_notice"] is True
    assert [b["text"] for b in fake_tg.sent()] == ["No new strong matches today (score ≥ 60)."]
    add_job(session, "Platform Engineer", company="New", score=75)
    fake_tg.requests.clear()
    assert send(session, fake_tg)["sent"] == 1 and card_titles(fake_tg) == ["<b>Platform Engineer</b>"]


def test_digest_skips_ineligible_old_acted_on_and_weak_jobs(session, fake_tg):
    unknown = add_job(session, "Python Engineer", company="U", score=70, eligible=None)
    add_job(session, "Ineligible", company="I", score=95, eligible=False)
    add_job(session, "Old", company="O", score=95, created_at=NOW - timedelta(days=8))
    saved = add_job(session, "Saved already", company="S", score=95)
    tracker.set_status(session, saved.id, "saved", now=NOW)
    session.commit()
    add_job(session, "Weak", company="W", score=59)
    send(session, fake_tg)
    assert card_titles(fake_tg) == ["<b>Python Engineer</b>"]
    assert "Eligibility unknown" in fake_tg.sent()[1]["text"]
    assert tracker.get_application(session, unknown.id).notified_at is not None


def test_empty_notice_can_be_turned_off(session, fake_tg):
    assert send(session, fake_tg, digest_send_empty=False) == {"candidates": 0, "sent": 0, "failed": 0, "errors": []}
    assert fake_tg.requests == []


def test_paused_digest_sends_nothing(session, fake_tg):
    add_job(session, score=90)
    bot.set_paused(session, True)
    assert send(session, fake_tg)["skipped"] == "paused"
    assert fake_tg.requests == []


def test_feedback_pushes_similar_jobs_below_the_threshold(session, fake_tg):
    disliked = add_job(session, "Senior Backend Engineer", company="Acme", score=50)
    tracker.set_status(session, disliked.id, "not_interested", now=NOW)
    tracker.mark_notified(session, disliked.id, NOW)
    liked = add_job(session, "ML Engineer", company="Zeta", score=50, skills=["pytorch", "llm"])
    tracker.set_status(session, liked.id, "saved", now=NOW)
    session.commit()
    add_job(session, "Backend Engineer", company="Acme", score=75)  # 75 - 20 = 55: dropped
    add_job(session, "ML Engineer II", company="Eta", score=58, skills=["pytorch", "llm"])  # 58 + 5 = 63: sent
    send(session, fake_tg)
    assert card_titles(fake_tg) == ["<b>ML Engineer II</b>"]
    assert "Similar to jobs you saved or applied to (llm, ml, pytorch)" in fake_tg.sent()[1]["text"]
    assert "Score <b>63</b> (58 + 5 feedback)" in fake_tg.sent()[1]["text"]


def test_rank_is_used_when_present(session, fake_tg):
    add_job(session, "Ranked high", company="R", score=40, extra={"rank": 0.9, "hire_probability": 0.25})
    add_job(session, "Ranked low", company="L", score=90, extra={"rank": 0.3})
    send(session, fake_tg)
    assert card_titles(fake_tg) == ["<b>Ranked high</b>"]
    assert "Hire probability 25%" in fake_tg.sent()[1]["text"]


OK = {"ok": True, "result": {"message_id": 1}}


def test_one_failed_card_is_counted_and_retried_next_time(session, fake_tg):
    a = add_job(session, "Backend A", company="A", score=90)
    b = add_job(session, "Backend B", company="B", score=80)
    # header goes through, the first card is rejected, the rest use the default (success)
    fake_tg.script["sendMessage"] = [httpx.Response(200, json=OK), tg_error(400, "Bad Request: can't parse entities")]
    stats = send(session, fake_tg)
    assert stats["sent"] == 1 and stats["failed"] == 1 and stats["errors"][0]["job_id"] == a.id
    assert tracker.get_application(session, a.id) is None
    assert tracker.get_application(session, b.id).notified_at is not None
    fake_tg.requests.clear()
    assert send(session, fake_tg)["sent"] == 1 and card_titles(fake_tg) == ["<b>Backend A</b>"]


def test_digest_stops_after_repeated_failures(session, fake_tg):
    for i in range(5):
        add_job(session, f"Backend {i}", company=f"C{i}", score=90)
    fake_tg.script["sendMessage"] = [httpx.Response(200, json=OK)] + [
        tg_error(400, "Bad Request: chat not found") for _ in range(5)
    ]
    stats = send(session, fake_tg)
    assert stats["failed"] == digest.MAX_CONSECUTIVE_FAILURES and stats["stopped_early"] is True
    assert len(fake_tg.sent()) == 1 + digest.MAX_CONSECUTIVE_FAILURES


def _factory(engine):
    @contextmanager
    def factory():
        with Session(engine, expire_on_commit=False) as s:
            yield s
            s.commit()

    return factory


def test_run_digest_records_a_run(engine, session, fake_tg):
    add_job(session, score=90)
    result = digest.run_digest(
        settings=settings(), session_factory=_factory(engine), client_factory=lambda s: fake_tg.client(), now=NOW
    )
    assert result["sent"] == 1 and result["api_calls"] == 2
    run = session.get(Run, result["run_id"])
    session.refresh(run)
    assert run.type == "telegram" and run.status == "completed" and run.stats["sent"] == 1


def test_run_digest_never_raises_when_telegram_is_down(engine, session, fake_tg):
    job = add_job(session, score=90)
    fake_tg.fail_all = True
    result = digest.run_digest(
        settings=settings(), session_factory=_factory(engine), client_factory=lambda s: fake_tg.client(), now=NOW
    )
    assert "TelegramError" in result["error"] and "network down" in result["error"]
    run = session.get(Run, result["run_id"])
    session.refresh(run)
    assert run.status == "failed" and "error" in run.stats
    assert tracker.get_application(session, job.id) is None  # not marked: it goes out next time


def test_run_digest_survives_a_broken_database():
    @contextmanager
    def broken():
        raise RuntimeError("database unreachable")
        yield  # pragma: no cover

    result = digest.run_digest(settings=settings(), session_factory=broken, client_factory=lambda s: None)
    assert result == {"error": "RuntimeError: database unreachable"}


def test_run_digest_is_off_without_token_or_chat():
    assert "skipped" in digest.run_digest(settings=settings(chat_id=""))
    assert "skipped" in digest.run_digest(settings=settings(bot_token=""))


# ----------------------------------------------------------------- cron integration


class _NoSources:
    async def execute(self, *_):
        class R:
            def scalars(self):
                return self

            def all(self):
                return []

        return R()

    async def commit(self):
        pass


def _cron(monkeypatch):
    async def fake_db():
        yield _NoSources()

    monkeypatch.setattr(app_settings, "cron_secret", "cron")
    main.app.dependency_overrides[get_db] = fake_db
    return TestClient(main.app).get("/api/cron/discover", headers={"Authorization": "Bearer cron"})


def test_cron_sends_the_digest_and_survives_telegram_failures(monkeypatch, engine, fake_tg):
    for key, value in settings().model_dump().items():
        monkeypatch.setattr(telegram_settings, key, value)
    fake_tg.fail_all = True
    import app.db_sync

    monkeypatch.setattr(app.db_sync, "get_sync_session", _factory(engine))
    monkeypatch.setattr(digest, "TelegramClient", lambda token, api_base: fake_tg.client())
    with Session(engine) as s:
        add_job(s, score=90, created_at=tracker.utcnow())
    try:
        resp = _cron(monkeypatch)
    finally:
        main.app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["sources"] == 0 and "network down" in body["telegram"]["error"]


def test_cron_skips_telegram_when_not_configured(monkeypatch):
    monkeypatch.setattr(telegram_settings, "bot_token", "")
    try:
        resp = _cron(monkeypatch)
    finally:
        main.app.dependency_overrides.clear()
    assert resp.json() == {"sources": 0, "results": []}
