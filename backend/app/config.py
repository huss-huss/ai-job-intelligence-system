"""Application configuration via environment variables."""

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

    # API
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    # Scraping
    scrape_user_agent: str = "AI-Job-Intelligence/1.0 (compatible; +https://github.com/ai-job-intelligence)"

    # Alerts (optional)
    alert_webhook_url: str = ""
    alert_score_threshold: float = 0.7


settings = Settings()
