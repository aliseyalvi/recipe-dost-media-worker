from __future__ import annotations

import hmac
import json
import logging
from pydantic import BaseModel
from pathlib import Path
from typing import Annotated, Any, Optional, Union
from uuid import UUID

from fastapi import BackgroundTasks, Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.ai.gemini_recipe import GeminiRecipeAnalyzer
from app.config import Settings, get_settings
from app.job_runner import JobNotFoundError, JobRunner, WorkerProcessingError
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


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            "logger": record.name,
        }
        for field in ("media_job_id", "exception_type"):
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
    settings: Settings = Depends(get_settings),
) -> None:
    expected = settings.worker_api_secret.get_secret_value()
    if x_worker_secret is None or not hmac.compare_digest(x_worker_secret, expected):
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
        worker_mode=settings.worker_mode,
        gemini_configured=settings.gemini_configured,
        gemini_video_model=settings.effective_gemini_video_model,
        gemini_video_fallback_models_count=len(
            settings.gemini_video_fallback_models
        ),
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
        max_video_duration_seconds=settings.max_video_duration_seconds,
        max_video_size_mb=settings.max_video_size_mb,
        max_memory_buffer_mb=settings.max_memory_buffer_mb,
        max_video_height=settings.max_video_height,
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
        tikwm_enabled_platforms=settings.tikwm_enabled_platforms,
        tikwm_hd=settings.tikwm_hd,
        tikwm_probe_bytes=settings.tikwm_probe_bytes,
        enable_cobalt_resolver=settings.enable_cobalt_resolver,
        cobalt_configured=settings.cobalt_configured,
        cobalt_enabled_platforms=settings.cobalt_enabled_platforms,
    )


class DebugSocialResolveRequest(BaseModel):
    url: str
    resolver: str = "auto"
    test_stream: bool = False


class DebugTikWMRequest(BaseModel):
    url: str
    test_stream: bool = False


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
def process_job(
    request: ProcessJobRequest,
    background_tasks: BackgroundTasks,
    _authorized: None = Depends(require_worker_secret),
    settings: Settings = Depends(get_settings),
) -> Union[ProcessJobResponse, JSONResponse]:
    try:
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
        runner = JobRunner(
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
        data, should_process = runner.accept(
            request.media_job_id,
            force=request.force,
            force_reset_attempt_budget=request.force_reset_attempt_budget,
        )
        if should_process:
            background_tasks.add_task(runner.run, request.media_job_id, True)
        return ProcessJobResponse(data=data)
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
