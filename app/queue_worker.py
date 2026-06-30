from __future__ import annotations

import asyncio
import io
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Callable
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
from app.schemas import GeminiRecipeResult, MediaJobRecord
from app.social.stream_download import extract_social_video
from app.social.thumbnail_upload import upload_source_thumbnail
from app.supabase_client import MediaJobRepository, create_repository

logger = logging.getLogger("recipe_dost.worker")

VIDEO_BRIEF_PROMPT = """Describe the recipe shown in this video as concise plain text.
Include recipe title, visible ingredients, likely quantities if inferable, cooking steps,
timing, cuisine/style, confidence, and uncertainties. Do not return JSON."""

RECIPE_JSON_PROMPT = """Convert this recipe brief into strict JSON only.
Required keys: title, summary, ingredients, steps, prep_time_minutes, cook_time_minutes,
servings, difficulty, cuisine, tags. Use null when unknown and arrays for ingredients,
steps, and tags.

Brief:
"""


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
    return {
        "success": True,
        "platform": "youtube",
        "resolver": "youtube_oembed",
        "title": body.get("title"),
        "description": body.get("title"),
        "author_username": body.get("author_name"),
        "thumbnail_url": body.get("thumbnail_url"),
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
    parsed = recipe_json_to_caption_shape(parsed)
    if save_mode != "local_brief_fallback":
        _validate_parsed_recipe(
            parsed,
            platform=platform,
            stage="gemini_recipe_json",
            model=text_model,
            user_message="Gemini did not return a usable recipe.",
        )
    result = _caption_result_to_recipe_result(parsed)
    return save_gemini_recipe_card(
        repository=repository,
        job=job,
        result=result,
        model=video_model,
        platform=platform,
        resolver="youtube_oembed",
        uploaded_thumbnail_url=uploaded_thumbnail_url,
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
    try:
        response = client.models.generate_content(
            model=model,
            contents=[uploaded_file, VIDEO_BRIEF_PROMPT],
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
    try:
        from google.genai import types

        uri_kwargs = {"file" + "_uri": youtube_url, "mime_type": "video/mp4"}
        video_part = types.Part.from_uri(**uri_kwargs)
        response = client.models.generate_content(
            model=model,
            contents=[video_part, VIDEO_BRIEF_PROMPT],
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
    return text


def generate_recipe_json(*, client: object, model: str, brief: str) -> dict:
    prompt = f"{RECIPE_JSON_PROMPT}{brief}"
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
            text = " ".join(
                str(item.get(key) or "").strip()
                for key in ("quantity", "name", "notes")
                if item.get(key)
            ).strip()
            if text:
                ingredients.append(text)
        elif str(item).strip():
            ingredients.append(str(item).strip())
    steps = []
    for item in parsed.get("steps") or []:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            if text:
                steps.append(text)
        elif str(item).strip():
            steps.append(str(item).strip())
    return {
        "title": str(parsed.get("title") or "AI Recipe from Video").strip(),
        "summary": str(parsed.get("summary") or "").strip(),
        "ingredients": ingredients,
        "steps": steps,
        "prep_time_minutes": parsed.get("prep_time_minutes"),
        "cook_time_minutes": parsed.get("cook_time_minutes"),
        "servings": parsed.get("servings"),
        "difficulty": parsed.get("difficulty"),
        "cuisine": parsed.get("cuisine"),
        "tags": parsed.get("tags") if isinstance(parsed.get("tags"), list) else [],
    }


def fallback_recipe_json_from_brief(
    brief_text: str,
    source_metadata: dict[str, object],
) -> dict:
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
    return {
        "title": title or "AI Recipe from Video",
        "summary": brief_text.strip(),
        "ingredients": [],
        "steps": [brief_text.strip()],
        "prep_time_minutes": None,
        "cook_time_minutes": None,
        "servings": None,
        "difficulty": None,
        "cuisine": None,
        "tags": tags,
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
                    generate_recipe_json(client=client, model=model_name, brief=brief_text)
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
        parsed = recipe_json_to_caption_shape(parsed)
        if save_mode != "local_brief_fallback":
            _validate_parsed_recipe(
                parsed,
                platform=platform,
                stage="gemini_recipe_json",
                model=text_model,
                user_message="Gemini did not return a usable recipe.",
            )
        parsed["save_mode"] = save_mode
        result = _caption_result_to_recipe_result(parsed)
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
