from __future__ import annotations

import hmac
import asyncio
import json
import logging
import platform
from importlib.metadata import PackageNotFoundError, version
from pydantic import BaseModel
from pathlib import Path
from typing import Annotated, Any, Optional, Union
from uuid import UUID

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.ai.gemini_recipe import GeminiRecipeAnalyzer
from app.config import Settings, get_settings, validate_external_resolver_runtime_config
from app.job_runner import JobNotFoundError, JobRunner, WorkerProcessingError
from app.metadata_preview import (
    MetadataPreviewError,
    SUPPORTED_METADATA_PLATFORMS,
    fetch_metadata_preview,
)
from app.worker_runtime.cleanup import cleanup_stale_media
from app.observability.sentry import flush_sentry, initialize_sentry, report_worker_error
from app.queue_worker import (
    queue_liveness_config,
    reset_queue_runtime,
    run_queue_loop,
    signal_queue_wake,
)
from app.runtime_config import worker_config_report
from app.schemas import (
    ErrorDetail,
    ErrorResponse,
    HealthResponse,
    ProcessJobRequest,
    ProcessJobResponse,
)
from app.supabase_client import create_repository
from app.social.platforms import detect_social_platform
from app.social.models import SocialResolverError
from app.social.resolver_orchestrator import resolve_social_video
from app.social.stream_download import probe_video_stream
from app.social.yt_dlp_resolver import (
    get_yt_dlp_version,
)
from app.social.tikwm_resolver import debug_tikwm

logger = logging.getLogger("recipe_dost.worker")

PROCESSING_STATUSES = {"resolving", "downloading", "analyzing"}
INITIAL_JOB_STATUS = "queued"
queue_task: asyncio.Task[None] | None = None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            "logger": record.name,
        }
        for field in (
            "media_job_id",
            "exception_type",
            "safe_config",
            "stage",
            "failure_stage",
            "reason",
            "attempt",
            "from_resolution",
            "to_resolution",
            "error_code",
            "normalized_error_code",
            "source_platform",
            "attempts",
            "duration_bucket",
            "size_bucket",
            "selected_resolution_bucket",
            "normalized_candidate_count",
            "candidate_source_type",
            "audio_state",
            "probe_performed",
            "probe_audio_present",
            "probe_video_present",
            "optimization_attempted",
            "optimization_succeeded",
            "terminal",
            "sentry_event_id",
            "resolver_adapter",
            "route_audience",
            "fallback_used",
            "provider_attempt",
            "provider_status_class",
            "media_mode",
            "latency_bucket",
            "json_parsed",
            "provider_input_kind",
            "alternate_input_attempted",
            "contract_complete",
            "short_link_expanded",
            "short_link_redirect_count",
            "first_input_contract_complete",
            "success_present",
            "success_type",
            "success_value",
            "media_type",
            "data_present",
            "content_present",
            "media_url_present",
            "thumbnail_url_present",
            "audio_object_present",
            "audio_url_present",
            "audio_downloadable",
            "normalized_video_count",
            "normalized_audio_count",
            "normalized_thumbnail_count",
            "data_type",
            "item_info_present",
            "item_struct_present",
            "video_present",
            "bitrate_info_present",
            "bitrate_info_type",
            "bitrate_info_count",
            "bitrate_audio_info_present",
            "bitrate_audio_info_type",
            "bitrate_audio_info_count",
            "play_addr_struct_present",
            "download_urls_count",
            "status_code",
            "status_msg_present",
            "raw_video_candidates",
            "eligible_video_candidates",
            "raw_audio_candidates",
            "eligible_audio_candidates",
            "raw_candidate_count",
            "raw_video_count",
            "raw_audio_count",
            "unknown_role_count",
            "declared_codec_present_count",
            "declared_container_present_count",
            "declared_mime_present_count",
            "size_present_count",
            "dimensions_present_count",
            "eligible_declared_video_count",
            "probe_required_video_count",
            "eligible_audio_count",
            "rejection_counts",
            "stale_directories_removed",
            "google_genai",
            "pydantic",
            "pydantic_core",
            "pydantic_settings",
            "python",
            "worker_processing_model",
            "pipeline_mode",
            "configured_concurrency",
            "queue_depth_bucket",
            "active_stage_count",
            "rss_bucket",
            "temp_byte_bucket",
            "latency_bucket",
            "retry_recovery_category",
            "lease_state_category",
        ):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, separators=(",", ":"))


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    worker_logger = logging.getLogger("recipe_dost.worker")
    worker_logger.handlers.clear()
    worker_logger.addHandler(handler)
    worker_logger.setLevel(logging.INFO)
    worker_logger.propagate = False


