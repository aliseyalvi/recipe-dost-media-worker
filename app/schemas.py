from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class ProcessJobRequest(BaseModel):
    media_job_id: UUID = Field(validation_alias=AliasChoices("media_job_id", "job_id"))
    force: bool = False
    force_reset_attempt_budget: bool = False


class HealthResponse(BaseModel):
    ok: Literal[True] = True
    service: Literal["recipe-dost-media-worker"] = "recipe-dost-media-worker"
    runtime_platform: str
    worker_mode: str
    background_mode: Literal["fastapi_background_tasks"]
    gemini_configured: bool
    gemini_video_model: str
    gemini_text_model: str
    gemini_video_fallback_models_count: int
    ai_analysis_mode: str
    gemini_video_input_mode: str
    gemini_video_analysis_mode: str
    gemini_extraction_mode: str
    gemini_primary_model: str
    gemini_fallback_model: str
    gemini_thinking_level: str
    gemini_structured_output_enabled: bool
    local_brief_parser_enabled: bool
    gemini_file_upload_timeout_seconds: int
    gemini_file_max_size_bytes: int
    gemini_file_poll_interval_seconds: int
    gemini_file_max_wait_seconds: int
    gemini_save_mode: str
    gemini_json_retry_enabled: bool
    gemini_json_fallback_enabled: bool
    gemini_video_brief_max_output_tokens: int
    gemini_recipe_json_max_output_tokens: int
    gemini_recipe_brief_max_chars: int
    gemini_max_output_tokens: int
    gemini_video_brief_retry_enabled: bool
    recipe_quality_gate_enabled: bool
    recipe_repair_enabled: bool
    recipe_min_ingredients: int
    recipe_min_steps: int
    recipe_min_brief_length: int
    recipe_bilingual_output_enabled: bool
    recipe_urdu_fallback_enabled: bool
    recipe_card_normalization_enabled: bool
    allow_private_metadata_failure: bool
    enable_queue_wake_endpoint: bool
    youtube_ai_input_mode: str
    youtube_metadata_mode: str
    supported_platforms: list[str]
    youtube_resolver: str
    instagram_resolver: str
    facebook_resolver: str
    mock_video_processing_mode: str
    mock_recipe_fixture_configured: bool
    gemini_max_calls_per_job: int
    gemini_daily_dev_call_limit: int
    gemini_social_upload_mode: str
    gemini_video_observation_timeout_seconds: int
    gemini_text_normalization_timeout_seconds: int
    max_job_runtime_seconds: int
    social_analysis_mode: str
    enable_tiktok_text_fallback: bool
    enable_social_video_gemini: bool
    enable_render_youtube_resolver: bool
    enable_render_instagram_resolver: bool
    enable_render_facebook_resolver: bool
    social_video_gemini_timeout_seconds: int
    social_text_gemini_timeout_seconds: int
    enable_gemini_repair_call: bool
    enable_gemini_fallbacks: bool
    disabled_models: list[str]
    daily_model_limits: dict[str, int]
    budget_reset_at: Optional[str]
    media_pipeline_mode: str
    enable_social_video: bool
    enable_yt_dlp: bool
    enable_cookie_file: bool
    max_video_duration_seconds: int
    preferred_video_size_mb: int
    max_video_size_mb: int
    media_adaptive_max_attempts: int
    max_memory_buffer_mb: int
    max_video_height: int
    min_video_height: int
    thumbnail_bucket: str
    use_source_thumbnail_first: bool
    store_raw_media: bool
    delete_raw_media_after_processing: bool
    cookie_dir: str
    enable_generic_cookie_fallback: bool
    tiktok_cookie_file_present: bool
    instagram_cookie_file_present: bool
    facebook_cookie_file_present: bool
    generic_cookie_file_present: bool
    ytdlp_extract_timeout_seconds: int
    ytdlp_socket_timeout_seconds: int
    ytdlp_retries: int
    ytdlp_use_subprocess: bool
    ytdlp_force_generic: bool
    ytdlp_enable_impersonation: bool
    enable_ytdlp_stdout_fallback: bool
    yt_dlp_version: str
    enable_tikwm_resolver: bool
    tikwm_configured: bool
    tikwm_api_url_configured: bool
    tikwm_enabled_platforms: list[str]
    tikwm_hd: bool
    tikwm_probe_bytes: int
    enable_cobalt_resolver: bool
    cobalt_configured: bool
    cobalt_enabled_platforms: list[str]
    worker_execution_mode: str
    worker_pipeline_mode: str
    worker_stage_transport: str
    worker_dispatch_concurrency: int
    worker_ingest_concurrency: int
    worker_gemini_analysis_concurrency: int
    worker_finalize_concurrency: int
    worker_ffmpeg_concurrency: int
    worker_gemini_upload_concurrency: int
    worker_ingest_queue_size: int
    worker_analysis_queue_size: int
    worker_finalize_queue_size: int
    queue_loop_enabled: bool
    queue_poll_interval_seconds: int
    enable_unified_ytdlp_stream_extractor: bool
    media_extract_output_mode: str
    enable_ytdlp_cookies: bool
    enable_instagram_cookies: bool
    enable_youtube_cookies: bool
    enable_facebook_cookies: bool
    enable_tiktok_cookies: bool
    instagram_cookies_configured: bool
    youtube_cookies_configured: bool
    facebook_cookies_configured: bool
    tiktok_cookies_configured: bool
    enable_metadata_only_mock_fallback: bool
    enable_og_metadata_fallback: bool
    allow_mock_completion_without_video: bool
    metadata_preview_enabled: bool
    supported_metadata_platforms: list[str]
    safe_config: dict[str, Any]


