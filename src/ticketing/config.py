"""Process configuration (environment). Business settings live in the `setting` table (F11)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TICKETING_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres@localhost:55432/ticketing"
    secret_key: str = "change-me-in-production"  # signs CSRF tokens; set TICKETING_SECRET_KEY
    base_url: str = "http://localhost:8000"
    cookie_secure: bool = False
    worker_id: str = "worker-1"
    # First platform admin created by the seed (password must be changed at first sign-in).
    bootstrap_admin_email: str = "admin@example.com"
    bootstrap_admin_password: str = "ChangeMe!12345"


@lru_cache
def get_config() -> AppConfig:
    return AppConfig()
