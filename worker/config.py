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

    # AI features (optional): extraction of job facts and cover letters. Leave the key empty for rules only.
    # For a local model: LLM_BASE_URL=http://localhost:11434/v1 OPENAI_MODEL=llama3.1 (not billed).
    openai_api_key: str = ""
    openai_model: str = "gpt-6-luna"
    llm_base_url: str = ""
    # Used when OPENAI_MODEL isn't available on the key (404/403). Empty: no fallback.
    llm_fallback_model: str = "gpt-4.1-mini"
    # Hard local budget (worker/budget.py), below the $1 limit on the OpenAI account.
    llm_monthly_budget_usd: float = 0.80
    llm_max_extractions_per_day: int = 30  # the day's top-N jobs by rules rank get LLM extraction
    llm_max_cover_letters_per_day: int = 5  # per user
    # "" = the model's default from worker/llm.py MODEL_CAPS ("low" for gpt-6-luna)
    llm_reasoning_effort: str = ""
    # Extra or corrected prices: '{"model": [input USD per 1M, output USD per 1M]}'
    llm_prices_json: str = ""
    llm_timeout_seconds: float = 30.0
    llm_concurrency: int = 4  # parallel extraction calls in the daily run
    llm_enrich_max_seconds: float = 75.0  # stop starting new extraction calls after this long
    # Ledger for `discover` without a database
    llm_usage_file: str = ".firstfetch/llm_usage.json"

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