configure_logging()
initialize_sentry()

app = FastAPI(
    title="Recipe Dost Media Worker",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
)


class UnauthorizedWorkerError(Exception):
    pass


def require_worker_secret(
    x_worker_secret: Annotated[
        Optional[str],
        Header(alias="X-Worker-Secret"),
    ] = None,
    authorization: Annotated[
        Optional[str],
        Header(alias="Authorization"),
    ] = None,
    settings: Settings = Depends(get_settings),
) -> None:
    expected = settings.worker_api_secret.get_secret_value()
    bearer = None
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            bearer = token.strip()
    if not (
        (x_worker_secret is not None and hmac.compare_digest(x_worker_secret, expected))
        or (bearer is not None and hmac.compare_digest(bearer, expected))
    ):
        raise UnauthorizedWorkerError


def error_response(code: str, message: str, status_code: int) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


@app.exception_handler(UnauthorizedWorkerError)
def handle_unauthorized_worker(
    _request: Request,
    _exc: UnauthorizedWorkerError,
) -> JSONResponse:
    return error_response(
        "unauthorized",
        "Missing or invalid worker secret.",
        401,
    )


@app.exception_handler(RequestValidationError)
def handle_request_validation(
    _request: Request,
    _exc: RequestValidationError,
) -> JSONResponse:
    return error_response(
        "invalid_request",
        "media_job_id must be a valid UUID.",
        422,
    )


