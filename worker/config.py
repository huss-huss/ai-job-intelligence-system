"""Worker config (mirrors backend app.config for standalone worker env)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    database_url: str = "postgresql://postgres:postgres@localhost:5432/jobs"
    redis_url: str = "redis://localhost:6379/0"
    queue_mode: str = "redis"  # "inline": analyze right after scraping, no RQ worker

    # Candidate profile used for scoring. PROFILE_YAML (the file's contents) wins over
    # PROFILE_PATH, which is handy on hosts without a writable filesystem.
    profile_yaml: str = ""

    profile_path: str = "profile.yaml"

    # LLM extraction (optional). Leave the key empty to use rules only.
    # For a local model: LLM_BASE_URL=http://localhost:11434/v1 OPENAI_MODEL=llama3.1
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    llm_base_url: str = ""

    # Notion sync (optional)
    notion_token: str = ""
    notion_database_id: str = ""

    # Currency rates for salary scoring: JSON of USD per unit, merged over worker/salary.py's table
    fx_rates_json: str = ""

    # Full descriptions for sources whose API returns excerpts (worker/sources/fulltext.py).
    # Postings from these sources with a description under MIN_CHARS get their page fetched, at most
    # MAX_PAGES pages and MAX_SECONDS per scrape run. MIN_CHARS=0 turns it off.
    full_description_sources: str = "remoteok,himalayas"
    full_description_min_chars: int = 1500
    full_description_max_pages: int = 10
    full_description_max_seconds: float = 20.0

    # Webhook alert (optional), fired for jobs at or above the profile's notify_threshold
    alert_webhook_url: str = ""


settings = Settings()
