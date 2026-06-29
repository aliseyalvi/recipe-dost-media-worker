from __future__ import annotations

import asyncio
import io
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Callable
from uuid import UUID

from app.config import Settings, get_settings
from app.job_runner import (
    PipelineFailure,
    VIDEO_RECIPE_PROMPT,
    _caption_result_to_recipe_result,
    _gemini_delete_file_rest,
    _gemini_upload_video_rest,
    _gemini_video_to_recipe_rest,
    _validate_parsed_recipe,
    platform_for_job,
    save_mock_recipe_card_for_job,
)
from app.schemas import GeminiRecipeResult, MediaJobRecord
from app.social.stream_download import extract_social_video
from app.social.thumbnail_upload import upload_source_thumbnail
from app.supabase_client import MediaJobRepository, create_repository

logger = logging.getLogger("recipe_dost.worker")


class QueueJobFailure(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str,
        exception_type: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.stage = stage
        self.exception_type = exception_type


async def run_queue_loop(_build_job_runner: Callable[[Settings], object]) -> None:
    settings = get_settings()
    logger.info("queue_loop_started")
    while True:
        try:
            processed = await asyncio.to_thread(process_next_queued_job)
            sleep_for = (
                settings.queue_poll_interval_seconds
                if processed
                else settings.queue_empty_sleep_seconds
            )
            await asyncio.sleep(max(1, sleep_for))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "queue_loop_iteration_failed",
                extra={"exception_type": type(exc).__name__},
            )
            await asyncio.sleep(max(1, settings.queue_empty_sleep_seconds))


def process_next_queued_job() -> bool:
    settings = get_settings()
    repository = create_repository(settings)
    job = repository.get_oldest_queued_job()
    if job is None:
        return False
    process_queue_job(repository, settings, job)
    return True


