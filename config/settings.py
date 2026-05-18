from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    database_url: str = "sqlite:///data/openclaw.db"
    redis_url: str = "redis://localhost:6379/0"
    log_level: str = "INFO"

    request_timeout: int = 30
    max_concurrent_fetches: int = 10
    user_agent: str = "openclaw/1.0"

    # retry knobs
    retry_attempts: int = 3
    retry_backoff_base: float = 2.0   # seconds; actual delay = base ** attempt
    retry_status_codes: list[int] = [429, 500, 502, 503, 504]


settings = Settings()
