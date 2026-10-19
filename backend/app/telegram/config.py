"""Telegram settings (TELEGRAM_* environment variables)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class TelegramSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TELEGRAM_", env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )

    # From @BotFather. Without it the bot and the digest are off.
    bot_token: str = ""
    # Sent back by Telegram in X-Telegram-Bot-Api-Secret-Token on every webhook call (1-256 of A-Z a-z 0-9 _ -).
    webhook_secret: str = ""
    # The owner's chat. Until it's set the bot replies to /start with the chat id and nothing else.
    # Other chats are answered only after joining with an owner's invite code (/invite).
    chat_id: str = ""
    # Public dashboard URL, for the personal links /dashboard sends (e.g. https://firstfetch.example.app).
    dashboard_url: str = ""

    # Daily digest, sent at the end of the daily cron run.
    digest_size: int = 5  # at most this many jobs per day
    digest_min_score: float = 60  # 0-100, after feedback adjustments
    digest_max_age_days: int = 7  # only jobs first stored within this many days
    digest_send_empty: bool = True  # send "no new strong matches" when nothing qualifies
    digest_max_seconds: float = 90  # stop sending (all users) after this long; the cron has 300 s in total

    # Times in messages are shown in this zone.
    timezone: str = "Africa/Addis_Ababa"
    api_base: str = "https://api.telegram.org"

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token)

    @property
    def can_send(self) -> bool:
        return bool(self.bot_token and self.chat_id)


telegram_settings = TelegramSettings()