def process_queue_job(
    repository: MediaJobRepository,
    settings: Settings,
    job: MediaJobRecord,
) -> None:
    extract_result: dict[str, object] | None = None
    video_buffer: io.BytesIO | None = None
    temp_file_path: str | None = None
    started_at = perf_counter()
    current_stage = "queue_claim"
    try:
        previous_status = job.status
        job = repository.update_job(
            job.id,
            status="resolving",
            progress=20,
            error_code=None,
            error_message=None,
            started_at=job.started_at or datetime.now(timezone.utc).isoformat(),
        )
        add_event(
            repository,
            job.id,
            "queue_loop_started",
            "Queue loop is active.",
            {"worker_execution_mode": settings.worker_execution_mode},
        )
        add_event(
            repository,
            job.id,
            "queue_job_claimed",
            "Queued job claimed for processing.",
            {
                "previous_status": previous_status,
                "status": "resolving",
                "progress": 20,
            },
        )

        if not settings.enable_unified_ytdlp_stream_extractor:
            current_stage = "unified_extractor"
            add_event(
                repository,
                job.id,
                "unified_extractor_failed",
                "Unified yt-dlp extractor is disabled.",
                {
                    "platform": platform_for_job(job),
                    "resolver": "ytdlp_stream",
                    "error_code": "unified_extractor_failed",
                },
            )
            raise QueueJobFailure(
                "unified_extractor_failed",
                "Unified social video extraction is not enabled.",
                stage=current_stage,
            )

        platform = platform_for_job(job)
        current_stage = "unified_extractor"
        add_event(
            repository,
            job.id,
            "unified_extractor_started",
            "Unified yt-dlp extractor started.",
            {"platform": platform, "resolver": "ytdlp_stream"},
        )
        job = repository.update_job(
            job.id,
            status="downloading",
            progress=35,
            error_code=None,
            error_message=None,
        )
        extract_result = extract_social_video(
            job.source_url,
            platform,
            str(job.id),
            settings,
        )
        emit_extractor_progress_events(repository, job.id, extract_result)
        if not extract_result.get("success"):
            error_code = str(extract_result.get("error_code") or "unified_extractor_failed")
            if not extract_result.get("metadata_fallback_attempted"):
                add_event(
                    repository,
                    job.id,
                    "unified_extractor_failed",
                    "Unified yt-dlp extractor failed.",
                    safe_extract_event_metadata(extract_result, error_code=error_code),
                )
            raise QueueJobFailure(
                error_code,
                str(
                    extract_result.get("safe_error_message")
                    or "This social video could not be processed."
                ),
                stage=(
                    "metadata_fallback"
                    if extract_result.get("metadata_fallback_attempted")
                    else current_stage
                ),
            )
        add_event(
            repository,
            job.id,
            "unified_extractor_completed",
            "Unified yt-dlp extractor completed.",
            safe_extract_event_metadata(extract_result),
        )

        video_buffer = extract_result.get("video_buffer")  # type: ignore[assignment]
        temp_file_path = extract_result.get("temp_file_path")  # type: ignore[assignment]
        uploaded_thumbnail_url = upload_thumbnail_best_effort(
            repository,
            settings,
            job,
            str_or_none(extract_result.get("thumbnail_url")),
        )
        current_stage = "metadata_save"
        save_metadata(repository, job.id, extract_result, uploaded_thumbnail_url)
        update_job_source_fields(repository, job.id, extract_result, uploaded_thumbnail_url)

        if settings.ai_analysis_mode == "gemini_video_poc":
            current_stage = "gemini_video_analysis"
            try:
                job = run_gemini_video_poc_analysis(
                    repository=repository,
                    job=job,
                    settings=settings,
                    extract_result=extract_result,
                    platform=platform,
                    uploaded_thumbnail_url=uploaded_thumbnail_url,
                )
            except QueueJobFailure:
                raise
            except Exception as exc:
                raise QueueJobFailure(
                    error_code_for_stage(current_stage),
                    safe_message_for_stage(current_stage),
                    stage=current_stage,
                    exception_type=type(exc).__name__,
                ) from exc
        else:
            current_stage = "mock_fixture_analysis"
            try:
                job = save_mock_recipe_card_for_job(
                    repository=repository,
                    job=job,
                    settings=settings,
                    platform=platform,
                    resolver=str(extract_result.get("resolver") or "ytdlp_stream"),
                    thumbnail_url=uploaded_thumbnail_url,
                    job_started_at=started_at,
                    fixture_error_code="mock_fixture_failed",
                )
            except PipelineFailure as exc:
                current_stage = str(exc.metadata.get("stage") or current_stage)
                raise QueueJobFailure(
                    exc.code,
                    exc.user_message,
                    stage=current_stage,
                    exception_type=type(exc.__cause__ or exc).__name__,
                ) from exc
            except Exception as exc:
                raise QueueJobFailure(
                    error_code_for_stage(current_stage),
                    safe_message_for_stage(current_stage),
                    stage=current_stage,
                    exception_type=type(exc).__name__,
                ) from exc
        add_event(
            repository,
            job.id,
            "queue_job_completed",
            "Queue job completed.",
            {
                "platform": platform,
                "resolver": str(extract_result.get("resolver") or "ytdlp_stream"),
                "elapsed_ms": int((perf_counter() - started_at) * 1000),
            },
        )
    except QueueJobFailure as exc:
        record_queue_failure(
            repository,
            job.id,
            exc.code,
            exc.message,
            stage=exc.stage,
            exception_type=exc.exception_type,
        )
    except Exception as exc:
        logger.error(
            "queue_job_failed",
            extra={"media_job_id": str(job.id), "exception_type": type(exc).__name__},
        )
        record_queue_failure(
            repository,
            job.id,
            error_code_for_stage(current_stage),
            safe_message_for_stage(current_stage),
            stage=current_stage,
            exception_type=type(exc).__name__,
        )
    finally:
        if video_buffer is not None:
            video_buffer.close()
        deleted = delete_temp_media(temp_file_path)
        if deleted:
            add_event(
                repository,
                job.id,
                "temp_video_deleted",
                "Temporary video file deleted.",
                {"stage": "queue_cleanup"},
            )
        extract_result = None


