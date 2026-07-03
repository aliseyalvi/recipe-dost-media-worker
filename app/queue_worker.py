from __future__ import annotations

import asyncio
import io
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable
from urllib.parse import quote
from uuid import UUID

import requests

from app.config import Settings, get_settings
from app.job_runner import (
    PipelineFailure,
    _caption_result_to_recipe_result,
    _extract_json_object,
    _validate_parsed_recipe,
    platform_for_job,
    save_mock_recipe_card_for_job,
)
from app.schemas import (
    GeminiRecipeResult,
    GroceryItem,
    MediaJobRecord,
    RecipeIngredient,
    RecipeStep,
)
from app.social.stream_download import extract_social_video
from app.social.thumbnail_selection import (
    early_frame_thumbnail_selected,
    platform_thumbnail,
    should_update_thumbnail,
)
from app.social.thumbnail_upload import (
    sample_video_frame_thumbnail,
    upload_source_thumbnail,
)
from app.supabase_client import MediaJobRepository, create_repository

logger = logging.getLogger("recipe_dost.worker")

PROMPT_VERSION = "m20_gemini31_single_pass_v1"
TWO_STEP_PROMPT_VERSION = "m20_recipe_video_v1"

CANONICAL_RECIPE_PROMPT = """Perform exhaustive line-by-line recipe extraction.
Do not summarize. Explicitly list every ingredient, including minor spices.
Extract every cooking step in granular order. Use captions, labels, visible text,
narration, and visual sequence. Return English and Urdu fields.

Return strict JSON only with exactly these keys:
title_en, title_ur, summary_en, summary_ur, ingredients, steps, grocery_list,
time, servings, difficulty, cuisine_en, cuisine_ur, tags, missing_info, confidence.

Use this shape:
{
  "title_en": "string",
  "title_ur": "string",
  "summary_en": "string",
  "summary_ur": "string",
  "ingredients": [{"name_en":"string","name_ur":"string","quantity":"string","notes_en":"string","notes_ur":"string"}],
  "steps": [{"step_number":1,"instruction_en":"string","instruction_ur":"string","tip_en":"string","tip_ur":"string"}],
  "grocery_list": [{"name_en":"string","name_ur":"string","quantity":"string"}],
  "time": {"prep_minutes": null, "cook_minutes": null, "total_minutes": null},
  "servings": null,
  "difficulty": null,
  "cuisine_en": "string",
  "cuisine_ur": "string",
  "tags": ["string"],
  "missing_info": ["string"],
  "confidence": "low"
}

If unknown, use null or empty arrays/strings. Do not omit keys.
Do not include markdown."""

CANONICAL_REPAIR_PROMPT = """Repair this invalid canonical recipe JSON.
Use only the safe source title/platform, validation errors, and invalid JSON below.
Do not use video, URLs, hidden data, or external facts. Return corrected strict
canonical JSON only, with the same keys requested for extraction."""

CANONICAL_RECIPE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "title_en": {"type": "string"},
        "title_ur": {"type": "string"},
        "summary_en": {"type": "string"},
        "summary_ur": {"type": "string"},
        "ingredients": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name_en": {"type": "string"},
                    "name_ur": {"type": "string"},
                    "quantity": {"type": "string"},
                    "notes_en": {"type": "string"},
                    "notes_ur": {"type": "string"},
                },
            },
        },
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step_number": {"type": "integer"},
                    "instruction_en": {"type": "string"},
                    "instruction_ur": {"type": "string"},
                    "tip_en": {"type": "string"},
                    "tip_ur": {"type": "string"},
                },
            },
        },
        "grocery_list": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name_en": {"type": "string"},
                    "name_ur": {"type": "string"},
                    "quantity": {"type": "string"},
                },
            },
        },
        "time": {"type": "object"},
        "servings": {},
        "difficulty": {},
        "cuisine_en": {"type": "string"},
        "cuisine_ur": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "missing_info": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string"},
    },
}

VIDEO_BRIEF_PROMPT = """Extract a detailed, sectioned recipe brief from this video.
Use visible labels, narration, captions, and the inferred cooking sequence.
Make a best-effort recipe extraction. Do not return a tiny summary.

Use exactly these plain-text sections:
TITLE
SUMMARY
INGREDIENTS
STEPS
GROCERY_LIST
TIME_AND_SERVINGS
CUISINE_STYLE
CONFIDENCE
MISSING_OR_UNCERTAIN_DETAILS"""

VIDEO_BRIEF_RETRY_PROMPT = """Extract full recipe details from this video.
The previous brief was too thin. Re-watch/reason from visible labels, narration,
captions, ingredient shots, quantities, and cooking sequence.

Return detailed plain text with these sections:
TITLE
SUMMARY
INGREDIENTS
STEPS
GROCERY_LIST
TIME_AND_SERVINGS
CUISINE_STYLE
CONFIDENCE
MISSING_OR_UNCERTAIN_DETAILS"""

RECIPE_JSON_PROMPT = """Convert this recipe brief into strict JSON only.
Required keys: title, summary, ingredients, steps, prep_time_minutes, cook_time_minutes,
servings, difficulty, cuisine, tags. Use null when unknown and arrays for ingredients,
steps, grocery_list, tags, missing_info, and uncertainties.

Ingredients may be strings or objects with name, quantity, notes, name_en, name_ur,
notes_en, and notes_ur. Steps may be strings or objects with instruction,
instruction_en, instruction_ur, tip, and tip_ur.
Write Urdu fields in Urdu script when possible. Do not invent unsupported details.

Brief:
"""

RECIPE_REPAIR_PROMPT = """Repair this recipe extraction into strict JSON only.
Use only the supplied recipe brief and source title. Do not use video, URLs, or hidden data.

Required keys:
title, summary, ingredients, steps, grocery_list, prep_time_minutes,
cook_time_minutes, servings, difficulty, cuisine, tags, missing_info, confidence.

Ingredient objects must include:
name, quantity, notes, name_en, name_ur, notes_en, notes_ur.

Step objects must include:
step_number, instruction, instruction_en, instruction_ur, tip, tip_en, tip_ur.

Write Urdu fields in Urdu script when possible. Use null when unknown.
Return JSON only."""

URDU_TRANSLATION_PROMPT = """Translate only these final recipe fields into Urdu script.
Return strict JSON only with keys ingredients, steps, grocery_list.
Keep array lengths and order exactly the same. Do not add recipe details."""


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


def should_use_youtube_gemini_url(
    job: MediaJobRecord,
    settings: Settings,
    platform: str,
) -> bool:
    return (
        platform == "youtube"
        and settings.ai_analysis_mode == "gemini_video_poc"
        and settings.youtube_ai_input_mode == "gemini_url"
    )


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

        platform = platform_for_job(job)
        if should_use_youtube_gemini_url(job, settings, platform):
            current_stage = "youtube_gemini_url"
            job = process_youtube_gemini_url_job(
                repository=repository,
                settings=settings,
                job=job,
                platform=platform,
                started_at=started_at,
            )
            add_event(
                repository,
                job.id,
                "queue_job_completed",
                "Queue job completed.",
                {
                    "platform": platform,
                    "resolver": "youtube_oembed",
                    "elapsed_ms": int((perf_counter() - started_at) * 1000),
                },
            )
            return

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
            str_or_none(extract_result.get("thumbnail_source")),
            safe_int_or_none(extract_result.get("duration_seconds")),
        )
        if uploaded_thumbnail_url is None and not extract_result.get("thumbnail_url"):
            uploaded_thumbnail_url = sample_thumbnail_best_effort(
                repository,
                settings,
                job,
                str_or_none(extract_result.get("temp_file_path")),
                safe_int_or_none(extract_result.get("duration_seconds")),
            )
            if uploaded_thumbnail_url:
                frame_thumbnail = early_frame_thumbnail_selected(platform)
                extract_result["thumbnail_source"] = frame_thumbnail.source
                extract_result["thumbnail_provider"] = frame_thumbnail.provider
                extract_result["thumbnail_strategy"] = frame_thumbnail.strategy
                extract_result["thumbnail_candidate_count"] = frame_thumbnail.candidate_count
                extract_result["thumbnail_selected"] = True
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
        if get_settings().allow_private_metadata_failure:
            return
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
        "thumbnail_source": result.get("thumbnail_source") or "placeholder",
        "thumbnail_provider": result.get("thumbnail_provider") or result.get("platform"),
        "thumbnail_strategy": result.get("thumbnail_strategy") or "platform_first_v2",
        "thumbnail_candidate_count": result.get("thumbnail_candidate_count") or 0,
        "thumbnail_selected": bool(result.get("thumbnail_selected")),
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
    thumbnail_source: str | None = None,
    duration_seconds: int | None = None,
) -> str | None:
    event_metadata = {
        "platform": job.platform,
        "thumbnail_source": thumbnail_source or "placeholder",
        "thumbnail_provider": job.platform,
        "thumbnail_strategy": "platform_first_v2",
        "candidate_count": 1 if thumbnail_url else 0,
        "thumbnail_selected": bool(thumbnail_url),
    }
    if duration_seconds is not None:
        event_metadata["duration_seconds"] = duration_seconds
    if not thumbnail_url:
        add_event_safely(
            repository,
            job.id,
            "thumbnail_selection_skipped",
            "No source thumbnail selected.",
            {**event_metadata, "thumbnail_selected": False},
        )
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
                {**event_metadata, "thumbnail_selected": True},
            )
            if job.recipe_item_id is not None:
                update_recipe_thumbnail_no_downgrade(
                    repository,
                    job,
                    public_url,
                    thumbnail_source or "placeholder",
                    event_metadata,
                )
        return public_url
    except Exception:
        add_event_safely(
            repository,
            job.id,
            "thumbnail_upload_failed",
            "Thumbnail upload failed.",
            {**event_metadata, "thumbnail_selected": False},
        )
        return None


def sample_thumbnail_best_effort(
    repository: MediaJobRepository,
    settings: Settings,
    job: MediaJobRecord,
    temp_file_path: str | None,
    duration_seconds: int | None,
) -> str | None:
    event_metadata: dict[str, object] = {
        "platform": job.platform,
        "thumbnail_source": "early_frame_fallback",
        "thumbnail_provider": job.platform,
        "thumbnail_strategy": "platform_first_v2",
        "candidate_count": 4,
        "thumbnail_selected": False,
    }
    if duration_seconds is not None:
        event_metadata["duration_seconds"] = duration_seconds
    try:
        public_url = sample_video_frame_thumbnail(
            temp_file_path,
            duration_seconds=duration_seconds,
            user_id=job.user_id,
            media_job_id=job.id,
            repository=repository,
            settings=settings,
        )
    except Exception:
        return None
    if not public_url:
        return None
    add_event(
        repository,
        job.id,
        "thumbnail_uploaded",
        "Sampled frame thumbnail uploaded.",
        {**event_metadata, "thumbnail_selected": True},
    )
    if job.recipe_item_id is not None:
        update_recipe_thumbnail_no_downgrade(
            repository,
            job,
            public_url,
            "early_frame_fallback",
            event_metadata,
        )
    return public_url