@app.get("/health", response_model=HealthResponse)
def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    return HealthResponse(
        runtime_platform=settings.runtime_platform,
        worker_mode=settings.worker_mode,
        background_mode="fastapi_background_tasks",
        gemini_configured=settings.gemini_configured,
        gemini_video_model=settings.effective_gemini_video_model,
        gemini_text_model=settings.effective_gemini_text_model,
        gemini_video_fallback_models_count=len(
            settings.gemini_video_fallback_models
        ),
        ai_analysis_mode=settings.ai_analysis_mode,
        gemini_video_input_mode=settings.gemini_video_input_mode,
        gemini_video_analysis_mode=settings.gemini_extraction_mode,
        gemini_extraction_mode=settings.gemini_extraction_mode,
        gemini_primary_model=settings.gemini_primary_model,
        gemini_fallback_model=settings.gemini_fallback_model,
        gemini_thinking_level=settings.gemini_thinking_level,
        gemini_structured_output_enabled=settings.gemini_structured_output_enabled,
        local_brief_parser_enabled=settings.local_brief_parser_enabled,
        gemini_file_upload_timeout_seconds=settings.gemini_file_upload_timeout_seconds,
        gemini_file_max_size_bytes=settings.gemini_file_max_size_bytes,
        gemini_file_poll_interval_seconds=settings.gemini_file_poll_interval_seconds,
        gemini_file_max_wait_seconds=settings.gemini_file_max_wait_seconds,
        gemini_save_mode=settings.gemini_save_mode,
        gemini_json_retry_enabled=settings.gemini_json_retry_enabled,
        gemini_json_fallback_enabled=settings.gemini_json_fallback_enabled,
        gemini_video_brief_max_output_tokens=(
            settings.gemini_video_brief_max_output_tokens
        ),
        gemini_recipe_json_max_output_tokens=(
            settings.gemini_recipe_json_max_output_tokens
        ),
        gemini_recipe_brief_max_chars=settings.gemini_recipe_brief_max_chars,
        gemini_max_output_tokens=settings.gemini_max_output_tokens,
        gemini_video_brief_retry_enabled=settings.gemini_video_brief_retry_enabled,
        recipe_quality_gate_enabled=settings.recipe_quality_gate_enabled,
        recipe_repair_enabled=settings.recipe_repair_enabled,
        recipe_min_ingredients=settings.recipe_min_ingredients,
        recipe_min_steps=settings.recipe_min_steps,
        recipe_min_brief_length=settings.recipe_min_brief_length,
        recipe_bilingual_output_enabled=settings.recipe_bilingual_output_enabled,
        recipe_urdu_fallback_enabled=settings.recipe_urdu_fallback_enabled,
        recipe_card_normalization_enabled=settings.recipe_card_normalization_enabled,
        allow_private_metadata_failure=settings.allow_private_metadata_failure,
        enable_queue_wake_endpoint=settings.enable_queue_wake_endpoint,
        youtube_ai_input_mode=settings.youtube_ai_input_mode,
        youtube_metadata_mode=settings.youtube_metadata_mode,
        supported_platforms=["tiktok", "youtube", "instagram", "facebook"],
        youtube_resolver=settings.youtube_resolver,
        instagram_resolver=settings.instagram_resolver,
        facebook_resolver=settings.facebook_resolver,
        mock_video_processing_mode=settings.mock_video_processing_mode,
        mock_recipe_fixture_configured=bool(settings.mock_recipe_fixture_path.strip()),
        gemini_max_calls_per_job=settings.gemini_max_calls_per_job,
        gemini_daily_dev_call_limit=settings.gemini_daily_dev_call_limit,
        gemini_social_upload_mode=settings.gemini_social_upload_mode,
        gemini_video_observation_timeout_seconds=(
            settings.gemini_video_observation_timeout_seconds
        ),
        gemini_text_normalization_timeout_seconds=(
            settings.gemini_text_normalization_timeout_seconds
        ),
        max_job_runtime_seconds=settings.max_job_runtime_seconds,
        social_analysis_mode=settings.social_analysis_mode,
        enable_tiktok_text_fallback=settings.enable_tiktok_text_fallback,
        enable_social_video_gemini=settings.enable_social_video_gemini,
        enable_render_youtube_resolver=settings.enable_render_youtube_resolver,
        enable_render_instagram_resolver=settings.enable_render_instagram_resolver,
        enable_render_facebook_resolver=settings.enable_render_facebook_resolver,
        social_video_gemini_timeout_seconds=settings.social_video_gemini_timeout_seconds,
        social_text_gemini_timeout_seconds=settings.social_text_gemini_timeout_seconds,
        enable_gemini_repair_call=settings.enable_gemini_repair_call,
        enable_gemini_fallbacks=settings.enable_gemini_fallbacks,
        disabled_models=settings.gemini_disabled_models,
        daily_model_limits=settings.gemini_daily_dev_model_limits,
        budget_reset_at=settings.gemini_dev_budget_reset_at,
        media_pipeline_mode=settings.media_pipeline_mode,
        enable_social_video=settings.enable_social_video,
        enable_yt_dlp=settings.enable_yt_dlp,
        enable_cookie_file=settings.enable_cookie_file,
        max_source_video_duration_seconds=(
            settings.max_source_video_duration_seconds
        ),
        max_video_duration_seconds=settings.max_source_video_duration_seconds,
        preferred_video_size_mb=settings.preferred_video_size_mb,
        max_video_size_mb=settings.max_video_size_mb,
        media_adaptive_max_attempts=settings.media_adaptive_max_attempts,
        max_memory_buffer_mb=settings.max_memory_buffer_mb,
        max_video_height=settings.max_video_height,
        min_video_height=settings.min_video_height,
        thumbnail_bucket=settings.thumbnail_bucket,
        use_source_thumbnail_first=settings.use_source_thumbnail_first,
        store_raw_media=settings.store_raw_media,
        delete_raw_media_after_processing=settings.delete_raw_media_after_processing,
        cookie_dir=settings.cookie_dir,
        enable_generic_cookie_fallback=settings.enable_generic_cookie_fallback,
        tiktok_cookie_file_present=Path(settings.tiktok_cookie_file).exists(),
        instagram_cookie_file_present=Path(settings.instagram_cookie_file).exists(),
        facebook_cookie_file_present=Path(settings.facebook_cookie_file).exists(),
        generic_cookie_file_present=Path(settings.cookie_file_path).exists(),
        ytdlp_extract_timeout_seconds=settings.ytdlp_extract_timeout_seconds,
        ytdlp_socket_timeout_seconds=settings.ytdlp_socket_timeout_seconds,
        ytdlp_retries=settings.ytdlp_retries,
        ytdlp_use_subprocess=settings.ytdlp_use_subprocess,
        ytdlp_force_generic=settings.ytdlp_force_generic,
        ytdlp_enable_impersonation=settings.ytdlp_enable_impersonation,
        enable_ytdlp_stdout_fallback=settings.enable_ytdlp_stdout_fallback,
        yt_dlp_version=get_yt_dlp_version(),
        enable_tikwm_resolver=settings.enable_tikwm_resolver,
        tikwm_configured=settings.tikwm_configured,
        tikwm_api_url_configured=bool(settings.tikwm_api_url.strip()),
        tikwm_enabled_platforms=settings.tikwm_enabled_platforms,
        tikwm_hd=settings.tikwm_hd,
        tikwm_probe_bytes=settings.tikwm_probe_bytes,
        enable_cobalt_resolver=settings.enable_cobalt_resolver,
        cobalt_configured=settings.cobalt_configured,
        cobalt_enabled_platforms=settings.cobalt_enabled_platforms,
        worker_execution_mode=settings.worker_execution_mode,
        worker_processing_model="sequential_queue",
        queue_loop_enabled=settings.worker_execution_mode == "queue"
        and queue_task is not None
        and not queue_task.done(),
        queue_poll_interval_seconds=settings.queue_poll_interval_seconds,
        enable_unified_ytdlp_stream_extractor=(
            settings.enable_unified_ytdlp_stream_extractor
        ),
        media_extract_output_mode=settings.media_extract_output_mode,
        enable_ytdlp_cookies=settings.enable_ytdlp_cookies,
        enable_instagram_cookies=settings.enable_instagram_cookies,
        enable_youtube_cookies=settings.enable_youtube_cookies,
        enable_facebook_cookies=settings.enable_facebook_cookies,
        enable_tiktok_cookies=settings.enable_tiktok_cookies,
        instagram_cookies_configured=cookies_configured(settings, "instagram"),
        youtube_cookies_configured=cookies_configured(settings, "youtube"),
        facebook_cookies_configured=cookies_configured(settings, "facebook"),
        tiktok_cookies_configured=cookies_configured(settings, "tiktok"),
        enable_metadata_only_mock_fallback=settings.enable_metadata_only_mock_fallback,
        enable_og_metadata_fallback=settings.enable_og_metadata_fallback,
        allow_mock_completion_without_video=settings.allow_mock_completion_without_video,
        metadata_preview_enabled=True,
        supported_metadata_platforms=SUPPORTED_METADATA_PLATFORMS,
        safe_config=safe_config_summary(settings),
    )


