"""`python -m worker.cli telegram-setup | telegram-test | telegram-digest`."""

from __future__ import annotations

import html
import os
import re
import sys
from collections.abc import Callable

from app.telegram.bot import COMMANDS
from app.telegram.client import TelegramClient, TelegramError
from app.telegram.config import TelegramSettings, telegram_settings

WEBHOOK_PATH = "/api/telegram/webhook"
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")


def _client(settings: TelegramSettings) -> TelegramClient:
    return TelegramClient(settings.bot_token, api_base=settings.api_base)


def webhook_url(base: str) -> str:
    base = base.strip().rstrip("/")
    return base if base.endswith(WEBHOOK_PATH) else base + WEBHOOK_PATH


def setup(
    url: str | None,
    *,
    drop_pending_updates: bool = True,
    settings: TelegramSettings | None = None,
    client_factory: Callable[[TelegramSettings], TelegramClient] = _client,
) -> int:
    settings = settings or telegram_settings
    url = url or os.environ.get("TELEGRAM_API_URL", "")
    problems = []
    if not settings.bot_token:
        problems.append("TELEGRAM_BOT_TOKEN is not set")
    if not SECRET_RE.fullmatch(settings.webhook_secret or ""):
        problems.append("TELEGRAM_WEBHOOK_SECRET must be 1-256 characters of A-Z a-z 0-9 _ -")
    if not url.startswith("https://"):
        problems.append("--url must be your public https API URL (Telegram only calls https webhooks)")
    if problems:
        for p in problems:
            print(p, file=sys.stderr)
        return 2
    target = webhook_url(url)
    try:
        with client_factory(settings) as client:
            me = client.get_me()
            client.set_webhook(target, settings.webhook_secret, drop_pending_updates=drop_pending_updates)
            client.set_my_commands([(name, desc[:256]) for name, desc in COMMANDS])
    except TelegramError as exc:
        print(f"Telegram refused: {exc}", file=sys.stderr)
        return 1
    print(f"Webhook set for @{me.get('username', '?')}: {target}")
    if settings.chat_id:
        print("TELEGRAM_CHAT_ID is set; try `python -m worker.cli telegram-test`.")
    else:
        print("Next: send /start to the bot. It replies with your chat id; set it as TELEGRAM_CHAT_ID and redeploy.")
    return 0


def send_test(
    *,
    settings: TelegramSettings | None = None,
    client_factory: Callable[[TelegramSettings], TelegramClient] = _client,
) -> int:
    settings = settings or telegram_settings
    if not settings.can_send:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set", file=sys.stderr)
        return 2
    try:
        with client_factory(settings) as client:
            client.send_message(settings.chat_id, "✅ <b>FirstFetch</b> test message: Telegram alerts work.")
    except TelegramError as exc:
        print(f"Telegram refused: {exc}", file=sys.stderr)
        return 1
    print("Test message sent.")
    return 0


def plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def digest(*, dry_run: bool = False, settings: TelegramSettings | None = None) -> int:
    settings = settings or telegram_settings
    from app import tracker
    from app.db_sync import get_sync_session
    from app.telegram.digest import preview, run_digest

    if dry_run:
        with get_sync_session() as session:
            cards = preview(session, settings, tracker.utcnow())
            session.rollback()
        print(f"{len(cards)} job(s) would be sent (min score {settings.digest_min_score:.0f}):\n")
        for card in cards:
            print(plain(card), end="\n\n")
        return 0
    result = run_digest(settings=settings)
    print(result)
    return 1 if result.get("error") else 0