def save_metadata(
    repository: MediaJobRepository,
    media_job_id: UUID,
    result: dict[str, object],
    uploaded_thumbnail_url: str | None,
) -> None:
    cdn_url = str_or_none(result.get("cdn_download_url"))
    try:
        repository.upsert_private_metadata(
            media_job_id,
            platform=str(result.get("platform") or ""),
            resolver=str(result.get("resolver") or "ytdlp_stream"),
            cdn_url=cdn_url,
            raw_metadata=result.get("raw_metadata") if isinstance(result.get("raw_metadata"), dict) else None,
        )
        add_event(
            repository,
            media_job_id,
            "private_metadata_saved",
            "Private source metadata saved.",
            safe_extract_event_metadata(result),
        )
    except Exception as exc:
        metadata = safe_failure_metadata(
            stage="private_metadata_save",
            error_code="private_metadata_save_failed",
            message="Private source metadata could not be saved.",
            exception_type=type(exc).__name__,
        )
        add_event_safely(
            repository,
            media_job_id,
            "private_metadata_save_failed",
            "Private source metadata save failed.",
            metadata,
        )
        raise QueueJobFailure(
            "private_metadata_save_failed",
            "Private source metadata could not be saved.",
            stage="private_metadata_save",
            exception_type=type(exc).__name__,
        ) from exc
    try:
        repository.update_source_metadata(
            media_job_id,
            public_source_metadata(result, uploaded_thumbnail_url),
        )
        add_event(
            repository,
            media_job_id,
            "source_metadata_updated",
            "Source metadata updated.",
            safe_extract_event_metadata(result),
        )
    except Exception as exc:
        metadata = safe_failure_metadata(
            stage="source_metadata_update",
            error_code="source_metadata_update_failed",
            message="Source metadata could not be saved.",
            exception_type=type(exc).__name__,
        )
        add_event_safely(
            repository,
            media_job_id,
            "source_metadata_update_failed",
            "Source metadata update failed.",
            metadata,
        )
        raise QueueJobFailure(
            "source_metadata_update_failed",
            "Source metadata could not be saved.",
            stage="source_metadata_update",
            exception_type=type(exc).__name__,
        ) from exc


def public_source_metadata(
    result: dict[str, object],
    uploaded_thumbnail_url: str | None,
) -> dict[str, object]:
    has_cdn_url = bool(result.get("cdn_download_url"))
    has_local_media = bool(result.get("temp_file_path") or result.get("video_buffer"))
    resolution_mode = str(result.get("resolution_mode") or (
        "local_file_ready" if has_local_media else "metadata_only"
    ))
    return {
        "platform": result.get("platform"),
        "resolver": result.get("resolver") or "ytdlp_stream",
        "title": result.get("title"),
        "description": result.get("description"),
        "author_username": result.get("author_username"),
        "duration_seconds": result.get("duration_seconds"),
        "thumbnail_source_present": bool(result.get("thumbnail_url")),
        "uploaded_thumbnail_url": uploaded_thumbnail_url,
        "resolution_mode": resolution_mode,
        "media": {
            "has_cdn_url": has_cdn_url,
            "cdn_url_stored_private": has_cdn_url,
            "mime_type": result.get("mime_type"),
            "size_bytes": result.get("size_bytes"),
            "width": result.get("width"),
            "height": result.get("height"),
            "video_bytes_verified": bool(result.get("video_bytes_verified")),
            "output_mode": result.get("output_mode"),
            "cookie_mode": result.get("cookie_mode"),
        },
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }


def update_job_source_fields(
    repository: MediaJobRepository,
    media_job_id: UUID,
    result: dict[str, object],
    uploaded_thumbnail_url: str | None,
) -> None:
    values = {
        "media_duration_seconds": result.get("duration_seconds"),
        "media_size_bytes": result.get("size_bytes"),
        "source_title": result.get("title"),
        "source_thumbnail_url": result.get("thumbnail_url"),
    }
    try:
        repository.update_job_metadata_best_effort(
            media_job_id,
            {key: value for key, value in values.items() if value is not None},
        )
        if uploaded_thumbnail_url:
            repository.update_job_metadata_best_effort(
                media_job_id,
                {"thumbnail_url": uploaded_thumbnail_url},
            )
    except Exception:
        pass