def safe_config_summary(settings: Settings) -> dict[str, object]:
    tiktok_enabled = (
        settings.enable_tikwm_resolver and "tiktok" in settings.tikwm_enabled_platforms
    )
    queue_mode = settings.worker_execution_mode == "queue"
    return {
        "runtime_dependencies": runtime_dependency_versions(),
        "app_env": settings.app_env,
        "worker_execution_mode": settings.worker_execution_mode,
        "worker_processing_model": "sequential_queue",
        "queue_mode_active": queue_mode,
        **queue_liveness_config(settings).safe_config(),
        "process_job_mode": (
            "queue_wake_compatibility" if queue_mode else "legacy_direct_background"
        ),
        "ai_analysis_mode": settings.ai_analysis_mode,
        "enabled_platforms": {
            "youtube": settings.enable_render_youtube_resolver,
            "instagram": settings.enable_render_instagram_resolver,
            "tiktok": tiktok_enabled,
            "facebook": settings.enable_render_facebook_resolver,
        },
        "resolvers": {
            "youtube": settings.youtube_resolver,
            "instagram": settings.instagram_resolver,
            "tiktok": (
                "queue_unified_ytdlp"
                if queue_mode
                else ("tikwm" if tiktok_enabled else "disabled")
            ),
            "tiktok_legacy_manual": "tikwm" if tiktok_enabled else "disabled",
            "facebook": settings.facebook_resolver,
        },
        "enable_unified_ytdlp_stream_extractor": (
            settings.enable_unified_ytdlp_stream_extractor
        ),
        "enable_instagram_cookies": settings.enable_instagram_cookies,
        "enable_ytdlp_cookies": settings.enable_ytdlp_cookies,
        "has_instagram_cookies": cookies_configured(settings, "instagram"),
        "has_youtube_cookies": cookies_configured(settings, "youtube"),
        "has_tiktok_cookies": cookies_configured(settings, "tiktok"),
        "media_extract_output_mode": settings.media_extract_output_mode,
        "max_source_video_duration_seconds": (
            settings.max_source_video_duration_seconds
        ),
        "max_video_duration_seconds": settings.max_source_video_duration_seconds,
        "preferred_video_size_mb": settings.preferred_video_size_mb,
        "max_video_size_mb": settings.max_video_size_mb,
        "media_adaptive_max_attempts": settings.media_adaptive_max_attempts,
        "media_resolver_mode": settings.media_resolver_mode,
        "external_media_resolver_audience": settings.external_media_resolver_audience,
        "instagram_resolver_chain": settings.instagram_resolver_chain,
        "tiktok_resolver_chain": settings.tiktok_resolver_chain,
        "cookie_media_fallback_enabled": settings.cookie_media_fallback_enabled,
        "youtube_direct_gemini_enabled": settings.youtube_direct_gemini_enabled,
        "external_provider_credentials_configured": {
            "rapidapi_instagram_unified_v1": (
                settings.instagram_rapidapi_key is not None
            ),
            "akng_tiktok": settings.tiktok_akng_access_token is not None,
        },
        "instagram_external_resolver": {
            "adapter_identifier": (
                settings.instagram_resolver_chain[0]
                if len(settings.instagram_resolver_chain) == 1
                else "disabled"
            ),
            "enabled": (
                settings.external_resolver_enabled
                and settings.instagram_resolver_chain
                == ["rapidapi_instagram_unified_v1"]
            ),
            "endpoint_configured": bool(
                settings.instagram_rapidapi_host.strip()
                and settings.instagram_rapidapi_base_url.strip()
            ),
            "credential_present": settings.instagram_rapidapi_key is not None,
            "approved_host_suffix_categories": _instagram_host_categories(
                settings.instagram_media_host_suffixes
            ),
        },
        "min_video_height": settings.min_video_height,
        "max_video_height": settings.max_video_height,
        "gemini_file_max_size_bytes": settings.gemini_file_max_size_bytes,
        "gemini_video_model": settings.effective_gemini_video_model,
        "gemini_text_model": settings.effective_gemini_text_model,
        "raw_url_logging_enabled": settings.allow_raw_url_logs,
        "config_source": safe_config_source_summary(settings),
        "mock_fallback": {
            "ai_analysis_mode": settings.ai_analysis_mode,
            "real_ai_fallback_to_mock": settings.real_ai_fallback_to_mock,
            "enable_metadata_only_mock_fallback": (
                settings.enable_metadata_only_mock_fallback
            ),
            "allow_mock_completion_without_video": (
                settings.allow_mock_completion_without_video
            ),
        },
    }