def update_recipe_thumbnail_no_downgrade(
    repository: MediaJobRepository,
    job: MediaJobRecord,
    thumbnail_url: str,
    thumbnail_source: str,
    base_metadata: dict[str, object],
) -> bool:
    if job.recipe_item_id is None:
        return False
    existing_url = repository.get_recipe_thumbnail(
        recipe_item_id=job.recipe_item_id,
        user_id=job.user_id,
    )
    will_update = should_update_thumbnail(
        existing_url=existing_url,
        existing_source=None,
        new_source=thumbnail_source,
    )
    add_event_safely(
        repository,
        job.id,
        "thumbnail_selected",
        "Thumbnail update decision made.",
        {
            **base_metadata,
            "used_existing_thumbnail": bool(existing_url and not will_update),
            "will_update_recipe_item_thumbnail": will_update,
        },
    )
    if not will_update:
        return False
    repository.update_recipe_thumbnail(
        recipe_item_id=job.recipe_item_id,
        user_id=job.user_id,
        thumbnail_url=thumbnail_url,
    )
    return True


def fetch_youtube_oembed(source_url: str) -> dict[str, object]:
    response = requests.get(
        "https://www.youtube.com/oembed"
        f"?url={quote(source_url, safe='')}&format=json",
        timeout=10,
    )
    response.raise_for_status()
    body = response.json()
    return body if isinstance(body, dict) else {}


def youtube_oembed_result(body: dict[str, object]) -> dict[str, object]:
    thumbnail = platform_thumbnail(
        body.get("thumbnail_url"),
        source="youtube_oembed",
        provider="youtube",
    )
    return {
        "success": True,
        "platform": "youtube",
        "resolver": "youtube_oembed",
        "title": body.get("title"),
        "description": body.get("title"),
        "author_username": body.get("author_name"),
        "thumbnail_url": thumbnail.url,
        "thumbnail_source": thumbnail.source,
        "thumbnail_provider": thumbnail.provider,
        "thumbnail_strategy": thumbnail.strategy,
        "thumbnail_candidate_count": thumbnail.candidate_count,
        "thumbnail_selected": thumbnail.selected,
        "resolution_mode": "gemini_url",
        "video_bytes_verified": False,
        "output_mode": "gemini_url",
        "cookie_mode": "none",
    }


def minimal_youtube_result() -> dict[str, object]:
    return {
        "success": True,
        "platform": "youtube",
        "resolver": "youtube_oembed",
        "resolution_mode": "gemini_url",
        "thumbnail_source": "placeholder",
        "thumbnail_provider": "youtube",
        "thumbnail_strategy": "platform_first_v2",
        "thumbnail_candidate_count": 0,
        "thumbnail_selected": False,
        "video_bytes_verified": False,
        "output_mode": "gemini_url",
        "cookie_mode": "none",
    }


def process_youtube_gemini_url_job(
    *,
    repository: MediaJobRepository,
    settings: Settings,
    job: MediaJobRecord,
    platform: str,
    started_at: float,
) -> MediaJobRecord:
    if settings.gemini_api_key is None:
        raise QueueJobFailure(
            "gemini_api_key_missing",
            "Gemini video analysis is not configured.",
            stage="youtube_gemini_url",
        )
    add_event(
        repository,
        job.id,
        "youtube_metadata_started",
        "YouTube metadata lookup started.",
        {"platform": platform, "resolver": "youtube_oembed"},
    )
    try:
        extract_result = youtube_oembed_result(fetch_youtube_oembed(job.source_url))
    except Exception as exc:
        extract_result = minimal_youtube_result()
        extract_result["metadata_error_type"] = type(exc).__name__
    add_event(
        repository,
        job.id,
        "youtube_metadata_completed",
        "YouTube metadata lookup completed.",
        {
            "platform": platform,
            "resolver": "youtube_oembed",
            "title_present": bool(extract_result.get("title")),
            "has_thumbnail": bool(extract_result.get("thumbnail_url")),
            "error_code": (
                "youtube_oembed_failed"
                if extract_result.get("metadata_error_type")
                else None
            ),
            "exception_type": extract_result.get("metadata_error_type"),
        },
    )
    uploaded_thumbnail_url = upload_thumbnail_best_effort(
        repository,
        settings,
        job,
        str_or_none(extract_result.get("thumbnail_url")),
        str_or_none(extract_result.get("thumbnail_source")),
        safe_int_or_none(extract_result.get("duration_seconds")),
    )
    save_metadata(repository, job.id, extract_result, uploaded_thumbnail_url)
    update_job_source_fields(repository, job.id, extract_result, uploaded_thumbnail_url)

    video_model = settings.effective_gemini_video_model
    job = repository.update_job(
        job.id,
        status="analyzing",
        progress=60,
        thumbnail_url=uploaded_thumbnail_url,
        error_code=None,
        error_message=None,
        ai_provider="gemini",
        ai_model=video_model,
    )
    client = create_gemini_client(
        settings.gemini_api_key.get_secret_value(),
        settings.gemini_file_upload_timeout_seconds,
    )
    base_metadata = {
        "platform": platform,
        "resolver": "youtube_oembed",
        "model": video_model,
        "input_mode": "youtube_url",
        "analysis_mode": settings.gemini_video_analysis_mode,
        "stage": "youtube_gemini_url",
        "video_bytes_verified": False,
    }
    if settings.gemini_extraction_mode == "single_pass_structured":
        try:
            from google.genai import types

            uri_kwargs = {"file" + "_uri": job.source_url, "mime_type": "video/mp4"}
            video_part = types.Part.from_uri(**uri_kwargs)
            result, accepted_model, repair_used = run_single_pass_structured_extraction(
                repository=repository,
                job=job,
                settings=settings,
                client=client,
                input_part=video_part,
                input_mode="youtube_url",
                source_metadata=extract_result,
                platform=platform,
            )
        except QueueJobFailure:
            raise
        except Exception as exc:
            raise QueueJobFailure(
                "youtube_gemini_url_failed",
                "Gemini YouTube URL analysis failed.",
                stage="youtube_gemini_url",
                exception_type=type(exc).__name__,
            ) from exc
        add_event(
            repository,
            job.id,
            "youtube_gemini_url_completed",
            "Gemini YouTube URL analysis completed.",
            {
                **base_metadata,
                "model": accepted_model,
                "extraction_mode": settings.gemini_extraction_mode,
                "repair_used": repair_used,
            },
        )
        return save_gemini_recipe_card(
            repository=repository,
            job=job,
            result=result,
            model=accepted_model,
            platform=platform,
            resolver="youtube_oembed",
            uploaded_thumbnail_url=uploaded_thumbnail_url,
            source_metadata=extract_result,
            used_fallback=accepted_model != settings.gemini_primary_model,
            client=client,
            settings=settings,
        )
    add_event(
        repository,
        job.id,
        "youtube_gemini_url_started",
        "Gemini YouTube URL analysis started.",
        base_metadata,
    )
    add_event(
        repository,
        job.id,
        "gemini_video_brief_started",
        "Gemini video brief started.",
        {**base_metadata, "stage": "gemini_video_brief"},
    )
    brief = generate_youtube_url_brief(
        client=client,
        youtube_url=job.source_url,
        model=video_model,
        repository=repository,
        job=job,
        event_metadata={**base_metadata, "stage": "gemini_video_brief"},
    )
    brief_used_fallback = not brief_has_recipe_content(brief, settings)
    add_event(
        repository,
        job.id,
        "youtube_gemini_url_completed",
        "Gemini YouTube URL analysis completed.",
        {**base_metadata, "text_length": len(brief)},
    )
    text_model = settings.effective_gemini_text_model
    json_metadata = {
        **base_metadata,
        "stage": "gemini_recipe_json",
        "model": text_model,
        "text_length": len(brief),
    }
    add_event(
        repository,
        job.id,
        "gemini_recipe_json_started",
        "Gemini recipe JSON started.",
        json_metadata,
    )
    parsed, save_mode = convert_brief_to_recipe_json(
        brief,
        extract_result,
        settings,
        client=client,
        repository=repository,
        job=job,
        event_metadata=json_metadata,
    )
    parsed["brief"] = brief
    parsed["save_mode"] = save_mode
    result = normalize_recipe_card_payload(
        parsed,
        extract_result,
        platform,
        text_model,
        PROMPT_VERSION,
    )
    return save_gemini_recipe_card(
        repository=repository,
        job=job,
        result=result,
        model=text_model,
        platform=platform,
        resolver="youtube_oembed",
        uploaded_thumbnail_url=uploaded_thumbnail_url,
        source_metadata=extract_result,
        used_fallback=save_mode == "local_brief_fallback" or brief_used_fallback,
        brief_text=brief,
        client=client,
        settings=settings,
    )


def create_gemini_client(api_key: str, timeout_seconds: int) -> object:
    from google import genai

    timeout_ms = max(1, timeout_seconds) * 1000
    try:
        return genai.Client(
            api_key=api_key,
            http_options={"timeout": timeout_ms},
        )
    except TypeError:
        return genai.Client(api_key=api_key)


def upload_gemini_video_file(
    *,
    client: object,
    temp_file_path: str,
    mime_type: str,
) -> object:
    files_api = getattr(client, "files")
    try:
        return files_api.upload(
            file=temp_file_path,
            config={"mime_type": mime_type},
        )
    except TypeError:
        try:
            return files_api.upload(
                path=temp_file_path,
                config={"mime_type": mime_type},
            )
        except TypeError:
            return files_api.upload(file=temp_file_path)


def get_gemini_file_name(uploaded_file: object) -> str | None:
    name = getattr(uploaded_file, "name", None)
    return str(name) if name else None


def get_gemini_file_state(uploaded_file: object) -> str:
    state = getattr(uploaded_file, "state", None)
    if hasattr(state, "name"):
        state = getattr(state, "name")
    state_text = str(state or "").upper()
    return state_text.rsplit(".", 1)[-1]


def wait_for_gemini_file_active(
    *,
    client: object,
    uploaded_file: object,
    settings: Settings,
) -> object:
    deadline = perf_counter() + max(1, settings.gemini_file_max_wait_seconds)
    active_file = uploaded_file
    while perf_counter() < deadline:
        state = get_gemini_file_state(active_file)
        if state == "ACTIVE":
            return active_file
        if state in {"FAILED", "ERROR"}:
            raise QueueJobFailure(
                "gemini_file_processing_failed",
                "Gemini video file processing failed.",
                stage="gemini_file_processing",
            )
        name = get_gemini_file_name(active_file)
        if not name:
            raise QueueJobFailure(
                "gemini_file_processing_failed",
                "Gemini video file processing failed.",
                stage="gemini_file_processing",
            )
        sleep_seconds = max(1, settings.gemini_file_poll_interval_seconds)
        if perf_counter() + sleep_seconds > deadline:
            break
        time.sleep(sleep_seconds)
        files_api = getattr(client, "files")
        try:
            active_file = files_api.get(name=name)
        except TypeError:
            active_file = files_api.get(name)
    raise QueueJobFailure(
        "gemini_file_processing_timeout",
        "Gemini video file processing timed out.",
        stage="gemini_file_processing",
    )


def extract_gemini_text(response: object) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    chunks: list[str] = []
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str) and part_text.strip():
                chunks.append(part_text)
    return "\n".join(chunks).strip()


def truncate_brief_for_json(brief_text: str, max_chars: int) -> str:
    text = brief_text.strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cutoff = text.rfind("\n", 0, max_chars)
    if cutoff < max_chars // 2:
        cutoff = max_chars
    return text[:cutoff].rstrip()


