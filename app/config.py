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
GeminiSocialUploadMode = Literal["files_api", "inline"]
GeminiVideoInputMode = Literal["file_api"]
GeminiVideoAnalysisMode = Literal["two_step_brief_then_json"]
GeminiExtractionMode = Literal["single_pass_structured", "two_step_brief_then_json"]
SocialAnalysisMode = Literal["text_first", "video_first", "video_only"]
AIAnalysisMode = Literal["gemini", "mock_fixture", "gemini_video_poc"]
MockVideoProcessingMode = Literal["skip_video_download", "download_then_mock"]
YouTubeResolver = Literal["ytdlp"]
InstagramResolver = Literal["snapinsta"]
FacebookResolver = Literal["snapsave"]
WorkerExecutionMode = Literal["webhook", "queue"]
MediaExtractOutputMode = Literal["temp_file", "memory_buffer"]
YouTubeAIInputMode = Literal["gemini_url", "ytdlp"]
YouTubeMetadataMode = Literal["oembed"]


class Settings(BaseSettings):
    app_env: str = "development"
    runtime_platform: str = "cloud_run_compatible"
    worker_mode: WorkerMode = "skeleton"
    media_pipeline_mode: MediaPipelineMode = "youtube_direct"
    worker_api_secret: SecretStr
    supabase_url: str
    supabase_service_role_key: SecretStr
    gemini_api_key: Optional[SecretStr] = None
    gemini_model: str = "gemini-3.5-flash"
    gemini_extraction_mode: GeminiExtractionMode = "single_pass_structured"
    gemini_primary_model: str = "gemini-3.1-flash-lite"
    gemini_repair_model: Optional[str] = None
    gemini_fallback_model: str = "gemini-2.5-flash"
    gemini_thinking_level: str = "high"
    gemini_structured_output_enabled: bool = True
    local_brief_parser_enabled: bool = False
    gemini_video_model: Optional[str] = None
    gemini_text_model: Optional[str] = None
    gemini_video_fallback_models: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    gemini_max_retries: int = 1
    gemini_retry_base_seconds: float = 2.0
    gemini_max_calls_per_job: int = 2
    gemini_daily_dev_call_limit: int = 5
    gemini_video_observation_timeout_seconds: int = 120
    gemini_text_normalization_timeout_seconds: int = 60
    gemini_files_upload_timeout_seconds: int = 90
    gemini_files_processing_timeout_seconds: int = 120
    gemini_social_upload_mode: GeminiSocialUploadMode = "files_api"
    gemini_video_input_mode: GeminiVideoInputMode = "file_api"
    gemini_video_analysis_mode: GeminiVideoAnalysisMode = "two_step_brief_then_json"
    gemini_file_upload_timeout_seconds: int = 120
    gemini_file_poll_interval_seconds: int = 5
    gemini_file_max_wait_seconds: int = 120
    gemini_save_mode: str = "structured_json_with_brief_fallback"
    gemini_json_retry_enabled: bool = True
    gemini_json_fallback_enabled: bool = True
    gemini_video_brief_max_output_tokens: int = 768
    gemini_recipe_json_max_output_tokens: int = 1536
    gemini_recipe_brief_max_chars: int = 4000
    gemini_max_output_tokens: int = 12000
    gemini_video_brief_retry_enabled: bool = True
    gemini_video_brief_max_retries: int = 1
    gemini_recipe_repair_max_retries: int = 1
    recipe_quality_gate_enabled: bool = True
    recipe_repair_enabled: bool = True
    recipe_min_ingredients: int = 2
    recipe_min_steps: int = 3
    recipe_min_brief_length: int = 500
    recipe_bilingual_output_enabled: bool = True
    recipe_urdu_fallback_enabled: bool = True
    recipe_card_normalization_enabled: bool = True
    allow_private_metadata_failure: bool = True
    enable_queue_wake_endpoint: bool = True
    youtube_ai_input_mode: YouTubeAIInputMode = "gemini_url"
    youtube_metadata_mode: YouTubeMetadataMode = "oembed"
    max_job_runtime_seconds: int = 240
    ai_analysis_mode: AIAnalysisMode = "gemini"
    real_ai_fallback_to_mock: bool = False
    mock_recipe_fixture_path: str = "app/fixtures/mock_recipe_card.json"
    mock_analysis_delay_ms: int = 500
    mock_video_processing_mode: MockVideoProcessingMode = "skip_video_download"
    social_analysis_mode: SocialAnalysisMode = "text_first"
    enable_tiktok_text_fallback: bool = True
    enable_social_video_gemini: bool = False
    social_video_gemini_timeout_seconds: int = 90
    social_text_gemini_timeout_seconds: int = 45
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
    enable_ytdlp_cookies: bool = False
    enable_instagram_cookies: bool = False
    enable_youtube_cookies: bool = False
    enable_facebook_cookies: bool = False
    enable_tiktok_cookies: bool = False
    instagram_cookies_file: Optional[str] = None
    youtube_cookies_file: Optional[str] = None
    facebook_cookies_file: Optional[str] = None
    tiktok_cookies_file: Optional[str] = None
    instagram_cookies_b64: Optional[SecretStr] = None
    youtube_cookies_b64: Optional[SecretStr] = None
    facebook_cookies_b64: Optional[SecretStr] = None
    tiktok_cookies_b64: Optional[SecretStr] = None
    ytdlp_user_agent: Optional[str] = None
    enable_metadata_only_mock_fallback: bool = True
    enable_og_metadata_fallback: bool = True
    allow_mock_completion_without_video: bool = True
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
    enable_render_youtube_resolver: bool = True
    youtube_resolver: YouTubeResolver = "ytdlp"
    instagram_resolver: InstagramResolver = "snapinsta"
    facebook_resolver: FacebookResolver = "snapsave"
    worker_execution_mode: WorkerExecutionMode = "queue"
    queue_poll_interval_seconds: int = 5
    queue_empty_sleep_seconds: int = 5
    enable_unified_ytdlp_stream_extractor: bool = False
    media_extract_output_mode: MediaExtractOutputMode = "temp_file"
    snapinsta_api_url: str = "https://snapinsta.app"
    snapsave_api_url: str = "https://snapsave.app"
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
    enable_render_instagram_resolver: bool = False
    enable_render_facebook_resolver: bool = False
    cobalt_api_url: Optional[str] = None
    cobalt_timeout_seconds: int = 45
    cobalt_always_proxy: bool = True
    cobalt_video_quality: str = "720"
    cobalt_enabled_platforms: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["instagram", "facebook"]
    )
    cobalt_api_key: Optional[SecretStr] = None
    temp_media_dir: str = "/tmp/recipe-dost-media"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def effective_gemini_video_model(self) -> str:
        return self.gemini_video_model or self.gemini_primary_model

    @property
    def effective_gemini_text_model(self) -> str:
        return self.gemini_text_model or self.gemini_primary_model

    @property
    def effective_gemini_repair_model(self) -> str:
        return self.gemini_repair_model or self.gemini_primary_model

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

    @field_validator("gemini_social_upload_mode", mode="before")
    @classmethod
    def fallback_unknown_gemini_social_upload_mode(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        return normalized if normalized in {"files_api", "inline"} else "files_api"

    @field_validator("gemini_extraction_mode", mode="before")
    @classmethod
    def fallback_unknown_gemini_extraction_mode(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        return (
            normalized
            if normalized in {"single_pass_structured", "two_step_brief_then_json"}
            else "single_pass_structured"
        )

    @field_validator("gemini_video_input_mode", mode="before")
    @classmethod
    def fallback_unknown_gemini_video_input_mode(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        return normalized if normalized == "file_api" else "file_api"

    @field_validator("gemini_video_analysis_mode", mode="before")
    @classmethod
    def fallback_unknown_gemini_video_analysis_mode(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        return (
            normalized
            if normalized == "two_step_brief_then_json"
            else "two_step_brief_then_json"
        )

    @field_validator("youtube_ai_input_mode", mode="before")
    @classmethod
    def fallback_unknown_youtube_ai_input_mode(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        return normalized if normalized in {"gemini_url", "ytdlp"} else "gemini_url"

    @field_validator("youtube_metadata_mode", mode="before")
    @classmethod
    def fallback_unknown_youtube_metadata_mode(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        return normalized if normalized == "oembed" else "oembed"

    @field_validator("social_analysis_mode", mode="before")
    @classmethod
    def fallback_unknown_social_analysis_mode(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        return (
            normalized
            if normalized in {"text_first", "video_first", "video_only"}
            else "text_first"
        )

    @field_validator("ai_analysis_mode", mode="before")
    @classmethod
    def fallback_unknown_ai_analysis_mode(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        return (
            normalized
            if normalized in {"gemini", "mock_fixture", "gemini_video_poc"}
            else "gemini"
        )

    @field_validator("mock_video_processing_mode", mode="before")
    @classmethod
    def fallback_unknown_mock_video_processing_mode(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        return (
            normalized
            if normalized in {"skip_video_download", "download_then_mock"}
            else "skip_video_download"
        )

    @field_validator("youtube_resolver", mode="before")
    @classmethod
    def fallback_unknown_youtube_resolver(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        return normalized if normalized == "ytdlp" else "ytdlp"

    @field_validator("instagram_resolver", mode="before")
    @classmethod
    def fallback_unknown_instagram_resolver(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        return normalized if normalized == "snapinsta" else "snapinsta"

    @field_validator("facebook_resolver", mode="before")
    @classmethod
    def fallback_unknown_facebook_resolver(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        return normalized if normalized == "snapsave" else "snapsave"

    @field_validator("worker_execution_mode", mode="before")
    @classmethod
    def fallback_unknown_worker_execution_mode(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        return normalized if normalized in {"webhook", "queue"} else "queue"

    @field_validator("media_extract_output_mode", mode="before")
    @classmethod
    def fallback_unknown_media_extract_output_mode(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        return normalized if normalized in {"temp_file", "memory_buffer"} else "temp_file"


@lru_cache
def get_settings() -> Settings:
    return Settings()