def upload_thumbnail_best_effort(
    repository: MediaJobRepository,
    settings: Settings,
    job: MediaJobRecord,
    thumbnail_url: str | None,
) -> str | None:
    if not thumbnail_url:
        return None
    try:
        public_url = upload_source_thumbnail(
            thumbnail_url,
            user_id=job.user_id,
            media_job_id=job.id,
            repository=repository,
            settings=settings,
        )
        if public_url:
            add_event(
                repository,
                job.id,
                "thumbnail_uploaded",
                "Thumbnail uploaded.",
                {"thumbnail_uploaded": True},
            )
            if job.recipe_item_id is not None:
                repository.update_recipe_thumbnail(
                    recipe_item_id=job.recipe_item_id,
                    user_id=job.user_id,
                    thumbnail_url=public_url,
                )
        return public_url
    except Exception:
        return None


def run_gemini_video_poc_analysis(
    *,
    repository: MediaJobRepository,
    job: MediaJobRecord,
    settings: Settings,
    extract_result: dict[str, object],
    platform: str,
    uploaded_thumbnail_url: str | None,
) -> MediaJobRecord:
    temp_file_path = str_or_none(extract_result.get("temp_file_path"))
    if (
        not extract_result.get("video_bytes_verified")
        or temp_file_path is None
        or not Path(temp_file_path).exists()
    ):
        raise QueueJobFailure(
            "video_required_for_ai",
            "Video could not be prepared for AI analysis.",
            stage="gemini_video_analysis",
        )
    if settings.gemini_api_key is None:
        raise QueueJobFailure(
            "gemini_video_analysis_failed",
            "Gemini video analysis is not configured.",
            stage="gemini_video_analysis",
        )

    model = settings.gemini_video_model or "gemini-2.5-flash"
    resolver = str(extract_result.get("resolver") or "ytdlp_stream")
    size_bytes = safe_int_or_none(extract_result.get("size_bytes"))
    duration_seconds = safe_int_or_none(extract_result.get("duration_seconds"))
    mime_type = str_or_none(extract_result.get("mime_type")) or "video/mp4"
    api_key = settings.gemini_api_key.get_secret_value()
    file_name: str | None = None

    job = repository.update_job(
        job.id,
        status="analyzing",
        progress=60,
        thumbnail_url=uploaded_thumbnail_url,
        error_code=None,
        error_message=None,
        ai_provider="gemini",
        ai_model=model,
    )
    repository.insert_event(
        job.id,
        event_type="gemini_video_analysis_started",
        message="Gemini video analysis started.",
        metadata=safe_ai_event_metadata(
            platform=platform,
            resolver=resolver,
            model=model,
            duration_seconds=duration_seconds,
            size_bytes=size_bytes,
            video_bytes_verified=True,
            stage="gemini_video_analysis",
        ),
    )
    try:
        file_uri, file_name = _gemini_upload_video_rest(
            api_key=api_key,
            file_path=temp_file_path,
            mime_type=mime_type,
            timeout_seconds=settings.gemini_files_upload_timeout_seconds,
        )
        parsed = _gemini_video_to_recipe_rest(
            api_key=api_key,
            model=model,
            file_uri=file_uri,
            mime_type=mime_type,
            prompt=VIDEO_RECIPE_PROMPT,
            timeout_seconds=settings.social_video_gemini_timeout_seconds,
        )
        _validate_parsed_recipe(
            parsed,
            platform=platform,
            stage="gemini_video_analysis",
            model=model,
            user_message="Gemini did not return a usable recipe.",
        )
        result = _caption_result_to_recipe_result(parsed)
    except ValueError as exc:
        repository.insert_event(
            job.id,
            event_type="gemini_video_analysis_failed",
            message="Gemini video analysis failed.",
            metadata=safe_ai_event_metadata(
                platform=platform,
                resolver=resolver,
                model=model,
                duration_seconds=duration_seconds,
                size_bytes=size_bytes,
                video_bytes_verified=True,
                stage="gemini_video_analysis",
                error_code="gemini_recipe_json_invalid",
                exception_type=type(exc).__name__,
            ),
        )
        raise QueueJobFailure(
            "gemini_recipe_json_invalid",
            "Gemini returned an invalid recipe response.",
            stage="gemini_video_analysis",
            exception_type=type(exc).__name__,
        ) from exc
    except PipelineFailure as exc:
        repository.insert_event(
            job.id,
            event_type="gemini_video_analysis_failed",
            message="Gemini video analysis failed.",
            metadata=safe_ai_event_metadata(
                platform=platform,
                resolver=resolver,
                model=model,
                duration_seconds=duration_seconds,
                size_bytes=size_bytes,
                video_bytes_verified=True,
                stage="gemini_video_analysis",
                error_code="gemini_recipe_json_invalid",
                exception_type=type(exc).__name__,
            ),
        )
        raise QueueJobFailure(
            "gemini_recipe_json_invalid",
            "Gemini returned an invalid recipe response.",
            stage="gemini_video_analysis",
            exception_type=type(exc).__name__,
        ) from exc
    except Exception as exc:
        repository.insert_event(
            job.id,
            event_type="gemini_video_analysis_failed",
            message="Gemini video analysis failed.",
            metadata=safe_ai_event_metadata(
                platform=platform,
                resolver=resolver,
                model=model,
                duration_seconds=duration_seconds,
                size_bytes=size_bytes,
                video_bytes_verified=True,
                stage="gemini_video_analysis",
                error_code="gemini_video_analysis_failed",
                exception_type=type(exc).__name__,
            ),
        )
        if settings.real_ai_fallback_to_mock:
            return save_mock_recipe_card_for_job(
                repository=repository,
                job=job,
                settings=settings,
                platform=platform,
                resolver=resolver,
                thumbnail_url=uploaded_thumbnail_url,
                job_started_at=perf_counter(),
                fixture_error_code="mock_fixture_failed",
            )
        raise QueueJobFailure(
            "gemini_video_analysis_failed",
            "Gemini video analysis failed.",
            stage="gemini_video_analysis",
            exception_type=type(exc).__name__,
        ) from exc
    finally:
        if file_name:
            _gemini_delete_file_rest(
                api_key=api_key,
                file_name=file_name,
                timeout_seconds=settings.gemini_files_upload_timeout_seconds,
            )

    repository.insert_event(
        job.id,
        event_type="gemini_video_analysis_completed",
        message="Gemini video analysis completed.",
        metadata=safe_ai_event_metadata(
            platform=platform,
            resolver=resolver,
            model=model,
            duration_seconds=duration_seconds,
            size_bytes=size_bytes,
            video_bytes_verified=True,
            stage="gemini_video_analysis",
        ),
    )
    return save_gemini_recipe_card(
        repository=repository,
        job=job,
        result=result,
        model=model,
        platform=platform,
        resolver=resolver,
        uploaded_thumbnail_url=uploaded_thumbnail_url,
    )


