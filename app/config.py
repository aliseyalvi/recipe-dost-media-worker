from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal, Optional

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

WorkerMode = Literal[
    "skeleton",
    "youtube_direct",
    "social_video",
    "apify_instagram",
    "rapidapi_social",
    "ytdlp_dev",
]
MediaPipelineMode = Literal["youtube_direct", "social_stream"]


class Settings(BaseSettings):
    app_env: str = "development"
    worker_mode: WorkerMode = "skeleton"
    media_pipeline_mode: MediaPipelineMode = "youtube_direct"
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
    max_video_size_mb: int = 30
    enable_social_video: bool = False
    enable_yt_dlp: bool = False
    cookie_dir: str = "/app/cookies"
    cookie_file_path: str = "/app/cookies.txt"
    enable_cookie_file: bool = True
    enable_generic_cookie_fallback: bool = False
    tiktok_cookie_file: str = "/app/cookies/tiktok.txt"
    instagram_cookie_file: str = "/app/cookies/instagram.txt"
    facebook_cookie_file: str = "/app/cookies/facebook.txt"
    ytdlp_extract_timeout_seconds: int = 45
    ytdlp_socket_timeout_seconds: int = 15
    ytdlp_retries: int = 1
    ytdlp_use_subprocess: bool = True
    ytdlp_force_generic: bool = False
    ytdlp_enable_impersonation: bool = False
    enable_ytdlp_stdout_fallback: bool = False
    max_memory_buffer_mb: int = 35
    max_video_height: int = 720
    thumbnail_bucket: str = "recipe-thumbnails"
    use_source_thumbnail_first: bool = True
    store_raw_media: bool = False
    delete_raw_media_after_processing: bool = True
    enable_tikwm_resolver: bool = True
    tikwm_api_url: str = "https://www.tikwm.com/api/"
    tikwm_timeout_seconds: int = 15
    tikwm_hd: bool = True
    tikwm_enabled_platforms: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["tiktok"]
    )
    tikwm_max_video_size_mb: int = 30
    tikwm_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    tikwm_probe_bytes: int = 262144
    enable_cobalt_resolver: bool = False
    cobalt_api_url: Optional[str] = None
    cobalt_timeout_seconds: int = 45
    cobalt_always_proxy: bool = True
    cobalt_video_quality: str = "720"
    cobalt_enabled_platforms: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["instagram", "facebook"]
    )
    cobalt_api_key: Optional[SecretStr] = None

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

    @property
    def tikwm_configured(self) -> bool:
        return self.enable_tikwm_resolver and bool(self.tikwm_api_url.strip())

    @property
    def cobalt_configured(self) -> bool:
        return self.enable_cobalt_resolver and bool((self.cobalt_api_url or "").strip())

    @field_validator("gemini_video_fallback_models", mode="before")
    @classmethod
    def parse_fallback_models(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("tikwm_enabled_platforms", "cobalt_enabled_platforms", mode="before")
    @classmethod
    def parse_platform_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [
                item.strip().lower()
                for item in value.split(",")
                if item.strip()
            ]
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
            "social_video",
            "apify_instagram",
            "rapidapi_social",
            "ytdlp_dev",
        }
        normalized = str(value).strip().lower()
        return normalized if normalized in known_modes else "skeleton"

    @field_validator("media_pipeline_mode", mode="before")
    @classmethod
    def fallback_unknown_media_pipeline_mode(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        return normalized if normalized in {"youtube_direct", "social_stream"} else "youtube_direct"


@lru_cache
def get_settings() -> Settings:
    return Settings()
