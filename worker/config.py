"""Worker config (mirrors backend app.config for standalone worker env)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    database_url: str = "postgresql://postgres:postgres@localhost:5432/jobs"
    redis_url: str = "redis://localhost:6379/0"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    alert_webhook_url: str = ""
    alert_score_threshold: float = 0.7
    scrape_user_agent: str = "AI-Job-Intelligence/1.0 (compatible; +https://github.com/ai-job-intelligence)"


settings = Settings()