def brief_has_recipe_content(brief_text: str, settings: Settings) -> bool:
    text = clean_recipe_text(brief_text).lower()
    if len(text) < max(1, settings.recipe_min_brief_length):
        return False
    sections = parse_brief_sections(brief_text)
    ingredients = sections.get("ingredients") or []
    steps = sections.get("steps") or []
    has_ingredient_words = bool(
        re.search(
            r"\b(ingredient|ingredients|cup|tbsp|tsp|grams?|kg|g|ml|oil|salt|masala|"
            r"onion|garlic|ginger|tomato|flour|rice|chicken|mutton|beef|egg)\b",
            text,
        )
    )
    has_step_words = bool(
        re.search(
            r"\b(add|mix|cook|fry|boil|bake|roast|simmer|stir|chop|slice|serve|"
            r"heat|pour|knead|marinate|blend)\b",
            text,
        )
    )
    return (len(ingredients) >= 1 or has_ingredient_words) and (
        len(steps) >= 2 or has_step_words
    )


def safe_quality_metadata(
    *,
    platform: str,
    model: str,
    result: GeminiRecipeResult | None = None,
    text_length: int | None = None,
    retry_count: int | None = None,
    used_fallback: bool | None = None,
    repair_used: bool | None = None,
    quality_passed: bool | None = None,
    error_code: str | None = None,
    exception_type: str | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {"platform": platform, "model": model}
    if result is not None:
        metadata.update(recipe_quality_stats(result))
    if text_length is not None:
        metadata["text_length"] = text_length
    if retry_count is not None:
        metadata["retry_count"] = retry_count
    if used_fallback is not None:
        metadata["used_fallback"] = used_fallback
    if repair_used is not None:
        metadata["repair_used"] = repair_used
    if quality_passed is not None:
        metadata["quality_passed"] = quality_passed
    if error_code is not None:
        metadata["error_code"] = error_code
    if exception_type is not None:
        metadata["exception_type"] = exception_type
    return metadata


def recipe_quality_stats(result: GeminiRecipeResult) -> dict[str, object]:
    return {
        "ingredients_count": len(result.ingredients),
        "steps_count": len(result.steps),
        "grocery_count": len(result.grocery_list),
        "summary_length": len(clean_recipe_text(result.short_summary_en)),
        "urdu_fields_present": recipe_urdu_fields_present(result),
    }


def recipe_urdu_fields_present(result: GeminiRecipeResult) -> bool:
    return any(
        [
            bool(clean_recipe_text(result.recipe_title_ur)),
            bool(clean_recipe_text(result.short_summary_ur)),
            any(clean_recipe_text(item.name_ur) for item in result.ingredients),
            any(clean_recipe_text(step.instruction_ur) for step in result.steps),
            any(clean_recipe_text(item.name_ur) for item in result.grocery_list),
        ]
    )


def recipe_quality_passed(result: GeminiRecipeResult, settings: Settings) -> bool:
    return (
        bool(clean_recipe_text(result.recipe_title_en))
        and
        len(result.ingredients) >= settings.recipe_min_ingredients
        and len(result.steps) >= settings.recipe_min_steps
        and len(result.grocery_list) >= 1
        and not summary_looks_raw(result.short_summary_en)
    )


def summary_looks_raw(summary: object) -> bool:
    text = clean_recipe_text(summary).lower()
    return bool(
        re.search(
            r"\b(recipe facts|ingredients\s+steps|grocery_list|time_and_servings|"
            r"missing_or_uncertain_details)\b",
            text,
        )
    )


def build_gemini_generation_config(
    settings: Settings,
    *,
    structured_schema: bool,
) -> dict[str, object]:
    config: dict[str, object] = {
        "response_mime_type": "application/json",
        "temperature": 0.1,
        "max_output_tokens": max(512, settings.gemini_max_output_tokens),
    }
    if structured_schema and settings.gemini_structured_output_enabled:
        config["response_schema"] = CANONICAL_RECIPE_SCHEMA
    if settings.gemini_thinking_level:
        config["thinking_config"] = {"thinking_level": settings.gemini_thinking_level}
    return config


def generate_content_json(
    *,
    client: object,
    model: str,
    contents: list[object],
    settings: Settings,
    structured_schema: bool,
) -> tuple[dict, object]:
    attempts = [
        build_gemini_generation_config(settings, structured_schema=structured_schema),
        build_gemini_generation_config(settings, structured_schema=False),
        {
            "response_mime_type": "application/json",
            "temperature": 0.1,
            "max_output_tokens": max(512, settings.gemini_max_output_tokens),
        },
    ]
    last_exc: Exception | None = None
    for config in attempts:
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
            text = extract_gemini_text(response)
            if not text:
                raise QueueJobFailure(
                    "gemini_recipe_json_empty",
                    "Gemini returned empty recipe JSON.",
                    stage="gemini_structured_extraction",
                )
            return _extract_json_object(text), response
        except TypeError as exc:
            last_exc = exc
            continue
        except Exception:
            raise
    if last_exc is not None:
        response = client.models.generate_content(model=model, contents=contents)
        text = extract_gemini_text(response)
        if not text:
            raise QueueJobFailure(
                "gemini_recipe_json_empty",
                "Gemini returned empty recipe JSON.",
                stage="gemini_structured_extraction",
            )
        return _extract_json_object(text), response
    raise RuntimeError("gemini_structured_generation_failed")


def canonical_time_value(time_value: object, key: str) -> int | None:
    if not isinstance(time_value, dict):
        return None
    aliases = {
        "prep": ("prep_minutes", "prep_time_minutes"),
        "cook": ("cook_minutes", "cook_time_minutes"),
        "total": ("total_minutes", "time_minutes"),
    }[key]
    for alias in aliases:
        value = safe_int_or_none(time_value.get(alias))
        if value is not None:
            return value
    return None


def validate_and_normalize_canonical_recipe(
    canonical_recipe: dict,
    source_metadata: dict[str, object],
    settings: Settings,
) -> tuple[GeminiRecipeResult, list[str]]:
    errors: list[str] = []
    platform = str(source_metadata.get("platform") or "")
    title = clean_recipe_text(
        canonical_recipe.get("title_en")
        or canonical_recipe.get("title")
        or source_metadata.get("title")
    )
    summary = clean_summary_text(
        canonical_recipe.get("summary_en") or canonical_recipe.get("summary"),
        title or "AI Recipe from Video",
    )
    ingredients_raw = canonical_recipe.get("ingredients")
    if not isinstance(ingredients_raw, list):
        ingredients_raw = []
    ingredients = [
        item
        for item in (parse_ingredient_item(value) for value in ingredients_raw)
        if item is not None
    ]
    steps_raw = canonical_recipe.get("steps")
    if not isinstance(steps_raw, list):
        steps_raw = []
    steps = [
        item
        for item in (
            parse_step_item(value, index + 1) for index, value in enumerate(steps_raw)
        )
        if item is not None
    ]
    steps = [
        RecipeStep(
            step_number=index + 1,
            instruction_en=step.instruction_en,
            instruction_ur=step.instruction_ur,
            tip_en=step.tip_en,
            tip_ur=step.tip_ur,
        )
        for index, step in enumerate(steps)
    ]
    grocery_raw = canonical_recipe.get("grocery_list")
    if not isinstance(grocery_raw, list):
        grocery_raw = []
    grocery = [
        item
        for item in (parse_grocery_item(value) for value in grocery_raw)
        if item is not None
    ]
    if not grocery:
        grocery = [
            GroceryItem(name_en=item.name_en, name_ur=item.name_ur, quantity=item.quantity)
            for item in ingredients
        ]
    time_value = canonical_recipe.get("time")
    difficulty = clean_recipe_text(canonical_recipe.get("difficulty")).lower()
    if difficulty in {"null", "none", "unknown"}:
        difficulty = "unknown"
    if difficulty not in {"easy", "medium", "hard", "unknown"}:
        difficulty = "unknown"
    confidence = clean_recipe_text(canonical_recipe.get("confidence")).lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = "low"
    tags_raw = canonical_recipe.get("tags") if isinstance(canonical_recipe.get("tags"), list) else []
    tags = [clean_recipe_text(tag).lower() for tag in tags_raw if clean_recipe_text(tag)]
    missing_raw = (
        canonical_recipe.get("missing_info")
        if isinstance(canonical_recipe.get("missing_info"), list)
        else []
    )
    missing = [
        clean_recipe_text(item)
        for item in missing_raw
        if clean_recipe_text(item) and clean_recipe_text(item).lower() not in set(tags)
    ]
    if not title:
        errors.append("title_missing")
    if len(ingredients) < settings.recipe_min_ingredients:
        errors.append("ingredients_too_few")
    if len(steps) < settings.recipe_min_steps:
        errors.append("steps_too_few")
    if summary_looks_raw(summary):
        errors.append("summary_raw_or_section_dump")
    result = GeminiRecipeResult(
        recipe_title_en=title or "AI Recipe from Video",
        recipe_title_ur=clean_recipe_text(canonical_recipe.get("title_ur")),
        short_summary_en=summary,
        short_summary_ur=clean_recipe_text(canonical_recipe.get("summary_ur")),
        confidence=confidence,
        prep_time_minutes=canonical_time_value(time_value, "prep"),
        cook_time_minutes=canonical_time_value(time_value, "cook"),
        servings=(
            str(canonical_recipe.get("servings"))
            if canonical_recipe.get("servings") not in {None, ""}
            else None
        ),
        difficulty=difficulty,
        ingredients=ingredients,
        steps=steps,
        grocery_list=grocery,
        missing_info_en=missing,
        missing_info_ur=[],
        storage_tip_en="",
        storage_tip_ur="",
        safety_note_en="",
        safety_note_ur="",
        source_assumptions_en=[],
        source_assumptions_ur=[],
    )
    if not recipe_quality_passed(result, settings):
        if "quality_gate_failed" not in errors:
            errors.append("quality_gate_failed")
    return result, errors


def canonical_to_safe_json(canonical: dict | None) -> str:
    if not canonical:
        return "{}"
    return json.dumps(canonical, ensure_ascii=False)[:12000]


def technical_gemini_failure(exc: Exception) -> bool:
    if isinstance(exc, QueueJobFailure):
        return exc.code in {
            "gemini_recipe_json_empty",
            "gemini_structured_extraction_failed",
        }
    status = exception_status(exc)
    if status in {400, 404, 429}:
        return True
    if status is not None and status >= 500:
        return True
    return type(exc).__name__ in {
        "ServerError",
        "InternalServerError",
        "NotFound",
        "BadRequest",
        "ClientError",
        "AttributeError",
        "TypeError",
    }


def structured_event_metadata(
    *,
    platform: str,
    model: str,
    input_mode: str,
    settings: Settings,
    result: GeminiRecipeResult | None = None,
    fallback_model: str | None = None,
    retry_count: int | None = None,
    quality_passed: bool | None = None,
    validation_errors_count: int | None = None,
    repair_used: bool | None = None,
    error_code: str | None = None,
    exception_type: str | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "platform": platform,
        "model": model,
        "input_mode": input_mode,
        "extraction_mode": settings.gemini_extraction_mode,
        "thinking_level": settings.gemini_thinking_level,
    }
    if fallback_model is not None:
        metadata["fallback_model"] = fallback_model
    if result is not None:
        metadata.update(recipe_quality_stats(result))
    if retry_count is not None:
        metadata["retry_count"] = retry_count
    if quality_passed is not None:
        metadata["quality_passed"] = quality_passed
    if validation_errors_count is not None:
        metadata["validation_errors_count"] = validation_errors_count
    if repair_used is not None:
        metadata["repair_used"] = repair_used
    if error_code is not None:
        metadata["error_code"] = error_code
    if exception_type is not None:
        metadata["exception_type"] = exception_type
    return metadata


def build_structured_contents(input_part: object, prompt: str) -> list[object]:
    return [input_part, prompt]


def run_structured_extraction_call(
    *,
    client: object,
    input_part: object,
    model: str,
    settings: Settings,
) -> tuple[dict, object]:
    return generate_content_json(
        client=client,
        model=model,
        contents=build_structured_contents(input_part, CANONICAL_RECIPE_PROMPT),
        settings=settings,
        structured_schema=True,
    )


def run_structured_repair_call(
    *,
    client: object,
    model: str,
    source_metadata: dict[str, object],
    platform: str,
    validation_errors: list[str],
    invalid_canonical: dict | None,
    settings: Settings,
) -> dict:
    prompt = (
        f"{CANONICAL_REPAIR_PROMPT}\n\n"
        f"Platform: {platform}\n"
        f"Source title: {clean_recipe_text(source_metadata.get('title')) or 'unknown'}\n"
        f"Validation errors: {', '.join(validation_errors[:12])}\n\n"
        f"Invalid JSON:\n{canonical_to_safe_json(invalid_canonical)}"
    )
    parsed, _response = generate_content_json(
        client=client,
        model=model,
        contents=[prompt],
        settings=settings,
        structured_schema=True,
    )
    return parsed


def run_single_pass_structured_extraction(
    *,
    repository: MediaJobRepository,
    job: MediaJobRecord,
    settings: Settings,
    client: object,
    input_part: object,
    input_mode: str,
    source_metadata: dict[str, object],
    platform: str,
) -> tuple[GeminiRecipeResult, str, bool]:
    model = settings.gemini_primary_model
    repository.insert_event(
        job.id,
        event_type="gemini_structured_extraction_started",
        message="Gemini structured extraction started.",
        metadata=structured_event_metadata(
            platform=platform,
            model=model,
            input_mode=input_mode,
            settings=settings,
        ),
    )
    canonical: dict | None = None
    try:
        canonical, _response = run_structured_extraction_call(
            client=client,
            input_part=input_part,
            model=model,
            settings=settings,
        )
    except Exception as exc:
        if not technical_gemini_failure(exc):
            repository.insert_event(
                job.id,
                event_type="gemini_structured_extraction_failed",
                message="Gemini structured extraction failed.",
                metadata=structured_event_metadata(
                    platform=platform,
                    model=model,
                    input_mode=input_mode,
                    settings=settings,
                    error_code="gemini_structured_extraction_failed",
                    exception_type=type(exc).__name__,
                ),
            )
            raise QueueJobFailure(
                "gemini_structured_extraction_failed",
                "Gemini structured recipe extraction failed.",
                stage="gemini_structured_extraction",
                exception_type=type(exc).__name__,
            ) from exc
        fallback_model = settings.gemini_fallback_model
        repository.insert_event(
            job.id,
            event_type="gemini_structured_extraction_fallback_model_used",
            message="Gemini structured extraction retrying with fallback model.",
            metadata=structured_event_metadata(
                platform=platform,
                model=model,
                fallback_model=fallback_model,
                input_mode=input_mode,
                settings=settings,
                retry_count=1,
                exception_type=type(exc).__name__,
            ),
        )
        model = fallback_model
        try:
            canonical, _response = run_structured_extraction_call(
                client=client,
                input_part=input_part,
                model=model,
                settings=settings,
            )
        except Exception as fallback_exc:
            repository.insert_event(
                job.id,
                event_type="gemini_structured_extraction_failed",
                message="Gemini structured extraction failed.",
                metadata=structured_event_metadata(
                    platform=platform,
                    model=model,
                    input_mode=input_mode,
                    settings=settings,
                    error_code="gemini_structured_extraction_failed",
                    exception_type=type(fallback_exc).__name__,
                ),
            )
            raise QueueJobFailure(
                "gemini_structured_extraction_failed",
                "Gemini structured recipe extraction failed.",
                stage="gemini_structured_extraction",
                exception_type=type(fallback_exc).__name__,
            ) from fallback_exc
    result, errors = validate_and_normalize_canonical_recipe(
        canonical,
        source_metadata,
        settings,
    )
    quality_passed = not errors and recipe_quality_passed(result, settings)
    repair_used = False
    if settings.recipe_quality_gate_enabled and not quality_passed:
        if not settings.recipe_repair_enabled:
            return result, model, repair_used
        repair_model = settings.effective_gemini_repair_model
        repository.insert_event(
            job.id,
            event_type="recipe_card_repair_started",
            message="Recipe card repair started.",
            metadata=structured_event_metadata(
                platform=platform,
                model=repair_model,
                input_mode=input_mode,
                settings=settings,
                result=result,
                validation_errors_count=len(errors),
                repair_used=True,
                quality_passed=False,
            ),
        )
        try:
            repaired = run_structured_repair_call(
                client=client,
                model=repair_model,
                source_metadata=source_metadata,
                platform=platform,
                validation_errors=errors,
                invalid_canonical=canonical,
                settings=settings,
            )
            repaired_result, repaired_errors = validate_and_normalize_canonical_recipe(
                repaired,
                source_metadata,
                settings,
            )
            repair_used = True
            result = repaired_result
            model = repair_model
            errors = repaired_errors
            quality_passed = not errors and recipe_quality_passed(result, settings)
            repository.insert_event(
                job.id,
                event_type="recipe_card_repair_completed",
                message="Recipe card repair completed.",
                metadata=structured_event_metadata(
                    platform=platform,
                    model=repair_model,
                    input_mode=input_mode,
                    settings=settings,
                    result=result,
                    validation_errors_count=len(errors),
                    repair_used=True,
                    quality_passed=quality_passed,
                ),
            )
        except Exception as exc:
            repository.insert_event(
                job.id,
                event_type="recipe_card_repair_failed",
                message="Recipe card repair failed.",
                metadata=structured_event_metadata(
                    platform=platform,
                    model=repair_model,
                    input_mode=input_mode,
                    settings=settings,
                    result=result,
                    validation_errors_count=len(errors),
                    repair_used=True,
                    quality_passed=False,
                    error_code="recipe_card_repair_failed",
                    exception_type=type(exc).__name__,
                ),
            )
    final_passed = recipe_quality_passed(result, settings) and not errors
    repository.insert_event(
        job.id,
        event_type="recipe_quality_gate_checked",
        message="Recipe quality gate checked.",
        metadata=structured_event_metadata(
            platform=platform,
            model=model,
            input_mode=input_mode,
            settings=settings,
            result=result,
            validation_errors_count=len(errors),
            repair_used=repair_used,
            quality_passed=final_passed,
        ),
    )
    if settings.recipe_quality_gate_enabled and not final_passed:
        repository.insert_event(
            job.id,
            event_type="recipe_quality_gate_failed",
            message="Recipe quality gate failed.",
            metadata=structured_event_metadata(
                platform=platform,
                model=model,
                input_mode=input_mode,
                settings=settings,
                result=result,
                validation_errors_count=len(errors),
                repair_used=repair_used,
                quality_passed=False,
                error_code="recipe_quality_gate_failed",
            ),
        )
        raise QueueJobFailure(
            "recipe_quality_gate_failed",
            "Could not extract enough recipe details from this video.",
            stage="recipe_quality_gate",
        )
    repository.insert_event(
        job.id,
        event_type="gemini_structured_extraction_completed",
        message="Gemini structured extraction completed.",
        metadata=structured_event_metadata(
            platform=platform,
            model=model,
            input_mode=input_mode,
            settings=settings,
            result=result,
            validation_errors_count=len(errors),
            repair_used=repair_used,
            quality_passed=final_passed,
        ),
    )
    return result, model, repair_used


def gemini_response_metadata(response: object) -> dict[str, object]:
    candidates = getattr(response, "candidates", []) or []
    parts_count = 0
    finish_reason = None
    for candidate in candidates:
        if finish_reason is None:
            finish_reason = getattr(candidate, "finish_reason", None)
            if hasattr(finish_reason, "name"):
                finish_reason = getattr(finish_reason, "name")
        content = getattr(candidate, "content", None)
        parts_count += len(getattr(content, "parts", []) or [])
    prompt_feedback = getattr(response, "prompt_feedback", None)
    return {
        "candidate_count": len(candidates),
        "parts_count": parts_count,
        "finish_reason": str(finish_reason) if finish_reason else None,
        "prompt_feedback_present": prompt_feedback is not None,
        "safety_block_present": bool(prompt_feedback),
    }


def generate_video_brief(
    *,
    client: object,
    uploaded_file: object,
    model: str,
    repository: MediaJobRepository,
    job: MediaJobRecord,
    event_metadata: dict[str, object],
) -> str:
    settings = get_settings()
    try:
        response = client.models.generate_content(
            model=model,
            contents=[uploaded_file, VIDEO_BRIEF_PROMPT],
            config={
                "temperature": 0.2,
                "max_output_tokens": max(
                    128,
                    settings.gemini_max_output_tokens,
                ),
            },
        )
    except Exception as exc:
        raise QueueJobFailure(
            "gemini_video_brief_failed",
            "Gemini video brief failed.",
            stage="gemini_video_brief",
            exception_type=type(exc).__name__,
        ) from exc
    text = extract_gemini_text(response)
    if not text:
        metadata = {**event_metadata, **gemini_response_metadata(response)}
        repository.insert_event(
            job.id,
            event_type="gemini_video_brief_failed",
            message="Gemini video brief failed.",
            metadata={**metadata, "error_code": "gemini_video_brief_empty"},
        )
        raise QueueJobFailure(
            "gemini_video_brief_empty",
            "Gemini returned an empty video brief.",
            stage="gemini_video_brief",
        )
    repository.insert_event(
        job.id,
        event_type="gemini_video_brief_completed",
        message="Gemini video brief completed.",
        metadata={
            **event_metadata,
            **gemini_response_metadata(response),
            "text_length": len(text),
        },
    )
    retry_count = 0
    while (
        settings.gemini_video_brief_retry_enabled
        and retry_count < max(0, settings.gemini_video_brief_max_retries)
        and not brief_has_recipe_content(text, settings)
    ):
        retry_count += 1
        repository.insert_event(
            job.id,
            event_type="gemini_video_brief_retry",
            message="Gemini video brief retry started.",
            metadata={
                "platform": event_metadata.get("platform"),
                "model": model,
                "text_length": len(text),
                "retry_count": retry_count,
            },
        )
        try:
            response = client.models.generate_content(
                model=model,
                contents=[uploaded_file, VIDEO_BRIEF_RETRY_PROMPT],
                config={
                    "temperature": 0.2,
                    "max_output_tokens": max(128, settings.gemini_max_output_tokens),
                },
            )
            retry_text = extract_gemini_text(response)
        except Exception as exc:
            raise QueueJobFailure(
                "gemini_video_brief_failed",
                "Gemini video brief failed.",
                stage="gemini_video_brief",
                exception_type=type(exc).__name__,
            ) from exc
        if retry_text:
            text = retry_text
            repository.insert_event(
                job.id,
                event_type="gemini_video_brief_completed",
                message="Gemini video brief completed.",
                metadata={
                    **event_metadata,
                    **gemini_response_metadata(response),
                    "text_length": len(text),
                    "retry_count": retry_count,
                },
            )
    return text


def generate_youtube_url_brief(
    *,
    client: object,
    youtube_url: str,
    model: str,
    repository: MediaJobRepository,
    job: MediaJobRecord,
    event_metadata: dict[str, object],
) -> str:
    settings = get_settings()
    try:
        from google.genai import types

        uri_kwargs = {"file" + "_uri": youtube_url, "mime_type": "video/mp4"}
        video_part = types.Part.from_uri(**uri_kwargs)
        response = client.models.generate_content(
            model=model,
            contents=[video_part, VIDEO_BRIEF_PROMPT],
            config={
                "temperature": 0.2,
                "max_output_tokens": max(
                    128,
                    settings.gemini_max_output_tokens,
                ),
            },
        )
    except Exception as exc:
        raise QueueJobFailure(
            "youtube_gemini_url_failed",
            "Gemini YouTube URL analysis failed.",
            stage="youtube_gemini_url",
            exception_type=type(exc).__name__,
        ) from exc
    text = extract_gemini_text(response)
    if not text:
        repository.insert_event(
            job.id,
            event_type="youtube_gemini_url_failed",
            message="Gemini YouTube URL analysis failed.",
            metadata={
                **event_metadata,
                **gemini_response_metadata(response),
                "error_code": "youtube_gemini_url_failed",
            },
        )
        raise QueueJobFailure(
            "youtube_gemini_url_failed",
            "Gemini YouTube URL analysis failed.",
            stage="youtube_gemini_url",
        )
    repository.insert_event(
        job.id,
        event_type="gemini_video_brief_completed",
        message="Gemini video brief completed.",
        metadata={
            **event_metadata,
            **gemini_response_metadata(response),
            "text_length": len(text),
        },
    )
    retry_count = 0
    while (
        settings.gemini_video_brief_retry_enabled
        and retry_count < max(0, settings.gemini_video_brief_max_retries)
        and not brief_has_recipe_content(text, settings)
    ):
        retry_count += 1
        repository.insert_event(
            job.id,
            event_type="gemini_video_brief_retry",
            message="Gemini video brief retry started.",
            metadata={
                "platform": event_metadata.get("platform"),
                "model": model,
                "text_length": len(text),
                "retry_count": retry_count,
            },
        )
        try:
            response = client.models.generate_content(
                model=model,
                contents=[video_part, VIDEO_BRIEF_RETRY_PROMPT],
                config={
                    "temperature": 0.2,
                    "max_output_tokens": max(128, settings.gemini_max_output_tokens),
                },
            )
            retry_text = extract_gemini_text(response)
        except Exception as exc:
            raise QueueJobFailure(
                "youtube_gemini_url_failed",
                "Gemini YouTube URL analysis failed.",
                stage="youtube_gemini_url",
                exception_type=type(exc).__name__,
            ) from exc
        if retry_text:
            text = retry_text
            repository.insert_event(
                job.id,
                event_type="gemini_video_brief_completed",
                message="Gemini video brief completed.",
                metadata={
                    **event_metadata,
                    **gemini_response_metadata(response),
                    "text_length": len(text),
                    "retry_count": retry_count,
                },
            )
    return text


def generate_recipe_json(
    *,
    client: object,
    model: str,
    brief: str,
    max_output_tokens: int,
) -> dict:
    prompt = f"{RECIPE_JSON_PROMPT}{brief}"
    try:
        response = client.models.generate_content(
            model=model,
            contents=[prompt],
            config={
                "response_mime_type": "application/json",
                "temperature": 0.1,
                "max_output_tokens": max(512, max_output_tokens),
            },
        )
    except TypeError:
        response = client.models.generate_content(model=model, contents=[prompt])
    text = extract_gemini_text(response)
    if not text:
        raise QueueJobFailure(
            "gemini_recipe_json_empty",
            "Gemini returned empty recipe JSON.",
            stage="gemini_recipe_json",
        )
    parsed = _extract_json_object(text)
    required = {"title", "summary", "ingredients", "steps"}
    if not required.issubset(parsed):
        raise ValueError("Recipe JSON missing required keys.")
    return parsed


def exception_status(exc: Exception) -> int | None:
    for name in ("status_code", "code"):
        value = getattr(exc, name, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def is_retryable_json_error(exc: Exception) -> bool:
    status = exception_status(exc)
    if status is not None and status >= 500:
        return True
    return type(exc).__name__ in {"ServerError", "InternalServerError"}


def normalize_recipe_json(parsed: dict) -> dict:
    ingredients = []
    for item in parsed.get("ingredients") or []:
        if isinstance(item, dict):
            ingredients.append(item)
        elif clean_recipe_text(item):
            ingredients.append(clean_recipe_text(item))
    steps = []
    for item in parsed.get("steps") or []:
        if isinstance(item, dict):
            steps.append(item)
        elif clean_recipe_text(item):
            steps.append(clean_recipe_text(item))
    return {
        "title": str(parsed.get("title") or "AI Recipe from Video").strip(),
        "summary": str(parsed.get("summary") or "").strip(),
        "ingredients": ingredients,
        "steps": steps,
        "grocery_list": parsed.get("grocery_list") or parsed.get("grocery") or [],
        "prep_time_minutes": parsed.get("prep_time_minutes"),
        "cook_time_minutes": parsed.get("cook_time_minutes"),
        "servings": parsed.get("servings"),
        "difficulty": parsed.get("difficulty"),
        "cuisine": parsed.get("cuisine"),
        "tags": parsed.get("tags") if isinstance(parsed.get("tags"), list) else [],
        "missing_info": parsed.get("missing_info") or parsed.get("uncertainties") or [],
        "confidence": parsed.get("confidence"),
    }


def fallback_recipe_json_from_brief(
    brief_text: str,
    source_metadata: dict[str, object],
) -> dict:
    sections = parse_brief_sections(brief_text)
    title = str(source_metadata.get("title") or "").strip()
    if not title:
        title = next(
            (line.strip("#:- ") for line in brief_text.splitlines() if line.strip()),
            "AI Recipe from Video",
        )
    platform = str(source_metadata.get("platform") or "").strip().lower()
    tags = ["ai-video-recipe"]
    if platform:
        tags.append(platform)
    summary = " ".join(sections.get("summary") or [])
    if not summary:
        summary = f"AI extracted recipe details for {title}."
    steps = sections.get("steps") or []
    if not steps:
        action_lines = [
            line
            for line in split_brief_lines(brief_text)
            if re.search(
                r"\b(add|mix|cook|fry|boil|bake|roast|simmer|stir|serve|heat)\b",
                line,
                flags=re.I,
            )
        ]
        steps = action_lines[:8]
    return {
        "title": title or "AI Recipe from Video",
        "summary": summary,
        "ingredients": sections.get("ingredients") or [],
        "steps": steps,
        "grocery_list": sections.get("grocery") or sections.get("ingredients") or [],
        "prep_time_minutes": None,
        "cook_time_minutes": None,
        "servings": None,
        "difficulty": None,
        "cuisine": None,
        "missing_info": sections.get("missing") or [],
        "confidence": (sections.get("confidence") or ["low"])[0],
        "brief": brief_text,
        "tags": [*tags, *sections.get("tags", [])],
    }


def convert_brief_to_recipe_json(
    brief_text: str,
    source_metadata: dict[str, object],
    settings: Settings,
    *,
    client: object,
    repository: MediaJobRepository,
    job: MediaJobRecord,
    event_metadata: dict[str, object],
) -> tuple[dict, str]:
    brief_for_json = truncate_brief_for_json(
        brief_text,
        settings.gemini_recipe_brief_max_chars,
    )
    primary_model = settings.gemini_text_model or settings.effective_gemini_video_model
    models = [primary_model]
    if primary_model != "gemini-2.5-flash":
        models.append("gemini-2.5-flash")
    last_exc: Exception | None = None
    retry_count = 0
    for model_name in models:
        attempts = 3 if settings.gemini_json_retry_enabled else 1
        for attempt in range(attempts):
            try:
                parsed = normalize_recipe_json(
                    generate_recipe_json(
                        client=client,
                        model=model_name,
                        brief=brief_for_json,
                        max_output_tokens=settings.gemini_max_output_tokens,
                    )
                )
                if not parsed["title"] or not parsed["steps"]:
                    raise ValueError("Recipe JSON missing usable title or steps.")
                repository.insert_event(
                    job.id,
                    event_type="gemini_recipe_json_completed",
                    message="Gemini recipe JSON completed.",
                    metadata={
                        **event_metadata,
                        "model": model_name,
                        "json_keys_count": len(parsed.keys()),
                        "ingredients_count": len(parsed["ingredients"]),
                        "steps_count": len(parsed["steps"]),
                        "retry_count": retry_count,
                        "save_mode": "gemini_json",
                    },
                )
                return parsed, "gemini_json"
            except (ValueError, QueueJobFailure) as exc:
                last_exc = exc
                break
            except Exception as exc:
                last_exc = exc
                if not is_retryable_json_error(exc) or attempt == attempts - 1:
                    break
                retry_count += 1
                repository.insert_event(
                    job.id,
                    event_type="gemini_recipe_json_retry",
                    message="Gemini recipe JSON retry.",
                    metadata={
                        **event_metadata,
                        "model": model_name,
                        "retry_count": retry_count,
                        "error_code": "gemini_recipe_json_failed",
                        "exception_type": type(exc).__name__,
                        "http_status": exception_status(exc),
                    },
                )
                time.sleep(min(2, 0.5 * (2 ** (retry_count - 1))))
        retry_count += 1
        if model_name != models[-1]:
            repository.insert_event(
                job.id,
                event_type="gemini_recipe_json_retry",
                message="Gemini recipe JSON retry with fallback model.",
                metadata={
                    **event_metadata,
                    "model": model_name,
                    "fallback_model": models[-1],
                    "retry_count": retry_count,
                    "error_code": "gemini_recipe_json_failed",
                    "exception_type": type(last_exc).__name__ if last_exc else None,
                    "http_status": exception_status(last_exc) if last_exc else None,
                },
            )
    repository.insert_event(
        job.id,
        event_type="gemini_recipe_json_failed",
        message="Gemini recipe JSON failed.",
        metadata={
            **event_metadata,
            "model": models[-1],
            "retry_count": retry_count,
            "error_code": (
                last_exc.code
                if isinstance(last_exc, QueueJobFailure)
                else "gemini_recipe_json_invalid"
            ),
            "exception_type": type(last_exc).__name__ if last_exc else None,
            "http_status": exception_status(last_exc) if last_exc else None,
        },
    )
    if not settings.gemini_json_fallback_enabled:
        raise QueueJobFailure(
            "gemini_recipe_json_invalid",
            "Gemini returned an invalid recipe response.",
            stage="gemini_recipe_json",
            exception_type=type(last_exc).__name__ if last_exc else None,
        )
    if not settings.local_brief_parser_enabled:
        raise QueueJobFailure(
            "gemini_recipe_json_invalid",
            "Gemini returned an invalid recipe response.",
            stage="gemini_recipe_json",
            exception_type=type(last_exc).__name__ if last_exc else None,
        )
    repository.insert_event(
        job.id,
        event_type="brief_card_build_started",
        message="Brief card build started.",
        metadata={**event_metadata, "save_mode": "local_brief_fallback"},
    )
    fallback = fallback_recipe_json_from_brief(brief_text, source_metadata)
    repository.insert_event(
        job.id,
        event_type="gemini_recipe_json_fallback_used",
        message="Gemini recipe JSON fallback used.",
        metadata={
            **event_metadata,
            "save_mode": "local_brief_fallback",
            "text_length": len(brief_text),
        },
    )
    repository.insert_event(
        job.id,
        event_type="brief_card_build_completed",
        message="Brief card build completed.",
        metadata={
            **event_metadata,
            "save_mode": "local_brief_fallback",
            "ingredients_count": len(fallback["ingredients"]),
            "steps_count": len(fallback["steps"]),
        },
    )
    return fallback, "local_brief_fallback"


def recipe_json_to_caption_shape(parsed: dict) -> dict:
    total_time = None
    prep = parsed.get("prep_time_minutes")
    cook = parsed.get("cook_time_minutes")
    if isinstance(prep, int) or isinstance(cook, int):
        total_time = (prep if isinstance(prep, int) else 0) + (
            cook if isinstance(cook, int) else 0
        )
    tags = parsed.get("tags") or []
    cuisine = parsed.get("cuisine")
    if cuisine and isinstance(tags, list):
        tags = [*tags, str(cuisine)]
    return {
        "is_recipe": True,
        "confidence": "medium",
        "title": parsed.get("title") or "Recipe",
        "summary": parsed.get("summary") or "",
        "ingredients": parsed.get("ingredients") or [],
        "steps": parsed.get("steps") or [],
        "servings": parsed.get("servings"),
        "time_minutes": total_time,
        "tags": tags,
    }


def delete_gemini_file_best_effort(
    client: object | None,
    uploaded_file: object | None,
) -> None:
    if client is None or uploaded_file is None:
        return
    name = get_gemini_file_name(uploaded_file)
    if not name:
        return
    try:
        files_api = getattr(client, "files")
        try:
            files_api.delete(name=name)
        except TypeError:
            files_api.delete(name)
    except Exception:
        pass


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
    temp_file = Path(temp_file_path) if temp_file_path is not None else None
    if (
        not extract_result.get("video_bytes_verified")
        or temp_file_path is None
        or temp_file is None
        or not temp_file.exists()
        or temp_file.stat().st_size <= 0
    ):
        raise QueueJobFailure(
            "video_required_for_ai",
            "Video could not be prepared for AI analysis.",
            stage="gemini_video_analysis",
        )
    if settings.gemini_api_key is None:
        raise QueueJobFailure(
            "gemini_api_key_missing",
            "Gemini video analysis is not configured.",
            stage="gemini_video_analysis",
        )

    model = settings.effective_gemini_video_model
    resolver = str(extract_result.get("resolver") or "ytdlp_stream")
    size_bytes = safe_int_or_none(extract_result.get("size_bytes"))
    size_bytes = size_bytes if size_bytes is not None else temp_file.stat().st_size
    duration_seconds = safe_int_or_none(extract_result.get("duration_seconds"))
    mime_type = "video/mp4"
    api_key = settings.gemini_api_key.get_secret_value()
    client: object | None = None
    uploaded_file: object | None = None

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
    try:
        client = create_gemini_client(
            api_key,
            settings.gemini_file_upload_timeout_seconds,
        )
        base_ai_metadata = safe_ai_event_metadata(
            platform=platform,
            resolver=resolver,
            model=model,
            duration_seconds=duration_seconds,
            size_bytes=size_bytes,
            video_bytes_verified=True,
            stage="gemini_file_upload",
            input_mode=settings.gemini_video_input_mode,
            analysis_mode=settings.gemini_video_analysis_mode,
            mime_type=mime_type,
        )
        repository.insert_event(
            job.id,
            event_type="gemini_file_upload_started",
            message="Gemini video file upload started.",
            metadata=base_ai_metadata,
        )
        try:
            uploaded_file = upload_gemini_video_file(
                client=client,
                temp_file_path=temp_file_path,
                mime_type=mime_type,
            )
        except Exception as exc:
            raise QueueJobFailure(
                "gemini_file_upload_failed",
                "Gemini video file upload failed.",
                stage="gemini_file_upload",
                exception_type=type(exc).__name__,
            ) from exc
        repository.insert_event(
            job.id,
            event_type="gemini_file_upload_completed",
            message="Gemini video file upload completed.",
            metadata=base_ai_metadata,
        )
        repository.insert_event(
            job.id,
            event_type="gemini_file_processing_started",
            message="Gemini video file processing started.",
            metadata={**base_ai_metadata, "stage": "gemini_file_processing"},
        )
        uploaded_file = wait_for_gemini_file_active(
            client=client,
            uploaded_file=uploaded_file,
            settings=settings,
        )
        repository.insert_event(
            job.id,
            event_type="gemini_file_active",
            message="Gemini video file is active.",
            metadata={
                **base_ai_metadata,
                "stage": "gemini_file_processing",
                "file_state": "ACTIVE",
            },
        )
        if settings.gemini_extraction_mode == "single_pass_structured":
            result, accepted_model, repair_used = run_single_pass_structured_extraction(
                repository=repository,
                job=job,
                settings=settings,
                client=client,
                input_part=uploaded_file,
                input_mode=settings.gemini_video_input_mode,
                source_metadata=extract_result,
                platform=platform,
            )
            parsed = {"save_mode": "single_pass_structured"}
            repository.insert_event(
                job.id,
                event_type="gemini_video_analysis_completed",
                message="Gemini video analysis completed.",
                metadata=safe_ai_event_metadata(
                    platform=platform,
                    resolver=resolver,
                    model=accepted_model,
                    duration_seconds=duration_seconds,
                    size_bytes=size_bytes,
                    video_bytes_verified=True,
                    stage="gemini_video_analysis",
                    analysis_mode=settings.gemini_extraction_mode,
                    input_mode=settings.gemini_video_input_mode,
                    mime_type=mime_type,
                )
                | {"repair_used": repair_used},
            )
            return save_gemini_recipe_card(
                repository=repository,
                job=job,
                result=result,
                model=accepted_model,
                platform=platform,
                resolver=resolver,
                uploaded_thumbnail_url=uploaded_thumbnail_url,
                source_metadata=extract_result,
                used_fallback=accepted_model != settings.gemini_primary_model,
                client=client,
                settings=settings,
            )
        brief_metadata = {
            **base_ai_metadata,
            "stage": "gemini_video_brief",
            "model": model,
        }
        repository.insert_event(
            job.id,
            event_type="gemini_video_brief_started",
            message="Gemini video brief started.",
            metadata=brief_metadata,
        )
        brief = generate_video_brief(
            client=client,
            uploaded_file=uploaded_file,
            model=model,
            repository=repository,
            job=job,
            event_metadata=brief_metadata,
        )
        brief_used_fallback = not brief_has_recipe_content(brief, settings)
        text_model = settings.effective_gemini_text_model
        json_metadata = {
            **base_ai_metadata,
            "stage": "gemini_recipe_json",
            "model": text_model,
            "text_length": len(brief),
        }
        repository.insert_event(
            job.id,
            event_type="gemini_recipe_json_started",
            message="Gemini recipe JSON started.",
            metadata=json_metadata,
        )
        parsed, save_mode = convert_brief_to_recipe_json(
            brief,
            extract_result,
            settings,
            client=client,
            repository=repository,
            job=job,
            event_metadata=json_metadata,
        )
        parsed["brief"] = brief
        parsed["save_mode"] = save_mode
        result = normalize_recipe_card_payload(
            parsed,
            extract_result,
            platform,
            text_model,
            PROMPT_VERSION,
        )
    except QueueJobFailure as exc:
        if exc.code == "gemini_video_brief_empty":
            raise
        failed_event = "gemini_video_analysis_failed"
        if exc.stage == "gemini_video_brief":
            failed_event = "gemini_video_brief_failed"
        elif exc.stage == "gemini_recipe_json":
            failed_event = "gemini_recipe_json_failed"
        repository.insert_event(
            job.id,
            event_type=failed_event,
            message="Gemini video analysis failed.",
            metadata=safe_ai_event_metadata(
                platform=platform,
                resolver=resolver,
                model=model,
                duration_seconds=duration_seconds,
                size_bytes=size_bytes,
                video_bytes_verified=True,
                stage=exc.stage,
                error_code=exc.code,
                exception_type=exc.exception_type,
                input_mode=settings.gemini_video_input_mode,
                analysis_mode=settings.gemini_video_analysis_mode,
                mime_type=mime_type,
            ),
        )
        raise
    except ValueError as exc:
        fail_stage = "gemini_recipe_json"
        repository.insert_event(
            job.id,
            event_type="gemini_recipe_json_failed",
            message="Gemini recipe JSON failed.",
            metadata=safe_ai_event_metadata(
                platform=platform,
                resolver=resolver,
                model=settings.effective_gemini_text_model,
                duration_seconds=duration_seconds,
                size_bytes=size_bytes,
                video_bytes_verified=True,
                stage=fail_stage,
                error_code="gemini_recipe_json_invalid",
                exception_type=type(exc).__name__,
                input_mode=settings.gemini_video_input_mode,
                analysis_mode=settings.gemini_video_analysis_mode,
                mime_type=mime_type,
            ),
        )
        raise QueueJobFailure(
            "gemini_recipe_json_invalid",
            "Gemini returned an invalid recipe response.",
            stage=fail_stage,
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
                input_mode=settings.gemini_video_input_mode,
                analysis_mode=settings.gemini_video_analysis_mode,
                mime_type=mime_type,
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
                input_mode=settings.gemini_video_input_mode,
                analysis_mode=settings.gemini_video_analysis_mode,
                mime_type=mime_type,
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
        delete_gemini_file_best_effort(client, uploaded_file)

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
            analysis_mode=settings.gemini_video_analysis_mode,
            input_mode=settings.gemini_video_input_mode,
            mime_type=mime_type,
        ),
    )
    return save_gemini_recipe_card(
        repository=repository,
        job=job,
        result=result,
        model=settings.effective_gemini_text_model,
        platform=platform,
        resolver=resolver,
        uploaded_thumbnail_url=uploaded_thumbnail_url,
        source_metadata=extract_result,
        used_fallback=parsed.get("save_mode") == "local_brief_fallback" or brief_used_fallback,
        brief_text=brief,
        client=client,
        settings=settings,
    )


def clean_recipe_text(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*\d+[\).\:-]\s*", "", text, flags=re.M)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def split_brief_lines(text: str) -> list[str]:
    return [
        clean_recipe_text(line)
        for line in str(text or "").splitlines()
        if clean_recipe_text(line)
    ]


def parse_brief_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = "summary"
    for raw_line in str(text or "").splitlines():
        raw = str(raw_line or "").strip()
        line = clean_recipe_text(raw)
        if not line:
            continue
        heading = clean_recipe_text(raw).lower().rstrip(":")
        if re.fullmatch(r"(title|recipe title)", heading):
            current = "title"
            continue
        if re.fullmatch(r"(summary|recipe facts|overview)", heading):
            current = "summary"
            continue
        if re.search(r"\b(visible )?ingredients?|quantities\b", heading):
            current = "ingredients"
            continue
        if re.search(r"\b(steps?|method|instructions?|directions)\b", heading):
            current = "steps"
            continue
        if re.search(r"\b(grocery|shopping|grocery_list)\b", heading):
            current = "grocery"
            continue
        if re.search(r"\b(timing|time|time_and_servings|servings)\b", heading):
            current = "timing"
            continue
        if re.search(r"\b(cuisine|style|cuisine_style)\b", heading):
            current = "tags"
            continue
        if re.search(r"\b(confidence)\b", heading):
            current = "confidence"
            continue
        if re.search(r"\b(missing|uncertain|uncertainties|missing_or_uncertain_details)\b", heading):
            current = "missing"
            continue
        inline_heading = re.match(r"^([A-Za-z_ ]{3,40}):\s+(.+)$", raw)
        if inline_heading:
            possible = clean_recipe_text(inline_heading.group(1)).lower()
            rest = clean_recipe_text(inline_heading.group(2))
            if re.search(r"\b(visible )?ingredients?|quantities\b", possible):
                current = "ingredients"
                if rest:
                    sections.setdefault(current, []).append(rest)
                continue
            if re.search(r"\b(steps?|method|instructions?|directions)\b", possible):
                current = "steps"
                if rest:
                    sections.setdefault(current, []).append(rest)
                continue
        sections.setdefault(current, []).append(line)
    return sections


def item_text(value: object, *keys: str) -> str:
    if isinstance(value, dict):
        return clean_recipe_text(" ".join(str(value.get(key) or "") for key in keys))
    return clean_recipe_text(value)


def parse_ingredient_item(value: object) -> RecipeIngredient | None:
    if isinstance(value, dict):
        name = clean_recipe_text(value.get("name_en") or value.get("name"))
        quantity = clean_recipe_text(value.get("quantity"))
        notes = clean_recipe_text(value.get("notes_en") or value.get("notes"))
        name_ur = clean_recipe_text(value.get("name_ur"))
        notes_ur = clean_recipe_text(value.get("notes_ur"))
    else:
        text = clean_recipe_text(value)
        if not text:
            return None
        name = text
        quantity = ""
        notes = ""
        name_ur = ""
        notes_ur = ""
        match = re.match(r"^([^:–—]{2,80})\s*[:–—]\s*(.+)$", text)
        if match:
            name = clean_recipe_text(match.group(1))
            quantity = clean_recipe_text(match.group(2))
    if not name and notes:
        name = notes
        notes = ""
    if not name:
        return None
    return RecipeIngredient(
        name_en=name,
        name_ur=name_ur,
        quantity=quantity,
        notes_en=notes,
        notes_ur=notes_ur,
    )


def parse_step_item(value: object, index: int) -> RecipeStep | None:
    if isinstance(value, dict):
        instruction = clean_recipe_text(
            value.get("instruction_en")
            or value.get("instruction")
            or value.get("text")
        )
        instruction_ur = clean_recipe_text(value.get("instruction_ur"))
        tip = clean_recipe_text(value.get("tip_en") or value.get("tip"))
        tip_ur = clean_recipe_text(value.get("tip_ur"))
        step_number = safe_int_or_none(value.get("step_number")) or index
    else:
        instruction = clean_recipe_text(value)
        instruction_ur = ""
        tip = ""
        tip_ur = ""
        step_number = index
    if not instruction:
        return None
    return RecipeStep(
        step_number=max(1, step_number),
        instruction_en=instruction,
        instruction_ur=instruction_ur,
        tip_en=tip,
        tip_ur=tip_ur,
    )


def parse_grocery_item(value: object) -> GroceryItem | None:
    if isinstance(value, dict):
        name = clean_recipe_text(value.get("name_en") or value.get("name"))
        quantity = clean_recipe_text(value.get("quantity"))
        name_ur = clean_recipe_text(value.get("name_ur"))
    else:
        ingredient = parse_ingredient_item(value)
        if ingredient is None:
            return None
        name = ingredient.name_en
        quantity = ingredient.quantity
        name_ur = ingredient.name_ur
    if not name:
        return None
    return GroceryItem(name_en=name, name_ur=name_ur, quantity=quantity)


def split_inline_items(lines: list[str]) -> list[str]:
    items: list[str] = []
    for line in lines:
        parts = re.split(r"\s*(?:;|\n)\s*", line)
        if len(parts) == 1 and "," in line and len(line) < 240:
            parts = re.split(r"\s*,\s*", line)
        items.extend(clean_recipe_text(part) for part in parts if clean_recipe_text(part))
    return items


def expand_recipe_items(values: list[object]) -> list[object]:
    expanded: list[object] = []
    for item in values:
        if isinstance(item, dict):
            expanded.append(item)
            continue
        expanded.extend(split_inline_items([str(item)]))
    return expanded


def clean_summary_text(summary: object, title: str) -> str:
    text = clean_recipe_text(summary)
    text = re.sub(
        r"^(recipe facts?|title|summary)\s*[:\-]\s*",
        "",
        text,
        flags=re.I,
    ).strip()
    text = re.sub(r"\b(INGREDIENTS|STEPS|GROCERY_LIST|TIME_AND_SERVINGS)\b.*", "", text).strip()
    if not text or text.lower() == title.lower():
        return f"AI extracted recipe details for {title}."
    parts = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(parts[:2]).strip()


def normalize_recipe_card_payload(
    raw_recipe: GeminiRecipeResult | dict,
    source_metadata: dict[str, object],
    platform: str,
    model: str,
    prompt_version: str,
) -> GeminiRecipeResult:
    if isinstance(raw_recipe, GeminiRecipeResult):
        known_tags = {"ai-video-recipe", platform, "tiktok", "instagram", "youtube", "facebook"}
        raw = {
            "title": raw_recipe.recipe_title_en,
            "summary": raw_recipe.short_summary_en,
            "ingredients": [item.model_dump(mode="json") for item in raw_recipe.ingredients],
            "steps": [item.model_dump(mode="json") for item in raw_recipe.steps],
            "grocery_list": [item.model_dump(mode="json") for item in raw_recipe.grocery_list],
            "prep_time_minutes": raw_recipe.prep_time_minutes,
            "cook_time_minutes": raw_recipe.cook_time_minutes,
            "servings": raw_recipe.servings,
            "difficulty": raw_recipe.difficulty,
            "confidence": raw_recipe.confidence,
            "tags": [
                item
                for item in raw_recipe.missing_info_en
                if clean_recipe_text(item).lower() in known_tags
            ],
            "missing_info": [
                item
                for item in raw_recipe.missing_info_en
                if clean_recipe_text(item).lower() not in known_tags
            ],
        }
    else:
        raw = raw_recipe

    brief_source = raw.get("brief") or raw.get("summary") or ""
    sections = parse_brief_sections(brief_source)
    section_title = " ".join(sections.get("title") or [])
    title = clean_recipe_text(
        raw.get("title") or section_title or source_metadata.get("title") or "AI Recipe from Video"
    )
    summary = clean_summary_text(raw.get("summary") or " ".join(sections.get("summary") or []), title)

    ingredients_source = raw.get("ingredients") or sections.get("ingredients") or []
    if isinstance(ingredients_source, list):
        ingredient_values = ingredients_source
    else:
        ingredient_values = [ingredients_source]
    ingredient_values = expand_recipe_items(ingredient_values)
    recipe_ingredients = [
        parsed
        for parsed in (parse_ingredient_item(item) for item in ingredient_values)
        if parsed is not None
    ]

    steps_source = raw.get("steps") or sections.get("steps") or []
    if isinstance(steps_source, list):
        step_values = steps_source
    else:
        step_values = [steps_source]
    recipe_steps = [
        parsed
        for parsed in (
            parse_step_item(item, index + 1) for index, item in enumerate(step_values)
        )
        if parsed is not None
    ]

    grocery_source = raw.get("grocery_list") or raw.get("grocery") or sections.get("grocery") or []
    if isinstance(grocery_source, list):
        grocery_values = grocery_source
    else:
        grocery_values = [grocery_source]
    grocery_values = expand_recipe_items(grocery_values)
    grocery_items = [
        parsed
        for parsed in (parse_grocery_item(item) for item in grocery_values)
        if parsed is not None
    ]
    if not grocery_items:
        grocery_items = [
            GroceryItem(
                name_en=item.name_en,
                name_ur=item.name_ur,
                quantity=item.quantity,
            )
            for item in recipe_ingredients
        ]

    tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
    tags = [clean_recipe_text(tag).lower() for tag in tags if clean_recipe_text(tag)]
    tags.extend(clean_recipe_text(tag).lower() for tag in sections.get("tags", []) if clean_recipe_text(tag))
    if "ai-video-recipe" not in tags:
        tags.append("ai-video-recipe")
    if platform and platform not in tags:
        tags.append(platform)

    confidence = clean_recipe_text(raw.get("confidence")).lower() or None
    if confidence not in {"low", "medium", "high"}:
        confidence = None
    difficulty = clean_recipe_text(raw.get("difficulty")).lower() or None
    if difficulty not in {"easy", "medium", "hard"}:
        difficulty = None

    missing_raw = raw.get("missing_info") or raw.get("uncertainties") or sections.get("missing") or []
    missing = [
        clean_recipe_text(item)
        for item in (missing_raw if isinstance(missing_raw, list) else [missing_raw])
        if clean_recipe_text(item) and clean_recipe_text(item).lower() not in set(tags)
    ]
    return GeminiRecipeResult(
        recipe_title_en=title,
        recipe_title_ur=clean_recipe_text(raw.get("title_ur") or raw.get("recipe_title_ur")),
        short_summary_en=summary,
        short_summary_ur=clean_recipe_text(raw.get("summary_ur") or raw.get("short_summary_ur")),
        confidence=confidence or "medium",
        prep_time_minutes=safe_int_or_none(raw.get("prep_time_minutes")),
        cook_time_minutes=safe_int_or_none(raw.get("cook_time_minutes")),
        servings=str(raw.get("servings")) if raw.get("servings") else None,
        difficulty=difficulty or "unknown",
        ingredients=recipe_ingredients,
        steps=recipe_steps,
        grocery_list=grocery_items,
        missing_info_en=missing,
        missing_info_ur=[],
        storage_tip_en="",
        storage_tip_ur="",
        safety_note_en="",
        safety_note_ur="",
        source_assumptions_en=[],
        source_assumptions_ur=[],
    )


def repair_recipe_card_with_gemini(
    *,
    client: object,
    model: str,
    brief_text: str,
    source_metadata: dict[str, object],
    settings: Settings,
) -> dict:
    source_title = clean_recipe_text(source_metadata.get("title"))
    prompt = (
        f"{RECIPE_REPAIR_PROMPT}\n\n"
        f"Source title: {source_title or 'unknown'}\n\n"
        f"Recipe brief:\n{truncate_brief_for_json(brief_text, settings.gemini_recipe_brief_max_chars)}"
    )
    try:
        response = client.models.generate_content(
            model=model,
            contents=[prompt],
            config={
                "response_mime_type": "application/json",
                "temperature": 0.1,
                "max_output_tokens": max(512, settings.gemini_max_output_tokens),
            },
        )
    except TypeError:
        response = client.models.generate_content(model=model, contents=[prompt])
    text = extract_gemini_text(response)
    if not text:
        raise QueueJobFailure(
            "gemini_recipe_json_empty",
            "Gemini returned empty recipe JSON.",
            stage="gemini_recipe_json",
        )
    parsed = _extract_json_object(text)
    if not {"title", "summary", "ingredients", "steps"}.issubset(parsed):
        raise ValueError("Repair JSON missing required keys.")
    parsed["brief"] = brief_text
    return normalize_recipe_json(parsed)


def apply_urdu_translation(
    result: GeminiRecipeResult,
    parsed: dict,
) -> GeminiRecipeResult:
    ingredients_ur = parsed.get("ingredients") if isinstance(parsed.get("ingredients"), list) else []
    steps_ur = parsed.get("steps") if isinstance(parsed.get("steps"), list) else []
    grocery_ur = parsed.get("grocery_list") if isinstance(parsed.get("grocery_list"), list) else []
    for index, item in enumerate(result.ingredients):
        if index >= len(ingredients_ur) or not isinstance(ingredients_ur[index], dict):
            continue
        item.name_ur = clean_recipe_text(ingredients_ur[index].get("name_ur") or item.name_ur)
        item.notes_ur = clean_recipe_text(ingredients_ur[index].get("notes_ur") or item.notes_ur)
    for index, step in enumerate(result.steps):
        if index >= len(steps_ur) or not isinstance(steps_ur[index], dict):
            continue
        step.instruction_ur = clean_recipe_text(
            steps_ur[index].get("instruction_ur") or step.instruction_ur
        )
        step.tip_ur = clean_recipe_text(steps_ur[index].get("tip_ur") or step.tip_ur)
    for index, item in enumerate(result.grocery_list):
        if index >= len(grocery_ur) or not isinstance(grocery_ur[index], dict):
            continue
        item.name_ur = clean_recipe_text(grocery_ur[index].get("name_ur") or item.name_ur)
    return result


def maybe_translate_urdu_fields(
    *,
    client: object | None,
    result: GeminiRecipeResult,
    model: str,
    settings: Settings,
) -> GeminiRecipeResult:
    if (
        client is None
        or not settings.recipe_bilingual_output_enabled
        or not settings.recipe_urdu_fallback_enabled
        or recipe_urdu_fields_present(result)
    ):
        return result
    payload = {
        "ingredients": [
            {"name_en": item.name_en, "notes_en": item.notes_en}
            for item in result.ingredients
        ],
        "steps": [
            {"instruction_en": step.instruction_en, "tip_en": step.tip_en}
            for step in result.steps
        ],
        "grocery_list": [
            {"name_en": item.name_en}
            for item in result.grocery_list
        ],
    }
    prompt = f"{URDU_TRANSLATION_PROMPT}\n\nJSON:\n{json.dumps(payload, ensure_ascii=False)}"
    try:
        response = client.models.generate_content(
            model=model,
            contents=[prompt],
            config={
                "response_mime_type": "application/json",
                "temperature": 0.1,
                "max_output_tokens": 2048,
            },
        )
        parsed = _extract_json_object(extract_gemini_text(response))
        return apply_urdu_translation(result, parsed)
    except Exception:
        return result


def enforce_recipe_quality(
    *,
    repository: MediaJobRepository,
    job: MediaJobRecord,
    result: GeminiRecipeResult,
    model: str,
    platform: str,
    source_metadata: dict[str, object],
    brief_text: str | None,
    used_fallback: bool,
    client: object | None,
    settings: Settings,
) -> GeminiRecipeResult:
    quality_passed = recipe_quality_passed(result, settings)
    repository.insert_event(
        job.id,
        event_type="recipe_quality_gate_checked",
        message="Recipe quality gate checked.",
        metadata=safe_quality_metadata(
            platform=platform,
            model=model,
            result=result,
            text_length=len(brief_text or ""),
            used_fallback=used_fallback,
            repair_used=False,
            quality_passed=quality_passed,
        ),
    )
    repair_used = False
    if (
        settings.recipe_quality_gate_enabled
        and not quality_passed
        and settings.recipe_repair_enabled
        and client is not None
        and brief_text
    ):
        for attempt in range(max(0, settings.gemini_recipe_repair_max_retries)):
            repository.insert_event(
                job.id,
                event_type="recipe_card_repair_started",
                message="Recipe card repair started.",
                metadata=safe_quality_metadata(
                    platform=platform,
                    model=model,
                    result=result,
                    text_length=len(brief_text),
                    retry_count=attempt + 1,
                    used_fallback=used_fallback,
                    repair_used=True,
                    quality_passed=False,
                ),
            )
            try:
                repaired = repair_recipe_card_with_gemini(
                    client=client,
                    model=model,
                    brief_text=brief_text,
                    source_metadata=source_metadata,
                    settings=settings,
                )
                result = normalize_recipe_card_payload(
                    repaired,
                    source_metadata,
                    platform,
                    model,
                    PROMPT_VERSION,
                )
                repair_used = True
                quality_passed = recipe_quality_passed(result, settings)
                repository.insert_event(
                    job.id,
                    event_type="recipe_card_repair_completed",
                    message="Recipe card repair completed.",
                    metadata=safe_quality_metadata(
                        platform=platform,
                        model=model,
                        result=result,
                        text_length=len(brief_text),
                        retry_count=attempt + 1,
                        used_fallback=used_fallback,
                        repair_used=True,
                        quality_passed=quality_passed,
                    ),
                )
                break
            except Exception as exc:
                repository.insert_event(
                    job.id,
                    event_type="recipe_card_repair_failed",
                    message="Recipe card repair failed.",
                    metadata=safe_quality_metadata(
                        platform=platform,
                        model=model,
                        result=result,
                        text_length=len(brief_text),
                        retry_count=attempt + 1,
                        used_fallback=used_fallback,
                        repair_used=True,
                        quality_passed=False,
                        error_code="recipe_card_repair_failed",
                        exception_type=type(exc).__name__,
                    ),
                )
    if settings.recipe_quality_gate_enabled and not recipe_quality_passed(result, settings):
        if len(result.ingredients) >= 1 and len(result.steps) >= 1:
            result.confidence = "low"
        else:
            repository.insert_event(
                job.id,
                event_type="recipe_quality_gate_failed",
                message="Recipe quality gate failed.",
                metadata=safe_quality_metadata(
                    platform=platform,
                    model=model,
                    result=result,
                    text_length=len(brief_text or ""),
                    used_fallback=used_fallback,
                    repair_used=repair_used,
                    quality_passed=False,
                    error_code="recipe_quality_gate_failed",
                ),
            )
            raise QueueJobFailure(
                "recipe_quality_gate_failed",
                "Could not extract enough recipe details from this video.",
                stage="recipe_quality_gate",
            )
    result = maybe_translate_urdu_fields(
        client=client,
        result=result,
        model=model,
        settings=settings,
    )
    if settings.recipe_bilingual_output_enabled and not recipe_urdu_fields_present(result):
        repository.insert_event(
            job.id,
            event_type="recipe_urdu_missing",
            message="Recipe Urdu fields remain missing.",
            metadata=safe_quality_metadata(
                platform=platform,
                model=model,
                result=result,
                text_length=len(brief_text or ""),
                used_fallback=used_fallback,
                repair_used=repair_used,
                quality_passed=recipe_quality_passed(result, settings),
            ),
        )
    return result


def save_gemini_recipe_card(
    *,
    repository: MediaJobRepository,
    job: MediaJobRecord,
    result: GeminiRecipeResult,
    model: str,
    platform: str,
    resolver: str,
    uploaded_thumbnail_url: str | None,
    source_metadata: dict[str, object] | None = None,
    used_fallback: bool = False,
    brief_text: str | None = None,
    client: object | None = None,
    settings: Settings | None = None,
) -> MediaJobRecord:
    settings = settings or get_settings()
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
        if settings.recipe_card_normalization_enabled:
            result = normalize_recipe_card_payload(
                result,
                source_metadata or {"platform": platform},
                platform,
                model,
                PROMPT_VERSION,
            )
        result = enforce_recipe_quality(
            repository=repository,
            job=job,
            result=result,
            model=model,
            platform=platform,
            source_metadata=source_metadata or {"platform": platform},
            brief_text=brief_text,
            used_fallback=used_fallback,
            client=client,
            settings=settings,
        )
        repository.insert_event(
            job.id,
            event_type="recipe_card_normalized",
            message="Recipe card normalized.",
            metadata={
                **safe_quality_metadata(
                    platform=platform,
                    model=model,
                    result=result,
                    text_length=len(brief_text or ""),
                    used_fallback=used_fallback,
                    repair_used=False,
                    quality_passed=recipe_quality_passed(result, settings),
                ),
                "resolver": resolver,
            },
        )
        recipe_card_id = repository.insert_recipe_card(
            job=job,
            result=result,
            model=model,
            prompt_version=PROMPT_VERSION,
        )
    except QueueJobFailure:
        raise
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
        "thumbnail_source": result.get("thumbnail_source") or "placeholder",
        "thumbnail_provider": result.get("thumbnail_provider") or result.get("platform"),
        "thumbnail_strategy": result.get("thumbnail_strategy") or "platform_first_v2",
        "candidate_count": result.get("thumbnail_candidate_count") or 0,
        "thumbnail_selected": bool(result.get("thumbnail_selected")),
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
    input_mode: str | None = None,
    analysis_mode: str | None = None,
    mime_type: str | None = None,
    file_state: str | None = None,
    text_length: int | None = None,
    candidate_count: int | None = None,
    parts_count: int | None = None,
    finish_reason: str | None = None,
    prompt_feedback_present: bool | None = None,
    safety_block_present: bool | None = None,
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
    if input_mode is not None:
        metadata["input_mode"] = input_mode
    if analysis_mode is not None:
        metadata["analysis_mode"] = analysis_mode
    if mime_type is not None:
        metadata["mime_type"] = mime_type
    if file_state is not None:
        metadata["file_state"] = file_state
    if text_length is not None:
        metadata["text_length"] = text_length
    if candidate_count is not None:
        metadata["candidate_count"] = candidate_count
    if parts_count is not None:
        metadata["parts_count"] = parts_count
    if finish_reason is not None:
        metadata["finish_reason"] = finish_reason
    if prompt_feedback_present is not None:
        metadata["prompt_feedback_present"] = prompt_feedback_present
    if safety_block_present is not None:
        metadata["safety_block_present"] = safety_block_present
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
    if stage == "recipe_quality_gate":
        return "recipe_quality_gate_failed"
    if stage == "job_completion_update":
        return "job_completion_update_failed"
    if stage == "mock_fixture_analysis":
        return "mock_fixture_failed"
    if stage == "metadata_fallback":
        return "metadata_unavailable"
    if stage == "gemini_file_upload":
        return "gemini_file_upload_failed"
    if stage == "gemini_file_processing":
        return "gemini_file_processing_failed"
    if stage == "gemini_video_brief":
        return "gemini_video_brief_failed"
    if stage == "gemini_recipe_json":
        return "gemini_recipe_json_invalid"
    if stage == "youtube_gemini_url":
        return "youtube_gemini_url_failed"
    if stage == "gemini_video_analysis":
        return "gemini_video_analysis_failed"
    return "queue_job_failed"


def safe_message_for_stage(stage: str) -> str:
    if stage == "recipe_card_saving":
        return "Recipe card could not be saved."
    if stage == "recipe_quality_gate":
        return "Could not extract enough recipe details from this video."
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
    if stage == "gemini_file_upload":
        return "Gemini video file upload failed."
    if stage == "gemini_file_processing":
        return "Gemini video file processing failed."
    if stage == "gemini_video_brief":
        return "Gemini video brief failed."
    if stage == "gemini_recipe_json":
        return "Gemini recipe JSON failed."
    if stage == "youtube_gemini_url":
        return "Gemini YouTube URL analysis failed."
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
