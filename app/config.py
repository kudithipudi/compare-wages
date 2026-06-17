import secrets
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: str = ""
    openrouter_model: str = "anthropic/claude-3.5-haiku"
    extraction_model: str = ""
    classification_model: str = ""
    narrative_model: str = ""
    use_mock_llm: bool = True
    database_url: str = "sqlite:///./data/wages.db"
    distance_cutoff_miles: float = 25.0
    root_path: str = ""  # e.g. "/compare-wages" when proxied behind a sub-path
    admin_username: str = ""
    admin_password: str = ""
    # Signing key for the admin session cookie. Set SESSION_SECRET in .env for prod so
    # sessions survive restarts; otherwise a fresh random key is generated each boot.
    session_secret: str = secrets.token_urlsafe(32)
    session_max_age_seconds: int = 12 * 3600


@lru_cache
def get_settings() -> Settings:
    return Settings()