def save_gemini_recipe_card(
    *,
    repository: MediaJobRepository,
    job: MediaJobRecord,
    result: GeminiRecipeResult,
    model: str,
    platform: str,
    resolver: str,
    uploaded_thumbnail_url: str | None,
) -> MediaJobRecord:
    job = repository.update_job(
        job.id,
        status="analyzing",
        progress=85,
        thumbnail_url=uploaded_thumbnail_url,
        error_code=None,
        error_message=None,
        ai_provider="gemini",
        ai_model=model,
    )
    repository.insert_event(
        job.id,
        event_type="recipe_card_saving_started",
        message="Recipe card save started.",
        metadata={"platform": platform, "resolver": resolver, "model": model, "stage": "recipe_card_saving"},
    )
    try:
        recipe_card_id = repository.insert_recipe_card(
            job=job,
            result=result,
            model=model,
        )
    except Exception as exc:
        raise QueueJobFailure(
            "recipe_card_insert_failed",
            "Recipe card could not be saved.",
            stage="recipe_card_saving",
            exception_type=type(exc).__name__,
        ) from exc
    repository.insert_event(
        job.id,
        event_type="recipe_card_saved",
        message="Recipe card saved.",
        metadata={"platform": platform, "resolver": resolver, "model": model, "stage": "recipe_card_saving"},
    )
    return repository.update_job(
        job.id,
        status="completed",
        progress=100,
        result_recipe_card_id=recipe_card_id,
        ai_provider="gemini",
        ai_model=model,
        thumbnail_url=uploaded_thumbnail_url,
        completed_at=datetime.now(timezone.utc).isoformat(),
        error_code=None,
        error_message=None,
    )


