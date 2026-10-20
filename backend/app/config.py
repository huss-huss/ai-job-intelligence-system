"""Application configuration via environment variables."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app_name: str = "AI Job Intelligence API"
    debug: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/jobs"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # "redis": hand jobs to the RQ worker. "inline": run them inside the request
    # (serverless deployments such as Vercel, where there is no long-running worker).
    queue_mode: Literal["redis", "inline"] = "redis"

    # Vercel Cron sends "Authorization: Bearer <CRON_SECRET>"
    cron_secret: str = ""

    # The owner's API key: "X-Admin-Token: <ADMIN_TOKEN>" reads everything and runs admin operations
    # (the owner's login session can do the same from the dashboard). The cron endpoint has its own
    # CRON_SECRET check.
    admin_token: str = ""

    # Interactive API docs (/api/docs, /api/redoc, /api/openapi.json). Unset: on locally, off on Vercel.
    api_docs: bool | None = None

    # Comma-separated extra origins allowed by CORS (dev origins are always allowed)
    cors_origins: str = ""

    # API
    openai_api_key: str = ""
    openai_model: str = "gpt-6-luna"
    llm_base_url: str = ""  # an OpenAI-compatible server instead of OpenAI (worker/config.py has the rest)

    @property
    def ai_enabled(self) -> bool:
        return bool(self.openai_api_key or self.llm_base_url)

    # Scraping
    scrape_user_agent: str = "AI-Job-Intelligence/1.0 (compatible; +https://github.com/ai-job-intelligence)"

    # Alerts (optional)
    alert_webhook_url: str = ""


settings = Settings()
