from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    worker_mode: Literal["skeleton"] = "skeleton"
    worker_api_secret: SecretStr
    supabase_url: str
    supabase_service_role_key: SecretStr
    max_video_duration_seconds: int = 90
    max_video_size_mb: int = 60

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