def safe_extract_event_metadata(
    result: dict[str, object],
    *,
    error_code: str | None = None,
) -> dict[str, object]:
    metadata = {
        "platform": result.get("platform"),
        "resolver": result.get("resolver") or "ytdlp_stream",
        "title_present": bool(result.get("title")),
        "description_present": bool(result.get("description")),
        "duration_seconds": result.get("duration_seconds"),
        "has_thumbnail": bool(result.get("thumbnail_url")),
        "has_cdn_url": bool(result.get("cdn_download_url")),
        "mime_type": result.get("mime_type"),
        "size_bytes": result.get("size_bytes"),
        "width": result.get("width"),
        "height": result.get("height"),
        "resolution_mode": result.get("resolution_mode"),
        "video_bytes_verified": result.get("video_bytes_verified"),
        "output_mode": result.get("output_mode"),
        "cookie_mode": result.get("cookie_mode"),
    }
    if error_code is not None:
        metadata["error_code"] = error_code
    return {key: value for key, value in metadata.items() if value is not None}


def safe_ai_event_metadata(
    *,
    platform: str,
    resolver: str,
    model: str,
    duration_seconds: int | None,
    size_bytes: int | None,
    video_bytes_verified: bool,
    stage: str,
    error_code: str | None = None,
    exception_type: str | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "platform": platform,
        "resolver": resolver,
        "model": model,
        "video_bytes_verified": video_bytes_verified,
        "stage": stage,
    }
    if duration_seconds is not None:
        metadata["duration_seconds"] = duration_seconds
    if size_bytes is not None:
        metadata["size_bytes"] = size_bytes
    if error_code is not None:
        metadata["error_code"] = error_code
    if exception_type is not None:
        metadata["exception_type"] = exception_type
    return metadata


def emit_extractor_progress_events(
    repository: MediaJobRepository,
    media_job_id: UUID,
    result: dict[str, object],
) -> None:
    platform = str(result.get("platform") or "")
    if result.get("cookie_setup_failed"):
        add_event_safely(
            repository,
            media_job_id,
            "cookie_setup_failed",
            "Cookie setup failed.",
            {"platform": platform},
        )
    if result.get("cookie_retry_attempted"):
        add_event_safely(
            repository,
            media_job_id,
            "unified_extractor_retry_with_cookies",
            "Unified yt-dlp extractor retrying with platform cookies.",
            {
                "platform": platform,
                "resolver": "ytdlp_stream_cookies",
                "cookie_mode": "platform_cookie_file",
            },
        )
    if result.get("metadata_fallback_attempted"):
        add_event_safely(
            repository,
            media_job_id,
            "metadata_fallback_started",
            "Metadata-only fallback started.",
            {"platform": platform},
        )
        add_event_safely(
            repository,
            media_job_id,
            "metadata_fallback_completed" if result.get("success") else "metadata_fallback_failed",
            (
                "Metadata-only fallback completed."
                if result.get("success")
                else "Metadata-only fallback failed."
            ),
            {
                "platform": platform,
                "resolver": result.get("resolver") or f"{platform}_opengraph",
                "title_present": bool(result.get("title")),
                "has_thumbnail": bool(result.get("thumbnail_url")),
                "error_code": result.get("error_code"),
            },
        )
    if result.get("video_bytes_verified"):
        add_event_safely(
            repository,
            media_job_id,
            "unified_extractor_video_verified",
            "Unified yt-dlp extractor verified downloaded video bytes.",
            {
                "platform": platform,
                "resolver": result.get("resolver") or "ytdlp_stream",
                "output_mode": result.get("output_mode"),
                "size_bytes": result.get("size_bytes"),
                "duration_seconds": result.get("duration_seconds"),
                "video_bytes_verified": True,
                "cookie_mode": result.get("cookie_mode") or "none",
            },
        )


