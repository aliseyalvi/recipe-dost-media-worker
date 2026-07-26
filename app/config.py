from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal, Optional
from urllib.parse import urlparse

from pydantic import (
    AliasChoices,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.runtime_config import (
    selected_worker_environment,
    worker_config_report,
    worker_config_value,
    worker_settings_file_source,
)

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
MediaResolverMode = Literal["legacy", "external", "hybrid"]
ExternalMediaResolverAudience = Literal[
    "disabled",
    "test_users",
    "all_authorized_ai",
]

REGISTERED_EXTERNAL_MEDIA_ADAPTERS = {
    "rapidapi_instagram_reels13",
    "akng_tiktok",
}


def _without_blank_values(source: object):
    def load() -> dict[str, object]:
        values = source()
        return {
            key: value
            for key, value in values.items()
            if not (isinstance(value, str) and not value.strip())
        }

    return load


class Settings(BaseSettings):
    app_env: str = selected_worker_environment()[0]
    runtime_platform: str = "cloud_run_compatible"
    worker_mode: WorkerMode = "skeleton"
    media_pipeline_mode: MediaPipelineMode = "youtube_direct"
    worker_api_secret: SecretStr = Field(
        validation_alias=AliasChoices("WORKER_API_SECRET", "MEDIA_WORKER_SHARED_SECRET")
    )
    supabase_url: str
    supabase_service_role_key: SecretStr
    gemini_api_key: Optional[SecretStr] = None
    gemini_model: str = "gemini-3.5-flash"
    gemini_extraction_mode: GeminiExtractionMode = worker_config_value(
        "gemini_extraction_mode", "single_pass_structured"
    )
    gemini_primary_model: str = worker_config_value(
        "gemini_primary_model", "gemini-3.1-flash-lite"
    )
    gemini_repair_model: Optional[str] = None
    gemini_fallback_model: str = worker_config_value(
        "gemini_fallback_model", "gemini-2.5-flash"
    )
    gemini_thinking_level: str = worker_config_value("gemini_thinking_level", "high")
    gemini_structured_output_enabled: bool = worker_config_value(
        "gemini_structured_output_enabled", True
    )
    local_brief_parser_enabled: bool = worker_config_value(
        "local_brief_parser_enabled", False
    )
    gemini_video_model: Optional[str] = worker_config_value("gemini_video_model", None)
    gemini_text_model: Optional[str] = worker_config_value("gemini_text_model", None)
    gemini_video_fallback_models: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    gemini_max_retries: int = 1
    gemini_retry_base_seconds: float = 2.0
    gemini_max_calls_per_job: int = worker_config_value("gemini_max_calls_per_job", 3)
    gemini_daily_dev_call_limit: int = 5
    gemini_video_observation_timeout_seconds: int = 120
    gemini_text_normalization_timeout_seconds: int = 60
    gemini_files_upload_timeout_seconds: int = 90
    gemini_files_processing_timeout_seconds: int = 120
    gemini_social_upload_mode: GeminiSocialUploadMode = "files_api"
    gemini_video_input_mode: GeminiVideoInputMode = worker_config_value(
        "gemini_video_input_mode", "file_api"
    )
    gemini_video_analysis_mode: GeminiVideoAnalysisMode = worker_config_value(
        "gemini_video_analysis_mode", "two_step_brief_then_json"
    )
    gemini_file_upload_timeout_seconds: int = worker_config_value(
        "gemini_file_upload_timeout_seconds", 120
    )
    gemini_file_max_size_bytes: int = worker_config_value(
        "gemini_file_max_size_bytes", 2_000_000_000
    )
    gemini_file_poll_interval_seconds: int = worker_config_value(
        "gemini_file_poll_interval_seconds", 5
    )
    gemini_file_max_wait_seconds: int = worker_config_value(
        "gemini_file_max_wait_seconds", 120
    )
    gemini_save_mode: str = worker_config_value(
        "gemini_save_mode", "structured_json_with_brief_fallback"
    )
    gemini_json_retry_enabled: bool = worker_config_value(
        "gemini_json_retry_enabled", True
    )
    gemini_json_fallback_enabled: bool = worker_config_value(
        "gemini_json_fallback_enabled", True
    )
    gemini_video_brief_max_output_tokens: int = 768
    gemini_recipe_json_max_output_tokens: int = 1536
    gemini_recipe_brief_max_chars: int = 4000
    gemini_max_output_tokens: int = worker_config_value("gemini_max_output_tokens", 12000)
    gemini_video_brief_retry_enabled: bool = worker_config_value(
        "gemini_video_brief_retry_enabled", True
    )
    gemini_video_brief_max_retries: int = worker_config_value(
        "gemini_video_brief_max_retries", 1
    )
    gemini_recipe_repair_max_retries: int = worker_config_value(
        "gemini_recipe_repair_max_retries", 1
    )
    recipe_quality_gate_enabled: bool = worker_config_value(
        "recipe_quality_gate_enabled", True
    )
    recipe_repair_enabled: bool = worker_config_value("recipe_repair_enabled", True)
    recipe_min_ingredients: int = worker_config_value("recipe_min_ingredients", 2)
    recipe_min_steps: int = worker_config_value("recipe_min_steps", 3)
    recipe_min_brief_length: int = worker_config_value("recipe_min_brief_length", 500)
    recipe_bilingual_output_enabled: bool = worker_config_value(
        "recipe_bilingual_output_enabled", True
    )
    recipe_urdu_fallback_enabled: bool = worker_config_value(
        "recipe_urdu_fallback_enabled", True
    )
    recipe_card_normalization_enabled: bool = worker_config_value(
        "recipe_card_normalization_enabled", True
    )
    allow_private_metadata_failure: bool = worker_config_value(
        "allow_private_metadata_failure", True
    )
    allow_raw_url_logs: bool = worker_config_value("allow_raw_url_logs", False)
    enable_queue_wake_endpoint: bool = worker_config_value(
        "enable_queue_wake_endpoint", True
    )
    youtube_ai_input_mode: YouTubeAIInputMode = worker_config_value(
        "youtube_ai_input_mode", "gemini_url"
    )
    youtube_metadata_mode: YouTubeMetadataMode = worker_config_value(
        "youtube_metadata_mode", "oembed"
    )
    max_job_runtime_seconds: int = 240
    ai_analysis_mode: AIAnalysisMode = worker_config_value("ai_analysis_mode", "gemini")
    real_ai_fallback_to_mock: bool = worker_config_value(
        "real_ai_fallback_to_mock", False
    )
    mock_recipe_fixture_path: str = worker_config_value(
        "mock_recipe_fixture_path", "app/fixtures/mock_recipe_card.json"
    )
    mock_analysis_delay_ms: int = worker_config_value("mock_analysis_delay_ms", 500)
    mock_video_processing_mode: MockVideoProcessingMode = worker_config_value(
        "mock_video_processing_mode", "skip_video_download"
    )
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
    max_video_duration_seconds: int = Field(
        default=worker_config_value("max_video_duration_seconds", 90),
        validation_alias=AliasChoices("MAX_VIDEO_DURATION_SECONDS", "MAX_VIDEO_SECONDS"),
    )
    preferred_video_size_mb: int = worker_config_value(
        "preferred_video_size_mb", 36
    )
    max_video_size_mb: int = worker_config_value("max_video_size_mb", 50)
    media_adaptive_max_attempts: int = worker_config_value(
        "media_adaptive_max_attempts", 4
    )
    enable_social_video: bool = False
    enable_yt_dlp: bool = False
    cookie_dir: str = "/app/cookies"
    cookie_file_path: str = "/app/cookies.txt"
    enable_cookie_file: bool = True
    enable_generic_cookie_fallback: bool = False
    enable_ytdlp_cookies: bool = worker_config_value("enable_ytdlp_cookies", False)
    enable_instagram_cookies: bool = worker_config_value(
        "enable_instagram_cookies", False
    )
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
    ytdlp_user_agent: Optional[str] = worker_config_value("ytdlp_user_agent", None)
    enable_metadata_only_mock_fallback: bool = worker_config_value(
        "enable_metadata_only_mock_fallback", True
    )
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
    max_video_height: int = worker_config_value("max_video_height", 720)
    min_video_height: int = worker_config_value("min_video_height", 360)
    media_resolver_mode: MediaResolverMode = worker_config_value(
        "media_resolver_mode", "legacy"
    )
    external_media_resolver_audience: ExternalMediaResolverAudience = (
        worker_config_value("external_media_resolver_audience", "disabled")
    )
    instagram_resolver_chain: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    tiktok_resolver_chain: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    cookie_media_fallback_enabled: bool = worker_config_value(
        "cookie_media_fallback_enabled", False
    )
    youtube_direct_gemini_enabled: bool = worker_config_value(
        "youtube_direct_gemini_enabled", True
    )
    external_provider_timeout_seconds: int = worker_config_value(
        "external_provider_timeout_seconds", 15
    )
    external_provider_response_max_bytes: int = worker_config_value(
        "external_provider_response_max_bytes", 2 * 1024 * 1024
    )
    external_media_download_connect_timeout_seconds: int = worker_config_value(
        "external_media_download_connect_timeout_seconds", 10
    )
    external_media_download_read_timeout_seconds: int = worker_config_value(
        "external_media_download_read_timeout_seconds", 30
    )
    external_media_download_total_timeout_seconds: int = worker_config_value(
        "external_media_download_total_timeout_seconds", 120
    )
    external_media_max_redirects: int = worker_config_value(
        "external_media_max_redirects", 2
    )
    instagram_rapidapi_key: Optional[SecretStr] = None
    instagram_rapidapi_host: str = worker_config_value(
        "instagram_rapidapi_host", ""
    )
    instagram_rapidapi_base_url: str = worker_config_value(
        "instagram_rapidapi_base_url", ""
    )
    tiktok_akng_access_token: Optional[SecretStr] = None
    tiktok_akng_base_url: str = worker_config_value(
        "tiktok_akng_base_url", ""
    )
    instagram_media_host_suffixes: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    tiktok_media_host_suffixes: Annotated[list[str], NoDecode] = Field(
        default_factory=list
    )
    thumbnail_bucket: str = worker_config_value("thumbnail_bucket", "recipe-thumbnails")
    use_source_thumbnail_first: bool = True
    store_raw_media: bool = False
    delete_raw_media_after_processing: bool = True
    enable_tikwm_resolver: bool = worker_config_value("enable_tikwm_resolver", True)
    enable_render_youtube_resolver: bool = True
    youtube_resolver: YouTubeResolver = "ytdlp"
    instagram_resolver: InstagramResolver = "snapinsta"
    facebook_resolver: FacebookResolver = "snapsave"
    worker_execution_mode: WorkerExecutionMode = worker_config_value(
        "worker_execution_mode", "queue"
    )
    queue_poll_interval_seconds: int = worker_config_value(
        "queue_poll_interval_seconds", 5
    )
    queue_empty_sleep_seconds: int = worker_config_value("queue_empty_sleep_seconds", 5)
    enable_unified_ytdlp_stream_extractor: bool = worker_config_value(
        "enable_unified_ytdlp_stream_extractor", False
    )
    media_extract_output_mode: MediaExtractOutputMode = worker_config_value(
        "media_extract_output_mode", "temp_file"
    )
    snapinsta_api_url: str = "https://snapinsta.app"
    snapsave_api_url: str = "https://snapsave.app"
    tikwm_api_url: str = worker_config_value(
        "tikwm_api_url", "https://www.tikwm.com/api/"
    )
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
    enable_render_instagram_resolver: bool = worker_config_value(
        "enable_render_instagram_resolver", False
    )
    enable_render_facebook_resolver: bool = False
    cobalt_api_url: Optional[str] = None
    cobalt_timeout_seconds: int = 45
    cobalt_always_proxy: bool = True
    cobalt_video_quality: str = "720"
    cobalt_enabled_platforms: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["instagram", "facebook"]
    )
    cobalt_api_key: Optional[SecretStr] = None
    temp_media_dir: str = worker_config_value("temp_media_dir", "/tmp/recipe-dost-media")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (
            init_settings,
            _without_blank_values(env_settings),
            _without_blank_values(dotenv_settings),
            worker_settings_file_source,
            file_secret_settings,
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
    def preferred_video_size_bytes(self) -> int:
        return self.preferred_video_size_mb * 1024 * 1024

    @property
    def max_video_size_bytes(self) -> int:
        return self.max_video_size_mb * 1024 * 1024

    @property
    def tikwm_configured(self) -> bool:
        return self.enable_tikwm_resolver and bool(self.tikwm_api_url.strip())

    @property
    def cobalt_configured(self) -> bool:
        return self.enable_cobalt_resolver and bool((self.cobalt_api_url or "").strip())

    @property
    def external_resolver_enabled(self) -> bool:
        return (
            self.media_resolver_mode in {"external", "hybrid"}
            and self.external_media_resolver_audience != "disabled"
        )

    @property
    def legacy_cookie_media_enabled(self) -> bool:
        return (
            self.media_resolver_mode in {"legacy", "hybrid"}
            and self.cookie_media_fallback_enabled
        )

    @field_validator("gemini_video_fallback_models", mode="before")
    @classmethod
    def parse_fallback_models(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator(
        "tikwm_enabled_platforms",
        "cobalt_enabled_platforms",
        "instagram_resolver_chain",
        "tiktok_resolver_chain",
        "instagram_media_host_suffixes",
        "tiktok_media_host_suffixes",
        mode="before",
    )
    @classmethod
    def parse_platform_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [
                item.strip().lower()
                for item in value.split(",")
                if item.strip()
            ]
        return value

    @field_validator("instagram_resolver_chain", "tiktok_resolver_chain")
    @classmethod
    def validate_resolver_chain(
        cls,
        value: list[str],
        info: ValidationInfo,
    ) -> list[str]:
        normalized: list[str] = []
        for adapter_id in value:
            item = str(adapter_id).strip().lower()
            if not item or item in normalized:
                continue
            if item not in REGISTERED_EXTERNAL_MEDIA_ADAPTERS:
                raise ValueError(
                    f"{info.field_name} contains unknown adapter identifier: {item}"
                )
            normalized.append(item)
        if len(normalized) > 2:
            raise ValueError("resolver chain supports at most primary plus one fallback")
        return normalized

    @field_validator("instagram_media_host_suffixes", "tiktok_media_host_suffixes")
    @classmethod
    def validate_media_host_suffixes(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for suffix in value:
            item = str(suffix).strip().lower().lstrip(".")
            if not item or "/" in item or ":" in item or item in normalized:
                continue
            normalized.append(item)
        return normalized

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

    @model_validator(mode="after")
    def validate_media_limits(self) -> "Settings":
        if self.preferred_video_size_mb <= 0:
            raise ValueError("preferred_video_size_mb must be greater than 0")
        if self.max_video_size_mb <= 0:
            raise ValueError("max_video_size_mb must be greater than 0")
        if self.preferred_video_size_mb > self.max_video_size_mb:
            raise ValueError(
                "preferred_video_size_mb must not exceed max_video_size_mb"
            )
        if self.max_video_size_bytes > self.gemini_file_max_size_bytes:
            raise ValueError(
                "max_video_size_mb must not exceed gemini_file_max_size_bytes"
            )
        if not 1 <= self.media_adaptive_max_attempts <= 4:
            raise ValueError("media_adaptive_max_attempts must be between 1 and 4")
        if not 360 <= self.min_video_height <= self.max_video_height <= 720:
            raise ValueError(
                "video height limits must satisfy 360 <= min <= max <= 720"
            )
        if not 5 <= self.external_provider_timeout_seconds <= 60:
            raise ValueError("external_provider_timeout_seconds must be between 5 and 60")
        if not 64 * 1024 <= self.external_provider_response_max_bytes <= 4 * 1024 * 1024:
            raise ValueError("external_provider_response_max_bytes is outside safe bounds")
        if not 0 <= self.external_media_max_redirects <= 3:
            raise ValueError("external_media_max_redirects must be between 0 and 3")
        if not 1 <= self.external_media_download_connect_timeout_seconds <= 30:
            raise ValueError("external media connect timeout is outside safe bounds")
        if not 1 <= self.external_media_download_read_timeout_seconds <= 60:
            raise ValueError("external media read timeout is outside safe bounds")
        if not 10 <= self.external_media_download_total_timeout_seconds <= self.max_job_runtime_seconds:
            raise ValueError("external media total timeout exceeds the job budget")
        if any(
            adapter != "rapidapi_instagram_reels13"
            for adapter in self.instagram_resolver_chain
        ):
            raise ValueError("Instagram resolver chain contains an incompatible adapter")
        if any(adapter != "akng_tiktok" for adapter in self.tiktok_resolver_chain):
            raise ValueError("TikTok resolver chain contains an incompatible adapter")
        return self

    @field_validator("media_resolver_mode", mode="before")
    @classmethod
    def validate_media_resolver_mode(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        if normalized not in {"legacy", "external", "hybrid"}:
            raise ValueError("unknown media resolver mode")
        return normalized

    @field_validator("external_media_resolver_audience", mode="before")
    @classmethod
    def validate_external_media_resolver_audience(cls, value: object) -> object:
        normalized = str(value).strip().lower()
        if normalized not in {"disabled", "test_users", "all_authorized_ai"}:
            raise ValueError("unknown external media resolver audience")
        return normalized

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


def validate_external_resolver_runtime_config(settings: Settings) -> None:
    """Fail startup closed only when external adapters can receive traffic."""
    if not settings.external_resolver_enabled:
        return
    if not settings.instagram_resolver_chain and not settings.tiktok_resolver_chain:
        report = worker_config_report(settings.app_env)
        selected_path = report.get("selected_config_display_path", "unknown")
        raise ValueError(
            f"{settings.app_env.capitalize()} external resolver configuration is enabled, "
            f"but no social adapter chain was loaded from {selected_path}. Expected at "
            "least one of INSTAGRAM_RESOLVER_CHAIN or TIKTOK_RESOLVER_CHAIN."
        )
    if "rapidapi_instagram_reels13" in settings.instagram_resolver_chain:
        if settings.instagram_rapidapi_key is None:
            raise ValueError("active Instagram resolver credentials are missing")
        if not settings.instagram_rapidapi_host.strip() or not settings.instagram_rapidapi_base_url.strip():
            raise ValueError("active Instagram resolver host configuration is missing")
        instagram_base = urlparse(settings.instagram_rapidapi_base_url)
        if (
            instagram_base.scheme != "https"
            or (instagram_base.hostname or "").lower()
            != settings.instagram_rapidapi_host.strip().lower()
        ):
            raise ValueError("active Instagram resolver endpoint must match its HTTPS host")
        if not settings.instagram_media_host_suffixes:
            raise ValueError("active Instagram media host policy is missing")
    if "akng_tiktok" in settings.tiktok_resolver_chain:
        if settings.tiktok_akng_access_token is None:
            raise ValueError("active TikTok resolver credentials are missing")
        if not settings.tiktok_akng_base_url.strip():
            raise ValueError("active TikTok resolver host configuration is missing")
        tiktok_base = urlparse(settings.tiktok_akng_base_url)
        if tiktok_base.scheme != "https" or not tiktok_base.hostname:
            raise ValueError("active TikTok resolver endpoint must use HTTPS")
        if not settings.tiktok_media_host_suffixes:
            raise ValueError("active TikTok media host policy is missing")