def runtime_dependency_versions() -> dict[str, str]:
    packages = {
        "google_genai": "google-genai",
        "pydantic": "pydantic",
        "pydantic_core": "pydantic-core",
        "pydantic_settings": "pydantic-settings",
    }
    versions: dict[str, str] = {}
    for field, distribution in packages.items():
        try:
            versions[field] = version(distribution)
        except PackageNotFoundError:
            versions[field] = "not_installed"
    versions["python"] = platform.python_version()
    return versions


def log_runtime_dependency_versions() -> None:
    try:
        logger.info(
            "runtime_dependency_versions",
            extra={
                **runtime_dependency_versions(),
                "worker_processing_model": "sequential_queue",
            },
        )
    except Exception:
        # Startup diagnostics are best-effort and must not block the worker.
        return


def _instagram_host_categories(suffixes: list[str]) -> list[str]:
    categories: list[str] = []
    for suffix in suffixes:
        normalized = suffix.lower().lstrip(".")
        category = (
            "meta_fbcdn"
            if normalized == "fbcdn.net"
            else "instagram_cdn"
            if normalized == "cdninstagram.com"
            else "other_approved"
        )
        if category not in categories:
            categories.append(category)
    return categories


def safe_config_source_summary(settings: Settings) -> dict[str, object]:
    report = worker_config_report(settings.app_env)
    all_sources = report.get("field_sources")
    source_map = all_sources if isinstance(all_sources, dict) else {}
    resolver_keys = (
        "MEDIA_RESOLVER_MODE",
        "EXTERNAL_MEDIA_RESOLVER_AUDIENCE",
        "INSTAGRAM_RESOLVER_CHAIN",
        "TIKTOK_RESOLVER_CHAIN",
        "COOKIE_MEDIA_FALLBACK_ENABLED",
        "YOUTUBE_DIRECT_GEMINI_ENABLED",
    )
    return {
        "app_env": report.get("app_env"),
        "environment_selection_source": report.get("environment_selection_source"),
        "selected_config_display_path": report.get("selected_config_display_path"),
        "selected_config_exists": report.get("selected_config_exists"),
        "selected_config_loaded": report.get("selected_config_loaded"),
        "loaded_files": report.get("loaded_files", []),
        "missing_files": report.get("missing_files", []),
        "resolver_field_sources": {
            key.lower(): source_map.get(key, "model_default") for key in resolver_keys
        },
    }