def record_queue_failure(
    repository: MediaJobRepository,
    media_job_id: UUID,
    error_code: str,
    message: str,
    *,
    stage: str,
    exception_type: str | None = None,
) -> None:
    repository.update_job(
        media_job_id,
        status="failed",
        progress=100,
        error_code=error_code,
        error_message=message,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )
    add_event(
        repository,
        media_job_id,
        "queue_job_failed",
        "Queue job failed.",
        safe_failure_metadata(
            stage=stage,
            error_code=error_code,
            message=message,
            exception_type=exception_type,
        ),
    )


def error_code_for_stage(stage: str) -> str:
    if stage == "unified_extractor":
        return "unified_extractor_failed"
    if stage == "private_metadata_save":
        return "private_metadata_save_failed"
    if stage in {"source_metadata_update", "source_metadata_backfill", "metadata_save"}:
        return "source_metadata_update_failed"
    if stage == "recipe_card_saving":
        return "recipe_card_insert_failed"
    if stage == "job_completion_update":
        return "job_completion_update_failed"
    if stage == "mock_fixture_analysis":
        return "mock_fixture_failed"
    if stage == "metadata_fallback":
        return "metadata_unavailable"
    if stage == "gemini_video_analysis":
        return "gemini_video_analysis_failed"
    return "queue_job_failed"


def safe_message_for_stage(stage: str) -> str:
    if stage == "recipe_card_saving":
        return "Recipe card could not be saved."
    if stage == "job_completion_update":
        return "Recipe card was saved, but the job could not be marked completed."
    if stage in {"private_metadata_save", "source_metadata_update", "metadata_save"}:
        return "Source metadata could not be saved."
    if stage == "source_metadata_backfill":
        return "Optional source metadata backfill failed."
    if stage == "mock_fixture_analysis":
        return "Mock recipe fixture could not be loaded."
    if stage == "unified_extractor":
        return "This social video could not be processed."
    if stage == "metadata_fallback":
        return "This social video could not be accessed publicly."
    if stage == "gemini_video_analysis":
        return "Gemini video analysis failed."
    return "Queue worker failed while processing this recipe."


def safe_failure_metadata(
    *,
    stage: str,
    error_code: str,
    message: str,
    exception_type: str | None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "stage": stage,
        "error_code": error_code,
        "safe_error_message": message,
    }
    if exception_type:
        metadata["exception_type"] = exception_type
    return metadata


def delete_temp_media(temp_file_path: str | None) -> bool:
    if not temp_file_path:
        return False
    path = Path(temp_file_path)
    deleted = False
    try:
        if path.exists():
            path.unlink()
            deleted = True
    except OSError:
        pass
    parent = path.parent
    try:
        if parent.exists() and parent.name:
            shutil.rmtree(parent, ignore_errors=True)
            deleted = True
    except OSError:
        pass
    return deleted


def add_event(
    repository: MediaJobRepository,
    media_job_id: UUID,
    event_type: str,
    message: str,
    metadata: dict[str, object],
) -> None:
    repository.add_event(
        media_job_id,
        event_type=event_type,
        message=message,
        metadata=metadata,
    )


def add_event_safely(
    repository: MediaJobRepository,
    media_job_id: UUID,
    event_type: str,
    message: str,
    metadata: dict[str, object],
) -> None:
    try:
        add_event(
            repository,
            media_job_id,
            event_type,
            message,
            metadata,
        )
    except Exception:
        pass


def str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def safe_int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
