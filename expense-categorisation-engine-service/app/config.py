from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "expense-categorisation-engine-service"
    database_path: str = "data/categorisation.db"
    llm_provider: str = "none"
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = 10.0
    llm_max_retries: int = 3
    llm_retry_base_delay_seconds: float = 1.0

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