def cookies_configured(settings: Settings, platform: str) -> bool:
    file_value = {
        "instagram": settings.instagram_cookies_file or settings.instagram_cookie_file,
        "youtube": settings.youtube_cookies_file,
        "facebook": settings.facebook_cookies_file or settings.facebook_cookie_file,
        "tiktok": settings.tiktok_cookies_file or settings.tiktok_cookie_file,
    }.get(platform)
    b64_value = {
        "instagram": settings.instagram_cookies_b64,
        "youtube": settings.youtube_cookies_b64,
        "facebook": settings.facebook_cookies_b64,
        "tiktok": settings.tiktok_cookies_b64,
    }.get(platform)
    return bool((file_value and Path(file_value).exists()) or b64_value is not None)


@app.get("/ping")
def ping() -> dict[str, bool]:
    return {"ok": True}


def queue_loop_enabled(settings: Settings) -> bool:
    return (
        settings.worker_execution_mode == "queue"
        and queue_task is not None
        and not queue_task.done()
    )


def ensure_queue_worker_started(settings: Settings) -> None:
    global queue_task
    if settings.worker_execution_mode != "queue":
        return
    signal_queue_wake()
    if queue_task is not None and not queue_task.done():
        return
    try:
        queue_task = asyncio.create_task(run_queue_loop(build_job_runner))
    except Exception as exc:
        report_worker_error(
            exc,
            feature="worker_runtime",
            operation="worker_startup",
            stage="startup",
            error_code="worker_startup_failed",
            source_platform="unknown",
            recoverable=True,
            terminal=False,
        )
        raise


