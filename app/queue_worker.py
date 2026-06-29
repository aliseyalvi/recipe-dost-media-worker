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
    _caption_result_to_recipe_result,
    _load_mock_recipe_fixture,
    _validate_parsed_recipe,
    platform_for_job,
)
from app.schemas import MediaJobRecord
from app.social.stream_download import extract_social_video
from app.social.thumbnail_upload import upload_source_thumbnail
from app.supabase_client import MediaJobRepository, create_repository

logger = logging.getLogger("recipe_dost.worker")


class QueueJobFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


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
            )

        platform = platform_for_job(job)
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
        if not extract_result.get("success"):
            error_code = str(extract_result.get("error_code") or "unified_extractor_failed")
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
        save_metadata(repository, job.id, extract_result, uploaded_thumbnail_url)
        update_job_source_fields(repository, job.id, extract_result, uploaded_thumbnail_url)

        job = repository.update_job(
            job.id,
            status="analyzing",
            progress=60,
            thumbnail_url=uploaded_thumbnail_url,
            error_code=None,
            error_message=None,
            ai_provider="mock",
            ai_model="mock-recipe-fixture",
        )
        add_event(
            repository,
            job.id,
            "mock_ai_analysis_started",
            "Mock fixture AI analysis started.",
            {"platform": platform, "resolver": "ytdlp_stream"},
        )
        try:
            parsed = _load_mock_recipe_fixture(settings.mock_recipe_fixture_path)
            _validate_parsed_recipe(
                parsed,
                platform=platform,
                stage="mock_fixture_analysis",
                model="mock-recipe-fixture",
                user_message="Mock recipe fixture does not include enough recipe details.",
            )
            result = _caption_result_to_recipe_result(parsed)
        except Exception as exc:
            raise QueueJobFailure(
                "mock_fixture_failed",
                "Mock recipe fixture could not be loaded.",
            ) from exc
        add_event(
            repository,
            job.id,
            "mock_ai_analysis_completed",
            "Mock fixture AI analysis completed.",
            {"platform": platform, "resolver": "ytdlp_stream"},
        )

        job = repository.update_job(
            job.id,
            status="analyzing",
            progress=85,
            thumbnail_url=uploaded_thumbnail_url,
            error_code=None,
            error_message=None,
        )
        try:
            recipe_card_id = repository.insert_recipe_card(
                job=job,
                result=result,
                model="mock-recipe-fixture",
            )
        except Exception as exc:
            raise QueueJobFailure(
                "recipe_card_insert_failed",
                "Recipe card could not be saved.",
            ) from exc
        add_event(
            repository,
            job.id,
            "recipe_card_saved",
            "Recipe card saved.",
            {"stage": "recipe_card_saving"},
        )
        repository.update_job(
            job.id,
            status="completed",
            progress=100,
            result_recipe_card_id=recipe_card_id,
            ai_provider="mock",
            ai_model="mock-recipe-fixture",
            thumbnail_url=uploaded_thumbnail_url,
            completed_at=datetime.now(timezone.utc).isoformat(),
            error_code=None,
            error_message=None,
        )
        add_event(
            repository,
            job.id,
            "queue_job_completed",
            "Queue job completed.",
            {
                "platform": platform,
                "resolver": "ytdlp_stream",
                "elapsed_ms": int((perf_counter() - started_at) * 1000),
            },
        )
    except QueueJobFailure as exc:
        record_queue_failure(repository, job.id, exc.code, exc.message)
    except Exception as exc:
        logger.error(
            "queue_job_failed",
            extra={"media_job_id": str(job.id), "exception_type": type(exc).__name__},
        )
        record_queue_failure(
            repository,
            job.id,
            "unified_extractor_failed",
            "Queue worker failed while processing this recipe.",
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
    repository.upsert_private_metadata(
        media_job_id,
        platform=str(result.get("platform") or ""),
        resolver="ytdlp_stream",
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


def public_source_metadata(
    result: dict[str, object],
    uploaded_thumbnail_url: str | None,
) -> dict[str, object]:
    has_cdn_url = bool(result.get("cdn_download_url"))
    has_local_media = bool(result.get("temp_file_path") or result.get("video_buffer"))
    return {
        "platform": result.get("platform"),
        "resolver": "ytdlp_stream",
        "title": result.get("title"),
        "description": result.get("description"),
        "author_username": result.get("author_username"),
        "duration_seconds": result.get("duration_seconds"),
        "thumbnail_source_present": bool(result.get("thumbnail_url")),
        "uploaded_thumbnail_url": uploaded_thumbnail_url,
        "resolution_mode": "local_file_ready" if has_local_media else "metadata_only",
        "media": {
            "has_cdn_url": has_cdn_url,
            "cdn_url_stored_private": has_cdn_url,
            "mime_type": result.get("mime_type"),
            "size_bytes": result.get("size_bytes"),
            "width": result.get("width"),
            "height": result.get("height"),
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
    repository.update_job_metadata_best_effort(
        media_job_id,
        {key: value for key, value in values.items() if value is not None},
    )
    if uploaded_thumbnail_url:
        repository.update_job_metadata_best_effort(
            media_job_id,
            {"thumbnail_url": uploaded_thumbnail_url},
        )


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


def safe_extract_event_metadata(
    result: dict[str, object],
    *,
    error_code: str | None = None,
) -> dict[str, object]:
    metadata = {
        "platform": result.get("platform"),
        "resolver": "ytdlp_stream",
        "title_present": bool(result.get("title")),
        "description_present": bool(result.get("description")),
        "duration_seconds": result.get("duration_seconds"),
        "has_thumbnail": bool(result.get("thumbnail_url")),
        "has_cdn_url": bool(result.get("cdn_download_url")),
        "mime_type": result.get("mime_type"),
        "size_bytes": result.get("size_bytes"),
        "width": result.get("width"),
        "height": result.get("height"),
    }
    if error_code is not None:
        metadata["error_code"] = error_code
    return {key: value for key, value in metadata.items() if value is not None}


def record_queue_failure(
    repository: MediaJobRepository,
    media_job_id: UUID,
    error_code: str,
    message: str,
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
        {"error_code": error_code},
    )


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


def str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
