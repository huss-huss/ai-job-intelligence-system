"""Bot API client: payloads, 429/5xx retries, throttling and token redaction (fake API, no network)."""

import logging

import httpx
import pytest
from app.telegram.client import MAX_MESSAGE_CHARS, TelegramClient, TelegramError

from tests.tracker_fixtures import TOKEN, FakeTelegram, tg_error


def test_send_message_uses_html_and_no_link_previews(fake_tg):
    client = fake_tg.client()
    result = client.send_message("42", "<b>hi</b>", reply_markup={"inline_keyboard": []})
    method, body = fake_tg.requests[0]
    assert method == "sendMessage" and result["message_id"] == 1001
    assert body == {
        "chat_id": "42",
        "text": "<b>hi</b>",
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
        "reply_markup": {"inline_keyboard": []},
    }


def test_429_is_retried_after_retry_after(fake_tg):
    fake_tg.script["sendMessage"] = [tg_error(429, "Too Many Requests: retry after 3", retry_after=3)]
    sleeps: list[float] = []
    client = fake_tg.client(sleeps=sleeps)
    client.send_message("42", "x")
    assert fake_tg.methods() == ["sendMessage", "sendMessage"]
    assert 3 in sleeps


def test_429_with_long_flood_wait_gives_up(fake_tg):
    fake_tg.script["sendMessage"] = [tg_error(429, "Too Many Requests", retry_after=600)]
    with pytest.raises(TelegramError) as exc:
        fake_tg.client(max_retry_after=30).send_message("42", "x")
    assert exc.value.error_code == 429 and len(fake_tg.requests) == 1


def test_429_retries_are_bounded(fake_tg):
    fake_tg.script["sendMessage"] = [tg_error(429, "Too Many Requests", retry_after=1) for _ in range(10)]
    with pytest.raises(TelegramError):
        fake_tg.client(max_retries=2).send_message("42", "x")
    assert len(fake_tg.requests) == 3


def test_server_errors_are_retried_then_succeed(fake_tg):
    fake_tg.script["sendMessage"] = [tg_error(502, "Bad Gateway")]
    fake_tg.client().send_message("42", "x")
    assert len(fake_tg.requests) == 2


def test_bad_request_is_not_retried(fake_tg):
    fake_tg.script["sendMessage"] = [tg_error(400, "Bad Request: can't parse entities")]
    with pytest.raises(TelegramError) as exc:
        fake_tg.client().send_message("42", "<b>")
    assert exc.value.error_code == 400 and len(fake_tg.requests) == 1


def test_network_errors_never_leak_the_token():
    def handler(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    client = TelegramClient(TOKEN, transport=httpx.MockTransport(handler), min_interval=0, sleep=lambda s: None)
    with pytest.raises(TelegramError) as exc:
        client.send_message("42", "x")
    assert TOKEN not in str(exc.value) and "<token>" in str(exc.value)


def test_httpx_request_logs_are_redacted(caplog, monkeypatch):
    monkeypatch.setattr(logging.getLogger("httpx"), "disabled", False)  # alembic's fileConfig disables it
    fake = FakeTelegram()
    with caplog.at_level(logging.INFO, logger="httpx"):
        fake.client().send_message("42", "x")
    assert caplog.records, "httpx should log the request"
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


def test_messages_are_spaced_under_30_per_second():
    now = {"t": 0.0}
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        now["t"] += seconds

    fake = FakeTelegram()
    client = TelegramClient(
        TOKEN, transport=httpx.MockTransport(fake.handler), sleep=sleep, clock=lambda: now["t"], min_interval=1 / 25
    )
    for _ in range(30):
        client.send_message("42", "x")
    # 30 messages need at least 29 gaps of 1/25 s: more than one second in total.
    assert now["t"] >= 29 / 25 - 1e-9
    assert len(sleeps) == 29


def test_overlong_message_is_refused_before_sending(fake_tg):
    with pytest.raises(ValueError):
        fake_tg.client().send_message("42", "x" * (MAX_MESSAGE_CHARS + 1))
    assert fake_tg.requests == []


def test_editing_with_unchanged_content_is_not_an_error(fake_tg):
    fake_tg.script["editMessageText"] = [tg_error(400, "Bad Request: message is not modified")]
    assert fake_tg.client().edit_message_text("42", 7, "same") is None


def test_set_webhook_sends_secret_and_drops_pending_updates(fake_tg):
    fake_tg.client().set_webhook("https://api.example.com/api/telegram/webhook", "sek")
    method, body = fake_tg.requests[0]
    assert method == "setWebhook"
    assert body["secret_token"] == "sek" and body["drop_pending_updates"] is True
    assert body["allowed_updates"] == ["message", "callback_query"]


def test_missing_token_is_refused():
    with pytest.raises(ValueError):
        TelegramClient("")
