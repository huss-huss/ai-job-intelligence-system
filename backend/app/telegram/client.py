"""Minimal Telegram Bot API client over httpx.

Only the handful of methods FirstFetch needs. Every request goes through `call`, which
spaces requests out (Telegram allows about 30 messages/second per bot), retries 429 after
the `retry_after` Telegram asks for, retries 5xx and network errors a couple of times, and
keeps the bot token out of every error message and log line.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 4096
MAX_CALLBACK_DATA_BYTES = 64


class TelegramError(RuntimeError):
    def __init__(self, method: str, description: str, error_code: int | None = None) -> None:
        super().__init__(f"Telegram {method} failed ({error_code}): {description}")
        self.method, self.description, self.error_code = method, description, error_code


class _RedactToken(logging.Filter):
    """httpx logs every request URL at INFO, and the Bot API puts the token in the URL path."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        if self.token in str(record.msg) or any(self.token in str(a) for a in args):
            record.msg = str(record.msg).replace(self.token, "<token>")
            record.args = tuple(str(a).replace(self.token, "<token>") if self.token in str(a) else a for a in args)
        return True


class TelegramClient:
    def __init__(
        self,
        token: str,
        *,
        api_base: str = "https://api.telegram.org",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 15.0,
        min_interval: float = 1 / 25,  # stay under 30 messages/second
        max_retries: int = 3,
        max_retry_after: float = 30.0,  # a longer flood wait isn't worth holding the cron run for
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is not set")
        self._token = token
        self._base = f"{api_base.rstrip('/')}/bot{token}"
        self._file_base = f"{api_base.rstrip('/')}/file/bot{token}"
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.max_retry_after = max_retry_after
        self._sleep, self._clock = sleep, clock
        self._last = float("-inf")
        self.calls: list[str] = []  # method names, for run stats and tests
        httpx_logger = logging.getLogger("httpx")
        if not any(isinstance(f, _RedactToken) and f.token == token for f in httpx_logger.filters):
            httpx_logger.addFilter(_RedactToken(token))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> TelegramClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<token>")

    def _throttle(self) -> None:
        wait = self._last + self.min_interval - self._clock()
        if wait > 0:
            self._sleep(wait)
        self._last = self._clock()

    def call(self, method: str, **params: Any) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        attempt = 0
        while True:
            self._throttle()
            self.calls.append(method)
            try:
                resp = self._http.post(f"{self._base}/{method}", json=payload)
            except httpx.HTTPError as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    self._sleep(min(2**attempt, 8))
                    continue
                raise TelegramError(method, self._redact(f"{type(exc).__name__}: {exc}")) from None
            try:
                data = resp.json()
            except ValueError:
                data = {"ok": False, "error_code": resp.status_code, "description": resp.text[:200]}
            if data.get("ok"):
                return data.get("result")
            code = data.get("error_code") or resp.status_code
            description = self._redact(str(data.get("description") or resp.reason_phrase))
            if code == 429 and attempt < self.max_retries:
                retry_after = float((data.get("parameters") or {}).get("retry_after") or 1)
                if retry_after <= self.max_retry_after:
                    attempt += 1
                    logger.warning("Telegram %s rate-limited, retrying in %ss", method, retry_after)
                    self._sleep(retry_after)
                    continue
            if code >= 500 and attempt < self.max_retries:
                attempt += 1
                self._sleep(min(2**attempt, 8))
                continue
            raise TelegramError(method, description, code)

    # ------------------------------------------------------------------ methods

    def send_message(self, chat_id: str | int, text: str, reply_markup: dict | None = None) -> dict:
        if len(text) > MAX_MESSAGE_CHARS:
            raise ValueError(f"message is {len(text)} characters; Telegram allows {MAX_MESSAGE_CHARS}")
        return self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            link_preview_options={"is_disabled": True},
            reply_markup=reply_markup,
        )

    def edit_message_text(
        self, chat_id: str | int, message_id: int, text: str, reply_markup: dict | None = None
    ) -> Any:
        try:
            return self.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text[:MAX_MESSAGE_CHARS],
                parse_mode="HTML",
                link_preview_options={"is_disabled": True},
                reply_markup=reply_markup,
            )
        except TelegramError as exc:
            if "message is not modified" in exc.description:  # same state tapped twice
                return None
            raise

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> Any:
        return self.call("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

    def set_webhook(self, url: str, secret_token: str, *, drop_pending_updates: bool = True) -> Any:
        return self.call(
            "setWebhook",
            url=url,
            secret_token=secret_token,
            drop_pending_updates=drop_pending_updates,
            allowed_updates=["message", "callback_query"],
        )

    def set_my_commands(self, commands: list[tuple[str, str]]) -> Any:
        return self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])

    def get_me(self) -> dict:
        return self.call("getMe")

    def get_file(self, file_id: str) -> dict:
        """{"file_id", "file_size", "file_path"}; the path is valid for an hour."""
        return self.call("getFile", file_id=file_id)

    def download_file(self, file_path: str, *, max_bytes: int = 20_000_000) -> bytes:
        """The file's bytes from https://api.telegram.org/file/bot<token>/<file_path>."""
        self._throttle()
        self.calls.append("downloadFile")
        url = f"{self._file_base}/{file_path.lstrip('/')}"
        try:
            with self._http.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise TelegramError("downloadFile", f"HTTP {resp.status_code}", resp.status_code)
                data = b""
                for part in resp.iter_bytes():
                    data += part
                    if len(data) > max_bytes:
                        raise TelegramError("downloadFile", f"file is over {max_bytes} bytes")
                return data
        except httpx.HTTPError as exc:
            raise TelegramError("downloadFile", self._redact(f"{type(exc).__name__}: {exc}")) from None