class MediaJobData(BaseModel):
    media_job_id: UUID
    status: str
    progress: int = Field(ge=0, le=100)
    error_code: Optional[str] = None
    accepted: bool = False


class ProcessJobResponse(BaseModel):
    ok: Literal[True] = True
    accepted: bool
    job_id: UUID
    status: Optional[str] = None
    reason: Optional[str] = None
    message: Optional[str] = None


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    ok: Literal[False] = False
    error: ErrorDetail


class MediaJobRecord(BaseModel):
    id: UUID
    user_id: UUID
    recipe_item_id: Optional[UUID]
    source_url: str
    platform: str
    status: str
    progress: int = Field(ge=0, le=100)
    thumbnail_url: Optional[str] = None
    result_recipe_card_id: Optional[UUID] = None
    error_code: Optional[str] = None
    started_at: Optional[str] = None
    media_duration_seconds: Optional[int] = None
    media_size_bytes: Optional[int] = None
    source_title: Optional[str] = None
    source_thumbnail_url: Optional[str] = None

    model_config = ConfigDict(extra="ignore")


class RecipeIngredient(BaseModel):
    name_en: str
    name_ur: str
    quantity: str
    notes_en: str
    notes_ur: str


class RecipeStep(BaseModel):
    step_number: int = Field(ge=1)
    instruction_en: str
    instruction_ur: str
    tip_en: str
    tip_ur: str


class GroceryItem(BaseModel):
    name_en: str
    name_ur: str
    quantity: str


class GeminiRecipeResult(BaseModel):
    recipe_title_en: str
    recipe_title_ur: str
    short_summary_en: str
    short_summary_ur: str
    confidence: str
    prep_time_minutes: Optional[int]
    cook_time_minutes: Optional[int]
    servings: Optional[str]
    difficulty: str
    ingredients: list[RecipeIngredient]
    steps: list[RecipeStep]
    grocery_list: list[GroceryItem]
    tags: list[str] = Field(default_factory=list)
    missing_info_en: list[str]
    missing_info_ur: list[str]
    storage_tip_en: str
    storage_tip_ur: str
    safety_note_en: str
    safety_note_ur: str
    source_assumptions_en: list[str]
    source_assumptions_ur: list[str]

    model_config = ConfigDict(extra="ignore")

    @field_validator(
        "ingredients",
        "steps",
        "grocery_list",
        "tags",
        "missing_info_en",
        "missing_info_ur",
        "source_assumptions_en",
        "source_assumptions_ur",
        mode="before",
    )
    @classmethod
    def require_array(cls, value: Any) -> Any:
        if not isinstance(value, list):
            raise ValueError("Expected an array.")
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def clamp_confidence(cls, value: Any) -> str:
        normalized = str(value).strip().lower()
        return normalized if normalized in {"low", "medium", "high"} else "low"

    @field_validator("difficulty", mode="before")
    @classmethod
    def clamp_difficulty(cls, value: Any) -> str:
        normalized = str(value).strip().lower()
        return (
            normalized
            if normalized in {"easy", "medium", "hard", "unknown"}
            else "unknown"
        )