@app.get("/wake")
async def wake(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    logger.info("queue_wake_received")
    if settings.enable_queue_wake_endpoint:
        ensure_queue_worker_started(settings)
    return {
        "ok": True,
        "worker_execution_mode": settings.worker_execution_mode,
        "queue_loop_enabled": queue_loop_enabled(settings),
    }


@app.on_event("startup")
async def start_queue_worker() -> None:
    try:
        settings = get_settings()
        validate_external_resolver_runtime_config(settings)
        stale_directories_removed = cleanup_stale_media(settings)
        log_runtime_dependency_versions()
        logger.info(
            "worker_safe_config_summary",
            extra={
                "safe_config": safe_config_summary(settings),
                "stale_directories_removed": stale_directories_removed,
            },
        )
        ensure_queue_worker_started(settings)
    except Exception as exc:
        report_worker_error(
            exc,
            feature="worker_runtime",
            operation="worker_startup",
            stage="startup",
            error_code="worker_startup_failed",
            source_platform="unknown",
            recoverable=False,
            terminal=True,
        )
        raise


@app.on_event("shutdown")
async def stop_queue_worker() -> None:
    global queue_task
    if queue_task is not None:
        queue_task.cancel()
        try:
            await queue_task
        except asyncio.CancelledError:
            pass
        queue_task = None
    reset_queue_runtime()
    flush_sentry(timeout=2.0)


def build_job_runner(settings: Settings) -> JobRunner:
    analyzer = None
    if settings.gemini_api_key is not None:
        analyzer = GeminiRecipeAnalyzer(
            api_key=settings.gemini_api_key.get_secret_value(),
            model=settings.effective_gemini_video_model,
            text_model=settings.effective_gemini_text_model,
            fallback_models=(
                settings.gemini_video_fallback_models
                if settings.enable_gemini_fallbacks
                else []
            ),
            max_retries=settings.gemini_max_retries,
            retry_base_seconds=settings.gemini_retry_base_seconds,
            video_observation_timeout_seconds=(
                settings.gemini_video_observation_timeout_seconds
            ),
            text_normalization_timeout_seconds=(
                settings.gemini_text_normalization_timeout_seconds
            ),
            files_upload_timeout_seconds=settings.gemini_files_upload_timeout_seconds,
            files_processing_timeout_seconds=(
                settings.gemini_files_processing_timeout_seconds
            ),
            social_text_timeout_seconds=settings.social_text_gemini_timeout_seconds,
        )
    return JobRunner(
        create_repository(settings),
        analyzer,
        worker_mode=settings.worker_mode,
        app_env=settings.app_env,
        gemini_max_calls_per_job=settings.gemini_max_calls_per_job,
        gemini_daily_dev_call_limit=settings.gemini_daily_dev_call_limit,
        gemini_disabled_models=settings.gemini_disabled_models,
        gemini_daily_dev_model_limits=settings.gemini_daily_dev_model_limits,
        gemini_dev_budget_reset_at=settings.gemini_dev_budget_reset_at,
        settings=settings,
    )


def run_job_background(job_id: UUID, force: bool = False) -> None:
    repository = None
    try:
        settings = get_settings()
        repository = create_repository(settings)
        runner = build_job_runner(settings)
        runner.run(job_id, force=force)
    except JobNotFoundError:
        logger.warning(
            "background_job_not_found",
            extra={"media_job_id": str(job_id)},
        )
    except Exception as exc:
        logger.error(
            "background_worker_failed",
            extra={
                "media_job_id": str(job_id),
                "exception_type": type(exc).__name__,
            },
        )
        if repository is None:
            try:
                repository = create_repository(get_settings())
            except Exception:
                return
        try:
            repository.update_job(
                job_id,
                status="failed",
                progress=100,
                error_code="background_worker_error",
                error_message="Worker failed while processing this recipe.",
            )
            repository.insert_event(
                job_id,
                event_type="background_worker_error",
                message="Background worker failed while processing the job.",
                metadata={
                    "error_code": "background_worker_error",
                    "exception_type": type(exc).__name__,
                },
            )
        except Exception as recovery_exc:
            logger.error(
                "background_worker_recovery_failed",
                extra={
                    "media_job_id": str(job_id),
                    "exception_type": type(recovery_exc).__name__,
                },
            )


class DebugSocialResolveRequest(BaseModel):
    url: str
    resolver: str = "auto"
    test_stream: bool = False


class DebugTikWMRequest(BaseModel):
    url: str
    test_stream: bool = False


class MetadataPreviewRequest(BaseModel):
    url: str
    platform: Optional[str] = None


@app.post(
    "/metadata-preview",
    response_model=None,
    responses={
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
def metadata_preview_endpoint(
    request: MetadataPreviewRequest,
    _authorized: None = Depends(require_worker_secret),
    settings: Settings = Depends(get_settings),
) -> Union[dict[str, object], JSONResponse]:
    try:
        preview = fetch_metadata_preview(
            url=request.url,
            platform=request.platform,
            settings=settings,
        )
        return preview.as_dict()
    except MetadataPreviewError as exc:
        return error_response(exc.code, exc.message, exc.status_code)
    except Exception as exc:
        logger.warning(
            "metadata_preview_failed",
            extra={"exception_type": type(exc).__name__},
        )
        return error_response(
            "metadata_preview_failed",
            "Metadata preview could not be fetched.",
            502,
        )


@app.post("/debug/social-resolve")
def debug_social_resolve_endpoint(
    request: DebugSocialResolveRequest,
    _authorized: None = Depends(require_worker_secret),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    platform = detect_social_platform(request.url)
    try:
        info = resolve_social_video(
            request.url,
            platform,
            settings,
            resolver=request.resolver,
        )
        response: dict[str, object] = {
            "ok": True,
            "platform": info.platform,
            "resolver": info.resolver,
            "has_stream_url": True,
            "has_stream_headers": bool(info.stream_headers),
            "has_thumbnail": bool(info.thumbnail_url),
            "title_present": bool(info.title),
            "video_id_present": bool(info.video_id),
            "duration_seconds": info.duration_seconds,
            "extractor": info.extractor,
            "selected_ext": info.ext,
            "mime_type": info.mime_type,
        }
        if request.test_stream:
            response["stream_probe"] = probe_video_stream(info)
        return response
    except SocialResolverError as exc:
        return {
            "ok": False,
            "platform": platform,
            "resolver": request.resolver,
            "error_code": exc.code,
            "message": exc.message,
        }


@app.post("/debug/tikwm")
def debug_tikwm_endpoint(
    request: DebugTikWMRequest,
    _authorized: None = Depends(require_worker_secret),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    return debug_tikwm(
        request.url,
        settings,
        test_stream=request.test_stream,
    )


@app.post(
    "/process-job",
    response_model=ProcessJobResponse,
    responses={
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
)
async def process_job(
    request: ProcessJobRequest,
    background_tasks: BackgroundTasks,
    _authorized: None = Depends(require_worker_secret),
    settings: Settings = Depends(get_settings),
) -> Union[ProcessJobResponse, JSONResponse]:
    try:
        repository = create_repository(settings)
        job = repository.get_job(request.media_job_id)
        if job is None:
            raise JobNotFoundError
        if job.status == "completed":
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=ProcessJobResponse(
                    accepted=False,
                    job_id=request.media_job_id,
                    status=job.status,
                    reason="already_completed",
                ).model_dump(mode="json", exclude_none=True),
            )
        if job.status in PROCESSING_STATUSES and not request.force:
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content=ProcessJobResponse(
                    accepted=False,
                    job_id=request.media_job_id,
                    status=job.status,
                    reason="already_processing",
                ).model_dump(mode="json", exclude_none=True),
            )
        if job.status == "failed" and not request.force:
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content=ProcessJobResponse(
                    accepted=False,
                    job_id=request.media_job_id,
                    status=job.status,
                    reason="already_failed",
                ).model_dump(mode="json", exclude_none=True),
            )
        if job.status != INITIAL_JOB_STATUS and not request.force:
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content=ProcessJobResponse(
                    accepted=False,
                    job_id=request.media_job_id,
                    status=job.status,
                    reason="not_queued",
                ).model_dump(mode="json", exclude_none=True),
            )
        if request.force_reset_attempt_budget and settings.app_env == "development":
            repository.insert_event(
                request.media_job_id,
                event_type="dev_attempt_budget_reset_requested",
                message="Development attempt budget reset requested.",
                metadata={},
            )
        if settings.worker_execution_mode == "queue":
            if job.status in PROCESSING_STATUSES:
                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content=ProcessJobResponse(
                        accepted=False,
                        job_id=request.media_job_id,
                        status=job.status,
                        reason="already_processing",
                    ).model_dump(mode="json", exclude_none=True),
                )
            if job.status == "failed":
                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content=ProcessJobResponse(
                        accepted=False,
                        job_id=request.media_job_id,
                        status=job.status,
                        reason="already_failed",
                    ).model_dump(mode="json", exclude_none=True),
                )
            if job.status != INITIAL_JOB_STATUS:
                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content=ProcessJobResponse(
                        accepted=False,
                        job_id=request.media_job_id,
                        status=job.status,
                        reason="not_queued",
                    ).model_dump(mode="json", exclude_none=True),
                )
            repository.insert_event(
                request.media_job_id,
                event_type="process_job_queue_wake_requested",
                message="Process-job request accepted as a queue wake.",
                metadata={
                    "worker_execution_mode": settings.worker_execution_mode,
                    "queue_loop_enabled": queue_loop_enabled(settings),
                },
            )
            ensure_queue_worker_started(settings)
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content=ProcessJobResponse(
                    accepted=True,
                    job_id=request.media_job_id,
                    message="Job accepted; queue worker wake requested.",
                ).model_dump(mode="json", exclude_none=True),
            )
        background_tasks.add_task(
            run_job_background,
            request.media_job_id,
            request.force,
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=ProcessJobResponse(
                accepted=True,
                job_id=request.media_job_id,
                message="Job accepted for background processing.",
            ).model_dump(mode="json", exclude_none=True),
        )
    except JobNotFoundError:
        return error_response("job_not_found", "Media job not found.", 404)
    except WorkerProcessingError:
        return error_response("worker_error", "Media worker failed.", 500)
    except Exception as exc:
        log_request_failure(request.media_job_id, exc)
        return error_response("worker_error", "Media worker failed.", 500)


def log_request_failure(media_job_id: UUID, exception: Exception) -> None:
    logger.error(
        "worker_request_failed",
        extra={
            "media_job_id": str(media_job_id),
            "exception_type": type(exception).__name__,
        },
    )
