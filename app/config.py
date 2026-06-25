from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal, Optional

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

WorkerMode = Literal[
    "skeleton",
    "youtube_direct",
    "apify_instagram",
    "rapidapi_social",
    "ytdlp_dev",
]


class Settings(BaseSettings):
    app_env: str = "development"
    worker_mode: WorkerMode = "skeleton"
    worker_api_secret: SecretStr
    supabase_url: str
    supabase_service_role_key: SecretStr
    gemini_api_key: Optional[SecretStr] = None
    gemini_model: str = "gemini-3.5-flash"
    gemini_video_model: Optional[str] = None
    gemini_text_model: Optional[str] = None
    gemini_video_fallback_models: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    gemini_max_retries: int = 1
    gemini_retry_base_seconds: float = 2.0
    gemini_max_calls_per_job: int = 2
    gemini_daily_dev_call_limit: int = 5
    enable_gemini_repair_call: bool = False
    enable_gemini_fallbacks: bool = False
    enable_auto_force_retry: bool = False
    gemini_disabled_models: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    gemini_daily_dev_model_limits: Annotated[dict[str, int], NoDecode] = Field(
        default_factory=dict
    )
    gemini_dev_budget_reset_at: Optional[str] = None
    max_video_duration_seconds: int = 90
    max_video_size_mb: int = 60

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def effective_gemini_video_model(self) -> str:
        return self.gemini_video_model or self.gemini_model or "gemini-3.5-flash"

    @property
    def effective_gemini_text_model(self) -> str:
        return self.gemini_text_model or self.gemini_model or "gemini-2.5-flash"

    @property
    def gemini_configured(self) -> bool:
        return self.gemini_api_key is not None

    @field_validator("gemini_video_fallback_models", mode="before")
    @classmethod
    def parse_fallback_models(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("gemini_disabled_models", mode="before")
    @classmethod
    def parse_disabled_models(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("gemini_daily_dev_model_limits", mode="before")
    @classmethod
    def parse_model_limits(cls, value: object) -> object:
        if isinstance(value, str):
            limits: dict[str, int] = {}
            for item in value.split(","):
                model, separator, limit = item.strip().partition(":")
                if separator and model:
                    limits[model] = max(0, int(limit))
            return limits
        return value

    @field_validator("worker_mode", mode="before")
    @classmethod
    def fallback_unknown_worker_mode(cls, value: object) -> object:
        known_modes = {
            "skeleton",
            "youtube_direct",
            "apify_instagram",
            "rapidapi_social",
            "ytdlp_dev",
        }
        normalized = str(value).strip().lower()
        return normalized if normalized in known_modes else "skeleton"


@lru_cache
def get_settings() -> Settings:
    return Settings()
