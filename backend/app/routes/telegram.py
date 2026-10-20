"""Telegram webhook: POST /api/telegram/webhook.

Telegram sends the secret given to setWebhook in X-Telegram-Bot-Api-Secret-Token. That header,
compared in constant time, is the only way in (the path is in main.PUBLIC_PATHS:
no login). Errors are logged and answered with 200, because
Telegram would otherwise redeliver the same update again and again.
"""

import hmac
import logging

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.db_sync import get_sync_db
from app.telegram import bot
from app.telegram.client import TelegramClient
from app.telegram.config import telegram_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/telegram", tags=["telegram"])

WEBHOOK_PATH = "/api/telegram/webhook"
SECRET_HEADER = "x-telegram-bot-api-secret-token"


def webhook_secret_ok(supplied: str | None) -> bool:
    expected = telegram_settings.webhook_secret
    return bool(expected) and hmac.compare_digest((supplied or "").encode(), expected.encode())


def make_client() -> TelegramClient:
    return TelegramClient(telegram_settings.bot_token, api_base=telegram_settings.api_base)


def get_client():
    client = make_client()
    try:
        yield client
    finally:
        client.close()


def _check_secret(x_telegram_bot_api_secret_token: str | None = Header(None)) -> None:
    if not telegram_settings.enabled or not telegram_settings.webhook_secret:
        raise HTTPException(404, "Telegram is not configured")
    if not webhook_secret_ok(x_telegram_bot_api_secret_token):
        raise HTTPException(401, "Bad secret token")


@router.post("/webhook", dependencies=[Depends(_check_secret)])
def webhook(
    update: dict = Body(..., description="A Telegram Update"),
    session: Session = Depends(get_sync_db),
    client: TelegramClient = Depends(get_client),
) -> dict:
    try:
        result = bot.handle_update(session, client, telegram_settings, update)
        session.commit()
    except Exception as exc:
        session.rollback()
        # Only the exception type: database errors carry their parameters (chat ids, names, codes).
        logger.warning("Telegram update %s failed: %s", update.get("update_id"), type(exc).__name__)
        result = "error"
    return {"ok": True, "result": result}
