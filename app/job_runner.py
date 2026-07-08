from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from threading import Event
from time import perf_counter, sleep
from typing import Callable
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import requests

from app.ai.gemini_recipe import (
    TEMPORARY_GEMINI_ERRORS,
    GeminiAnalysisError,
    GeminiCallGuardError,
    GeminiRecipeAnalyzer,
)
from app.config import Settings, WorkerMode
from app.schemas import (
    GeminiRecipeResult,
    GroceryItem,
    MediaJobData,
    MediaJobRecord,
    RecipeIngredient,
    RecipeStep,
)
from app.social.platforms import detect_social_platform
from app.social.stream_download import VideoBuffer, safe_int, stream_video_to_memory
from app.social.thumbnail_selection import should_update_thumbnail
from app.social.thumbnail_upload import upload_source_thumbnail
from app.social.models import SocialResolverError, SocialVideoInfo
from app.social.resolver_orchestrator import resolve_social_video
from app.supabase_client import MediaJobRepository

logger = logging.getLogger("recipe_dost.worker")

SKELETON_ERROR_CODE = "worker_skeleton_no_media_provider"
SKELETON_ERROR_MESSAGE = (
    "Automatic video analysis is currently available for YouTube. "
    "Paste caption or visible ingredients for this source."
)

GEMINI_INVALID_JSON_MESSAGE = (
    "Gemini returned an invalid recipe response. Please try again."
)

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RESOLVING = "resolving"
JOB_STATUS_DOWNLOADING = "downloading"
JOB_STATUS_ANALYZING = "analyzing"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"

DB_SAFE_STATUSES = {
    JOB_STATUS_QUEUED,
    JOB_STATUS_RESOLVING,
    JOB_STATUS_DOWNLOADING,
    JOB_STATUS_ANALYZING,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
}

IDEMPOTENT_STATUSES = {
    JOB_STATUS_RESOLVING,
    JOB_STATUS_DOWNLOADING,
    JOB_STATUS_ANALYZING,
    JOB_STATUS_COMPLETED,
}

YOUTUBE_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


class JobNotFoundError(Exception):
    pass


class WorkerProcessingError(Exception):
    pass


class PipelineFailure(Exception):
    def __init__(
        self,
        code: str,
        user_message: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.user_message = user_message
        self.metadata = metadata or {}


CAPTION_RECIPE_PROMPT = """\
You are Recipe Dost, an AI recipe assistant.
Extract a recipe from the social media caption/title below.
Strict JSON only. No markdown fences. No commentary.

Do not invent ingredients, quantities, or steps that are not present.
If the text is not a recipe or is too thin, return is_recipe=false.

Return exactly this JSON shape:
{
  "is_recipe": true,
  "confidence": "high",
  "title": "string",
  "summary": "string",
  "ingredients": ["string"],
  "steps": ["string"],
  "servings": null,
  "time_minutes": null,
  "tags": ["string"]
}"""


def _extract_json_object(text: str) -> dict:
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        raise ValueError("No JSON object found in response.")
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : index + 1]
                try:
                    value = json.loads(candidate)
                    if isinstance(value, dict):
                        return value
                except json.JSONDecodeError:
                    pass
                break
    raise ValueError("No JSON object found in response.")


def _build_caption_recipe_prompt(
    caption: str,
    platform: str,
    duration_seconds: float | None,
) -> str:
    duration = "unknown" if duration_seconds is None else str(duration_seconds)
    return (
        f"{CAPTION_RECIPE_PROMPT}\n\n"
        f"Platform: {platform}\n"
        f"Duration seconds: {duration}\n\n"
        f"Caption/title:\n{caption.strip()}"
    )


def _gemini_caption_to_recipe_sync(
    *,
    api_key: str,
    model: str,
    caption: str,
    platform: str,
    duration_seconds: float | None,
    timeout_seconds: int = 45,
) -> dict:
    if not api_key:
        raise RuntimeError("gemini_api_key_missing")

    prompt = _build_caption_recipe_prompt(caption, platform, duration_seconds)
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    response = requests.post(url, headers=headers, json=payload, timeout=timeout_seconds)
    if response.status_code == 429:
        raise RuntimeError("gemini_rate_limited")
    if response.status_code >= 400:
        raise RuntimeError(f"gemini_api_error_{response.status_code}")

    body = response.json()
    try:
        text = body["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("gemini_api_error_missing_text") from exc

    return _extract_json_object(text)


def _caption_result_to_recipe_result(parsed: dict) -> GeminiRecipeResult:
    title = parsed.get("title", "Recipe")
    summary = parsed.get("summary", "")
    ingredients_raw = parsed.get("ingredients") or []
    steps_raw = parsed.get("steps") or []
    servings = parsed.get("servings")
    time_minutes = parsed.get("time_minutes")
    confidence = str(parsed.get("confidence", "medium")).strip().lower()
    tags = parsed.get("tags") or []

    recipe_ingredients = [
        RecipeIngredient(
            name_en=str(i).strip(),
            name_ur="",
            quantity="",
            notes_en="",
            notes_ur="",
        )
        for i in ingredients_raw
    ]

    recipe_steps = [
        RecipeStep(
            step_number=idx + 1,
            instruction_en=str(s).strip(),
            instruction_ur="",
            tip_en="",
            tip_ur="",
        )
        for idx, s in enumerate(steps_raw)
    ]

    grocery_items = [
        GroceryItem(name_en=str(i).strip(), name_ur="", quantity="")
        for i in ingredients_raw
    ]

    prep_time = None
    cook_time = None
    if isinstance(time_minutes, (int, float)) and time_minutes > 0:
        prep_time = int(time_minutes)

    difficulty = (
        confidence
        if confidence in {"easy", "medium", "hard"}
        else "unknown"
    )

    return GeminiRecipeResult(
        recipe_title_en=title,
        recipe_title_ur="",
        short_summary_en=summary,
        short_summary_ur="",
        confidence=confidence,
        prep_time_minutes=prep_time,
        cook_time_minutes=cook_time,
        servings=str(servings) if servings else None,
        difficulty=difficulty,
        ingredients=recipe_ingredients,
        steps=recipe_steps,
        grocery_list=grocery_items,
        missing_info_en=tags,
        missing_info_ur=[],
        storage_tip_en="",
        storage_tip_ur="",
        safety_note_en="",
        safety_note_ur="",
        source_assumptions_en=[],
        source_assumptions_ur=[],
    )


def _load_mock_recipe_fixture(fixture_path: str) -> dict:
    with open(fixture_path, encoding="utf-8") as f:
        value = json.load(f)
    if not isinstance(value, dict):
        raise ValueError("Mock recipe fixture must be a JSON object.")
    return value


def save_mock_recipe_card_for_job(
    *,
    repository: MediaJobRepository,
    job: MediaJobRecord,
    settings: Settings,
    platform: str,
    resolver: str,
    thumbnail_url: str | None,
    job_started_at: float,
    check_job_runtime: Callable[[UUID, float], None] | None = None,
    fixture_error_code: str = "mock_fixture_invalid",
) -> MediaJobRecord:
    model = "mock-recipe-fixture"
    fixture_path = settings.mock_recipe_fixture_path
    if check_job_runtime is not None:
        check_job_runtime(job.id, job_started_at)
    job = repository.update_job(
        job.id,
        status=JOB_STATUS_ANALYZING,
        progress=60,
        thumbnail_url=thumbnail_url,
        error_code=None,
        error_message=None,
        ai_provider="mock",
        ai_model=model,
    )
    repository.insert_event(
        job.id,
        event_type="mock_ai_analysis_started",
        message="Mock fixture AI analysis started.",
        metadata={
            "platform": platform,
            "resolver": resolver,
        },
    )
    if settings.mock_analysis_delay_ms > 0:
        sleep(settings.mock_analysis_delay_ms / 1000)
    try:
        parsed = _load_mock_recipe_fixture(fixture_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise PipelineFailure(
            fixture_error_code,
            "Mock recipe fixture could not be loaded.",
            {
                "platform": platform,
                "stage": "mock_fixture_analysis",
                "fixture_path": fixture_path,
                "exception_type": type(exc).__name__,
            },
        ) from exc

    _validate_parsed_recipe(
        parsed,
        platform=platform,
        stage="mock_fixture_analysis",
        model=model,
        user_message="Mock recipe fixture does not include enough recipe details.",
    )
    repository.insert_event(
        job.id,
        event_type="mock_ai_analysis_completed",
        message="Mock fixture AI analysis completed.",
        metadata={
            "platform": platform,
            "resolver": resolver,
            "title_present": bool(parsed.get("title")),
        },
    )
    result = _caption_result_to_recipe_result(parsed)
    if check_job_runtime is not None:
        check_job_runtime(job.id, job_started_at)
    job = repository.update_job(
        job.id,
        status=JOB_STATUS_ANALYZING,
        progress=85,
        thumbnail_url=thumbnail_url,
        error_code=None,
        error_message=None,
    )
    repository.insert_event(
        job.id,
        event_type="recipe_card_saving_started",
        message="Recipe card save started.",
        metadata={"stage": "recipe_card_saving", "progress": 85},
    )
    if job.recipe_item_id is None:
        raise PipelineFailure(
            "recipe_item_required",
            "A saved recipe is required before analysis can be completed.",
        )
    try:
        recipe_card_id = repository.insert_recipe_card(
            job=job,
            result=result,
            model=model,
        )
    except Exception as exc:
        raise PipelineFailure(
            "recipe_card_insert_failed",
            "Recipe card could not be saved.",
            {
                "platform": platform,
                "stage": "recipe_card_saving",
                "exception_type": type(exc).__name__,
            },
        ) from exc
    repository.insert_event(
        job.id,
        event_type="recipe_card_saved",
        message="Recipe card saved.",
        metadata={"stage": "recipe_card_saving"},
    )
    try:
        job = repository.update_job(
            job.id,
            status=JOB_STATUS_COMPLETED,
            progress=100,
            result_recipe_card_id=recipe_card_id,
            ai_provider="mock",
            ai_model=model,
            thumbnail_url=thumbnail_url,
            completed_at=datetime.now(timezone.utc).isoformat(),
            error_code=None,
            error_message=None,
        )
    except Exception as exc:
        raise PipelineFailure(
            "job_completion_update_failed",
            "Recipe card was saved, but the job could not be marked completed.",
            {
                "platform": platform,
                "stage": "job_completion_update",
                "exception_type": type(exc).__name__,
            },
        ) from exc
    return job


def _validate_parsed_recipe(
    parsed: dict,
    *,
    platform: str,
    stage: str,
    model: str,
    user_message: str,
) -> None:
    if parsed.get("is_recipe") is not True:
        raise PipelineFailure(
            "insufficient_recipe_details",
            user_message,
            {"platform": platform, "stage": stage, "model": model},
        )

    confidence = str(parsed.get("confidence", "medium")).strip().lower()
    if confidence == "low":
        raise PipelineFailure(
            "insufficient_recipe_details",
            user_message,
            {"platform": platform, "stage": stage, "model": model},
        )

    ingredients = parsed.get("ingredients") or []
    steps = parsed.get("steps") or []
    if not isinstance(ingredients, list) or len(ingredients) == 0:
        raise PipelineFailure(
            "insufficient_recipe_details",
            user_message,
            {"platform": platform, "stage": stage, "model": model},
        )
    if not isinstance(steps, list) or len(steps) == 0:
        raise PipelineFailure(
            "insufficient_recipe_details",
            user_message,
            {"platform": platform, "stage": stage, "model": model},
        )


VIDEO_RECIPE_PROMPT = """\
You are Recipe Dost, an AI recipe assistant.
Analyze this recipe video carefully.
Extract a recipe only if enough details are visible or spoken/on-screen.
Do not invent ingredients, quantities, or steps that are not present.
If the video does not contain enough recipe detail, return is_recipe=false.

Return exactly this JSON shape:
{
  "is_recipe": true,
  "confidence": "high",
  "title": "string",
  "summary": "string",
  "ingredients": ["string"],
  "steps": ["string"],
  "servings": null,
  "time_minutes": null,
  "tags": ["string"]
}"""


def _download_tiktok_to_temp(
    info: SocialVideoInfo,
    job_id: str,
    settings: Settings,
) -> tuple[str, str, int]:
    os.makedirs(settings.temp_media_dir, exist_ok=True)
    ext = (info.ext or "mp4").lstrip(".")
    file_path = os.path.join(settings.temp_media_dir, f"{job_id}.{ext}")
    max_size_bytes = settings.tikwm_max_video_size_mb * 1024 * 1024

    request_headers = dict(info.stream_headers)
    total = 0
    try:
        with requests.get(
            info.stream_url,
            stream=True,
            timeout=(10, 120),
            headers=request_headers,
            allow_redirects=True,
        ) as response:
            if response.status_code in {401, 403}:
                raise SocialResolverError(
                    "social_stream_forbidden",
                    "This social video stream could not be accessed.",
                    {"platform": info.platform, "resolver": info.resolver},
                )
            if response.status_code == 404:
                raise SocialResolverError(
                    "social_stream_expired",
                    "This social video stream expired before processing.",
                    {"platform": info.platform, "resolver": info.resolver},
                )
            response.raise_for_status()
            content_length = safe_int(response.headers.get("content-length"))
            if content_length is not None and content_length > max_size_bytes:
                raise SocialResolverError(
                    "media_size_exceeded",
                    "This video is too large for automatic recipe analysis.",
                    {"platform": info.platform, "resolver": info.resolver, "size_bytes": content_length},
                )
            mime_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            with open(file_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_size_bytes:
                        response.close()
                        raise SocialResolverError(
                            "media_size_exceeded",
                            "This video is too large for automatic recipe analysis.",
                            {"platform": info.platform, "resolver": info.resolver, "size_bytes": total},
                        )
                    f.write(chunk)
            if total == 0:
                raise SocialResolverError(
                    "media_download_failed",
                    "This social video could not be downloaded for analysis.",
                    {"platform": info.platform, "resolver": info.resolver},
                )
    except SocialResolverError:
        _safe_unlink(file_path)
        raise
    except Exception:
        _safe_unlink(file_path)
        raise
    return file_path, mime_type or info.mime_type or "video/mp4", total


def _gemini_upload_video_rest(
    *,
    api_key: str,
    file_path: str,
    mime_type: str,
    timeout_seconds: int = 90,
) -> tuple[str, str]:
    file_size = os.path.getsize(file_path)
    with open(file_path, "rb") as f:
        file_data = f.read()

    metadata = {
        "file": {
            "display_name": os.path.basename(file_path),
            "mime_type": mime_type,
        }
    }

    boundary = "recipe_dost_upload_boundary"
    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8") + file_data + f"\r\n--{boundary}--\r\n".encode("utf-8")

    upload_url = (
        "https://generativelanguage.googleapis.com/upload/v1beta/files"
        "?uploadType=multipart"
    )
    response = requests.post(
        upload_url,
        headers={
            "X-Goog-Api-Key": api_key,
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        data=body,
        timeout=timeout_seconds,
    )
    if response.status_code == 429:
        raise RuntimeError("gemini_rate_limited")
    if response.status_code >= 400:
        raise RuntimeError(f"gemini_api_error_{response.status_code}")
    body_json = response.json()
    file_entry = body_json.get("file", {})
    media_reference = file_entry.get("uri", "")
    file_name = file_entry.get("name", "")
    if not media_reference:
        raise RuntimeError("gemini_api_error_upload_no_uri")
    return media_reference, file_name


def _gemini_video_to_recipe_rest(
    *,
    api_key: str,
    model: str,
    media_reference: str,
    mime_type: str,
    prompt: str,
    timeout_seconds: int = 120,
) -> dict:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {
                        "fileData": {
                            "fileUri": media_reference,
                            "mimeType": mime_type,
                        }
                    },
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    response = requests.post(url, headers=headers, json=payload, timeout=timeout_seconds)
    if response.status_code == 429:
        raise RuntimeError("gemini_rate_limited")
    if response.status_code >= 400:
        raise RuntimeError(f"gemini_api_error_{response.status_code}")

    body = response.json()
    try:
        text = body["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("gemini_api_error_missing_text") from exc

    return _extract_json_object(text)


def _safe_unlink(file_path: str) -> None:
    try:
        os.unlink(file_path)
    except OSError:
        pass


def _gemini_delete_file_rest(*, api_key: str, file_name: str, timeout_seconds: int = 15) -> None:
    if not file_name:
        return
    try:
        requests.delete(
            f"https://generativelanguage.googleapis.com/v1beta/{file_name}",
            headers={"X-Goog-Api-Key": api_key},
            timeout=timeout_seconds,
        )
    except Exception:
        pass


class JobRunner:
    def __init__(
        self,
        repository: MediaJobRepository,
        analyzer: GeminiRecipeAnalyzer | None = None,
        worker_mode: WorkerMode = "skeleton",
        app_env: str = "development",
        gemini_max_calls_per_job: int = 2,
        gemini_daily_dev_call_limit: int = 5,
        gemini_disabled_models: list[str] | None = None,
        gemini_daily_dev_model_limits: dict[str, int] | None = None,
        gemini_dev_budget_reset_at: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._repository = repository
        self._analyzer = analyzer
        self._worker_mode = worker_mode
        self._app_env = app_env
        self._gemini_max_calls_per_job = max(0, gemini_max_calls_per_job)
        self._gemini_daily_dev_call_limit = max(0, gemini_daily_dev_call_limit)
        self._gemini_disabled_models = set(gemini_disabled_models or [])
        self._gemini_daily_dev_model_limits = gemini_daily_dev_model_limits or {}
        self._gemini_dev_budget_reset_at = gemini_dev_budget_reset_at
        self._settings = settings
        self._private_metadata_saved_job_ids: set[UUID] = set()

    def accept(
        self,
        media_job_id: UUID,
        *,
        force: bool = False,
        force_reset_attempt_budget: bool = False,
    ) -> tuple[MediaJobData, bool]:
        job = self._repository.get_job(media_job_id)
        if job is None:
            raise JobNotFoundError
        if job.status == JOB_STATUS_COMPLETED:
            return self._to_response(job), False
        if job.status in {
            JOB_STATUS_RESOLVING,
            JOB_STATUS_DOWNLOADING,
            JOB_STATUS_ANALYZING,
        } and not force:
            return self._to_response(job), False
        if job.status == JOB_STATUS_FAILED and not force:
            return self._to_response(job), False
        if job.status != JOB_STATUS_QUEUED and not force:
            return self._to_response(job), False
        if force_reset_attempt_budget and self._app_env == "development":
            self._repository.insert_event(
                media_job_id,
                event_type="dev_attempt_budget_reset_requested",
                message="Development attempt budget reset requested.",
                metadata={},
            )
        previous_status = job.status
        job = self._repository.update_job(
            media_job_id,
            status=JOB_STATUS_RESOLVING,
            progress=20,
            error_code=None,
            error_message=None,
            started_at=job.started_at or datetime.now(timezone.utc).isoformat(),
        )
        self._repository.insert_event(
            media_job_id,
            event_type="worker_started",
            message="Media worker accepted the job.",
            metadata={
                "status": JOB_STATUS_RESOLVING,
                "progress": 20,
                "previous_status": previous_status,
            },
        )
        data = self._to_response(job)
        data.accepted = True
        return data, True

    def run(
        self,
        media_job_id: UUID,
        *,
        force: bool = False,
        background_resume: bool = False,
    ) -> MediaJobData:
        try:
            job = self._repository.get_job(media_job_id)
            if job is None:
                raise JobNotFoundError

            if background_resume:
                force = True
            if job.status == JOB_STATUS_COMPLETED:
                return self._to_response(job)
            if job.status in {
                JOB_STATUS_RESOLVING,
                JOB_STATUS_DOWNLOADING,
                JOB_STATUS_ANALYZING,
            } and not force:
                self._repository.insert_event(
                    media_job_id,
                    event_type="worker_already_processing",
                    message="Duplicate worker request skipped because the job is already processing.",
                    metadata={"status": job.status},
                )
                return self._to_response(job)
            if job.status == JOB_STATUS_FAILED and not force:
                self._repository.insert_event(
                    media_job_id,
                    event_type="duplicate_job_skipped",
                    message="Duplicate worker request skipped because force retry was not requested.",
                    metadata={"status": job.status},
                )
                return self._to_response(job)
            if job.status != JOB_STATUS_QUEUED and not force:
                return self._to_response(job)

            previous_status = job.status
            job = self._repository.update_job(
                media_job_id,
                status=JOB_STATUS_RESOLVING,
                progress=20,
                error_code=None,
                error_message=None,
                started_at=job.started_at or datetime.now(timezone.utc).isoformat(),
            )
            self._repository.insert_event(
                media_job_id,
                event_type="worker_started",
                message="Media worker accepted the job.",
                metadata={
                    "status": JOB_STATUS_RESOLVING,
                    "progress": 20,
                    "force": force,
                    "previous_status": previous_status,
                },
            )

            platform = platform_for_job(job)
            job_started_at = perf_counter()
            if platform == "tiktok":
                job = self._run_tiktok_text_first(job, job_started_at)
            elif platform == "youtube":
                if self._settings and self._settings.ai_analysis_mode == "mock_fixture":
                    job = self._run_mock_resolver_job(job, platform, job_started_at)
                else:
                    job = self._run_youtube(job, job_started_at)
            elif platform in {"instagram", "facebook"}:
                if self._settings and self._settings.ai_analysis_mode == "mock_fixture":
                    job = self._run_mock_resolver_job(job, platform, job_started_at)
                else:
                    job = self._run_social_video(job, platform, job_started_at)
            else:
                job = self._run_skeleton(job)

            return self._to_response(job)
        except JobNotFoundError:
            raise
        except PipelineFailure as exc:
            self._log_job_failure(media_job_id, type(exc).__name__)
            self._record_failure(
                media_job_id,
                error_code=exc.code,
                error_message=exc.user_message,
                exception_type=type(exc).__name__,
                metadata=exc.metadata,
            )
            failed_job = self._repository.get_job(media_job_id)
            if failed_job is not None:
                return self._to_response(failed_job)
            raise WorkerProcessingError from exc
        except Exception as exc:
            self._log_job_failure(media_job_id, type(exc).__name__)
            error_code = "job_update_failed" if is_postgrest_api_error(exc) else "worker_error"
            error_message = (
                "Media job update failed."
                if error_code == "job_update_failed"
                else "Media worker failed."
            )
            self._record_failure(
                media_job_id,
                error_code=error_code,
                error_message=error_message,
                exception_type=type(exc).__name__,
            )
            raise WorkerProcessingError from exc

    def _run_tiktok_text_first(
        self,
        job: MediaJobRecord,
        job_started_at: float,
    ) -> MediaJobRecord:
        settings = self._settings
        if settings is None:
            raise PipelineFailure(
                "social_video_not_enabled",
                "Social video analysis is not enabled yet.",
                {"platform": "tiktok"},
            )
        self._check_job_runtime(job.id, job_started_at)
        job = self._repository.update_job(
            job.id,
            status=JOB_STATUS_RESOLVING,
            progress=20,
            error_code=None,
            error_message=None,
        )
        self._repository.insert_event(
            job.id,
            event_type="tikwm_resolver_started",
            message="TikWM resolver started.",
            metadata={"platform": "tiktok", "resolver": "tikwm"},
        )
        resolver_started_at = perf_counter()
        try:
            info = resolve_social_video(
                job.source_url,
                "tiktok",
                settings,
                resolver="tikwm",
            )
        except SocialResolverError as exc:
            self._repository.insert_event(
                job.id,
                event_type="tikwm_resolver_failed",
                message="TikWM resolver failed.",
                metadata={
                    "platform": "tiktok",
                    "resolver": "tikwm",
                    "error_code": exc.code,
                    "exception_type": type(exc).__name__,
                    "elapsed_ms": int((perf_counter() - resolver_started_at) * 1000),
                    **safe_event_metadata(exc.metadata),
                },
            )
            raise PipelineFailure(
                exc.code,
                exc.message,
                {"platform": "tiktok", **exc.metadata},
            ) from exc
        except Exception as exc:
            self._repository.insert_event(
                job.id,
                event_type="tikwm_resolver_failed",
                message="TikWM resolver failed.",
                metadata={
                    "platform": "tiktok",
                    "resolver": "tikwm",
                    "error_code": "tikwm_resolver_exception",
                    "exception_type": type(exc).__name__,
                    "elapsed_ms": int((perf_counter() - resolver_started_at) * 1000),
                },
            )
            raise PipelineFailure(
                "tikwm_resolver_exception",
                "TikTok metadata could not be resolved.",
                {
                    "platform": "tiktok",
                    "resolver": "tikwm",
                    "exception_type": type(exc).__name__,
                },
            ) from exc

        self._repository.insert_event(
            job.id,
            event_type="tikwm_resolver_completed",
            message="TikWM resolver completed.",
            metadata={
                "platform": "tiktok",
                "resolver": "tikwm",
                "title_present": bool(info.title),
                "has_thumbnail": bool(info.thumbnail_url),
                "duration_seconds": info.duration_seconds,
                "elapsed_ms": int((perf_counter() - resolver_started_at) * 1000),
            },
        )
        self._update_social_metadata_best_effort(job, info)

        if (
            info.duration_seconds is not None
            and info.duration_seconds > settings.max_video_duration_seconds
        ):
            self._repository.insert_event(
                job.id,
                event_type="video_too_long",
                message="Video duration exceeded the configured limit.",
                metadata={
                    "platform": "tiktok",
                    "resolver": "tikwm",
                    "duration_seconds": info.duration_seconds,
                    "max_video_duration_seconds": settings.max_video_duration_seconds,
                },
            )
            raise PipelineFailure(
                "video_duration_exceeded",
                (
                    "This video is longer than the current 90 second limit. "
                    "Please try a shorter recipe video."
                ),
                {
                    "platform": "tiktok",
                    "resolver": "tikwm",
                    "duration_seconds": info.duration_seconds,
                    "max_video_duration_seconds": settings.max_video_duration_seconds,
                },
            )

        if settings.ai_analysis_mode == "mock_fixture":
            return self._run_social_text_analysis(
                job,
                info,
                settings,
                job_started_at,
            )

        if self._should_use_tiktok_text_first(info, settings):
            try:
                return self._run_social_text_analysis(
                    job,
                    info,
                    settings,
                    job_started_at,
                )
            except PipelineFailure as exc:
                if exc.code == "insufficient_recipe_details" and settings.enable_social_video_gemini:
                    self._repository.insert_event(
                        job.id,
                        event_type="social_text_insufficient_video_fallback_started",
                        message="Text analysis insufficient. Falling back to video Gemini.",
                        metadata={
                            "platform": "tiktok",
                            "resolver": info.resolver,
                            "stage": "video_fallback",
                        },
                    )
                    return self._run_tiktok_video_fallback(
                        job,
                        info,
                        settings,
                        job_started_at,
                    )
                raise

        raise PipelineFailure(
            "social_caption_missing",
            (
                "This TikTok video could not be analyzed from its caption. "
                "Please paste the caption or try another recipe video."
            ),
            {
                "platform": "tiktok",
                "resolver": "tikwm",
                "title_present": bool(info.title),
            },
        )

    def _run_mock_resolver_job(
        self,
        job: MediaJobRecord,
        platform: str,
        job_started_at: float,
    ) -> MediaJobRecord:
        settings = self._settings
        if settings is None:
            raise PipelineFailure(
                "social_video_not_enabled",
                "Social video analysis is not enabled yet.",
                {"platform": platform},
            )
        resolver_name = configured_resolver_for_platform(platform, settings)
        self._check_job_runtime(job.id, job_started_at)
        job = self._repository.update_job(
            job.id,
            status=JOB_STATUS_RESOLVING,
            progress=20,
            error_code=None,
            error_message=None,
        )
        self._repository.insert_event(
            job.id,
            event_type="platform_resolver_started",
            message="Platform resolver started.",
            metadata={"platform": platform, "resolver": resolver_name},
        )
        try:
            info = resolve_social_video(
                job.source_url,
                platform,
                settings,
                resolver=resolver_name,
            )
        except SocialResolverError as exc:
            failed_resolver = str(exc.metadata.get("resolver") or resolver_name)
            self._repository.insert_event(
                job.id,
                event_type="platform_resolver_failed",
                message="Platform resolver failed.",
                metadata={
                    "platform": platform,
                    "resolver": failed_resolver,
                },
            )
            raise PipelineFailure(
                exc.code,
                exc.message,
                {"platform": platform, **safe_event_metadata(exc.metadata)},
            ) from exc

        self._repository.insert_event(
            job.id,
            event_type="platform_resolver_completed",
            message="Platform resolver completed.",
            metadata=social_metadata_event_metadata(info),
        )
        self._update_social_metadata_best_effort(job, info)
        if (
            info.duration_seconds is not None
            and info.duration_seconds > settings.max_video_duration_seconds
        ):
            self._repository.insert_event(
                job.id,
                event_type="video_too_long",
                message="Video duration exceeded the configured limit.",
                metadata={
                    "platform": platform,
                    "resolver": info.resolver,
                    "duration_seconds": info.duration_seconds,
                    "max_video_duration_seconds": settings.max_video_duration_seconds,
                },
            )
            raise PipelineFailure(
                "video_duration_exceeded",
                (
                    "This video is longer than the current 90 second limit. "
                    "Please try a shorter recipe video."
                ),
                {
                    "platform": platform,
                    "resolver": info.resolver,
                    "duration_seconds": info.duration_seconds,
                    "max_video_duration_seconds": settings.max_video_duration_seconds,
                },
            )
        thumbnail_url = self._upload_social_thumbnail_best_effort(
            job,
            info.thumbnail_url,
            settings,
            info=info,
        )
        return self._run_mock_fixture_analysis(
            job,
            info,
            settings,
            job_started_at,
            thumbnail_url,
        )

    def _run_social_video(
        self,
        job: MediaJobRecord,
        platform: str,
        job_started_at: float,
    ) -> MediaJobRecord:
        settings = self._settings
        if settings is None:
            raise PipelineFailure(
                "social_video_not_enabled",
                "Social video analysis is not enabled yet.",
                {"platform": platform},
            )
        if platform == "instagram" and not settings.enable_render_instagram_resolver:
            raise PipelineFailure(
                "instagram_resolver_disabled",
                "Instagram processing is not enabled yet.",
                {"platform": platform, "resolver": "disabled"},
            )
        if platform == "facebook" and not settings.enable_render_facebook_resolver:
            raise PipelineFailure(
                "facebook_resolver_disabled",
                "Facebook processing is not enabled yet.",
                {"platform": platform, "resolver": "disabled"},
            )
        if not settings.enable_social_video:
            raise PipelineFailure(
                "social_video_not_enabled",
                "Social video analysis is not enabled yet.",
                {"platform": platform},
            )
        if self._analyzer is None:
            raise PipelineFailure("gemini_not_configured", "Gemini video analysis is not configured yet.")

        resolver_name = expected_resolver_for_platform(platform)
        self._check_job_runtime(job.id, job_started_at)
        self._repository.insert_event(
            job.id,
            event_type=f"{resolver_name}_resolver_started",
            message=f"{resolver_name} resolver started.",
            metadata={
                "platform": platform,
                "resolver": resolver_name,
            },
        )
        job = self._repository.update_job(job.id, status=JOB_STATUS_RESOLVING, progress=20, error_code=None, error_message=None)
        resolver_started_at = perf_counter()
        try:
            info = resolve_social_video(
                job.source_url,
                platform,
                settings,
            )
        except SocialResolverError as exc:
            failed_resolver = str(exc.metadata.get("resolver") or resolver_name)
            self._repository.insert_event(
                job.id,
                event_type=f"{failed_resolver}_resolver_failed",
                message=f"{failed_resolver} resolver failed.",
                metadata={
                    "platform": platform,
                    "resolver": failed_resolver,
                    "error_code": exc.code,
                    "exception_type": type(exc).__name__,
                    "elapsed_ms": int((perf_counter() - resolver_started_at) * 1000),
                    **safe_event_metadata(exc.metadata),
                },
            )
            raise PipelineFailure(exc.code, exc.message, {"platform": platform, **exc.metadata}) from exc
        self._repository.insert_event(
            job.id,
            event_type=f"{info.resolver}_resolver_completed",
            message=f"{info.resolver} resolver completed.",
            metadata={
                **_social_metadata(info),
                "elapsed_ms": int((perf_counter() - resolver_started_at) * 1000),
            },
        )
        self._update_social_metadata_best_effort(job, info)
        if (
            info.duration_seconds is not None
            and info.duration_seconds > settings.max_video_duration_seconds
        ):
            self._repository.insert_event(
                job.id,
                event_type="video_too_long",
                message="Video duration exceeded the configured limit.",
                metadata={
                    "platform": platform,
                    "resolver": info.resolver,
                    "duration_seconds": info.duration_seconds,
                    "max_video_duration_seconds": settings.max_video_duration_seconds,
                },
            )
            raise PipelineFailure(
                "video_duration_exceeded",
                (
                    "This video is longer than the current 90 second limit. "
                    "Please try a shorter recipe video."
                ),
                {
                    "platform": platform,
                    "resolver": info.resolver,
                    "duration_seconds": info.duration_seconds,
                    "max_video_duration_seconds": settings.max_video_duration_seconds,
                },
            )
        if platform == "tiktok":
            if self._should_use_tiktok_text_first(info, settings):
                return self._run_social_text_analysis(
                    job,
                    info,
                    settings,
                    job_started_at,
                )
            raise PipelineFailure(
                "social_caption_missing",
                (
                    "This TikTok video could not be analyzed from its caption. "
                    "Please paste the caption or try another recipe video."
                ),
                {
                    "platform": platform,
                    "resolver": info.resolver,
                    "title_present": bool(info.title),
                },
            )
        if not settings.enable_social_video_gemini:
            raise PipelineFailure(
                "social_video_gemini_disabled",
                "Social video analysis is not enabled for this worker.",
                {"platform": platform, "resolver": info.resolver},
            )

        self._check_job_runtime(job.id, job_started_at)
        job = self._repository.update_job(job.id, status=JOB_STATUS_DOWNLOADING, progress=35, error_code=None, error_message=None)
        self._repository.insert_event(job.id, event_type="social_stream_started", message="Social video stream started.", metadata={"platform": platform, "resolver": info.resolver, "ext": info.ext, "duration_known": info.duration_seconds is not None})
        video: VideoBuffer | None = None
        try:
            try:
                video = stream_video_to_memory(
                    info,
                    settings,
                    max_size_mb=(
                        settings.tikwm_max_video_size_mb
                        if info.resolver == "tikwm"
                        else None
                    ),
                )
            except SocialResolverError as exc:
                self._repository.insert_event(
                    job.id,
                    event_type="social_stream_failed",
                    message="Social video stream failed.",
                    metadata={
                        "platform": platform,
                        "resolver": info.resolver,
                        "error_code": exc.code,
                        "ext": info.ext,
                        **safe_event_metadata(exc.metadata),
                    },
                )
                raise PipelineFailure(
                    exc.code,
                    exc.message,
                    {"platform": platform, **exc.metadata},
                ) from exc
            self._repository.insert_event(job.id, event_type="social_stream_completed", message="Social video stream completed.", metadata={"platform": platform, "resolver": info.resolver, "size_bytes": video.size_bytes, "mime_type": video.mime_type, "ext": info.ext, "duration_known": info.duration_seconds is not None})

            self._check_job_runtime(job.id, job_started_at)
            job = self._repository.update_job(job.id, status=JOB_STATUS_DOWNLOADING, progress=45, error_code=None, error_message=None)
            thumbnail_url = self._upload_social_thumbnail_best_effort(
                job,
                info.thumbnail_url,
                settings,
                info=info,
            )

            self._filter_model_candidates(job.id)
            self._check_job_runtime(job.id, job_started_at)
            job = self._repository.update_job(job.id, status=JOB_STATUS_ANALYZING, progress=50, thumbnail_url=thumbnail_url, error_code=None, error_message=None, ai_provider="gemini", ai_model=self._analyzer.model)
            upload_mode = settings.gemini_social_upload_mode
            self._repository.insert_event(job.id, event_type="gemini_social_video_started", message="Gemini social video analysis started.", metadata={"model": self._analyzer.model, "stage": "video_observation", "mime_type": video.mime_type, "size_bytes": video.size_bytes})
            self._repository.insert_event(job.id, event_type="video_observation_started", message="Gemini video observation started.", metadata={"model": self._analyzer.model, "mime_type": video.mime_type, "size_bytes": video.size_bytes})
            try:
                outcome = self._run_social_video_gemini_call(
                    job,
                    video,
                    thumbnail_url,
                    upload_mode,
                    settings,
                )
            except GeminiCallGuardError as exc:
                raise PipelineFailure(exc.code, exc.message, exc.metadata) from exc
            except GeminiAnalysisError as exc:
                raise PipelineFailure(exc.code, gemini_user_message(exc.code), {"stage": "gemini_social_video", "attempted_models": exc.attempted_models, "final_model": exc.final_model, "attempts_count": exc.attempts_count, "provider_error_type": exc.provider_error_type}) from exc
            self._repository.insert_event(job.id, event_type="gemini_social_video_completed", message="Gemini social video analysis completed.", metadata={"model": outcome.model, "stage": "recipe_normalization"})

            self._check_job_runtime(job.id, job_started_at)
            job = self._repository.update_job(job.id, status=JOB_STATUS_ANALYZING, progress=85, error_code=None, error_message=None)
            self._repository.insert_event(job.id, event_type="recipe_card_saving_started", message="Recipe card save started.", metadata={"stage": "recipe_card_saving", "progress": 85})
            if job.recipe_item_id is None:
                raise PipelineFailure("recipe_item_required", "A saved recipe is required before analysis can be completed.")
            recipe_card_id = self._repository.insert_recipe_card(job=job, result=outcome.result, model=outcome.model)
            self._repository.insert_event(job.id, event_type="recipe_card_saved", message="Recipe card saved.", metadata={"stage": "recipe_card_saving"})
            job = self._repository.update_job(job.id, status=JOB_STATUS_COMPLETED, progress=100, result_recipe_card_id=recipe_card_id, ai_provider="gemini", ai_model=outcome.model, thumbnail_url=thumbnail_url, completed_at=datetime.now(timezone.utc).isoformat(), error_code=None, error_message=None)
            self._repository.insert_event(job.id, event_type="completed", message="Social video recipe analysis completed.", metadata={"platform": platform, "model": outcome.model, "status": JOB_STATUS_COMPLETED})
            return job
        finally:
            if video is not None:
                video.buffer.close()

    def _run_social_video_gemini_call(
        self,
        job: MediaJobRecord,
        video: VideoBuffer,
        thumbnail_url: str | None,
        upload_mode: str,
        settings: Settings,
    ):
        if self._analyzer is None:
            raise PipelineFailure(
                "gemini_not_configured",
                "Gemini video analysis is not configured yet.",
            )
        cancelled = Event()
        started_at = perf_counter()
        metadata = {
            "stage": "video_observation",
            "model": self._analyzer.model,
            "mime_type": video.mime_type,
            "size_bytes": video.size_bytes,
            "timeout_seconds": settings.social_video_gemini_timeout_seconds,
        }
        self._repository.insert_event(
            job.id,
            event_type="gemini_video_call_started",
            message="Gemini social video call started.",
            metadata=metadata,
        )

        def on_event(event_type: str, event_metadata: dict[str, object]) -> None:
            if not cancelled.is_set():
                self._handle_gemini_event(
                    job.id,
                    event_type,
                    event_metadata,
                    thumbnail_url,
                )

        def before_call(model: str, stage: str) -> dict[str, object]:
            if cancelled.is_set():
                raise RuntimeError("Gemini social video call was cancelled.")
            return self._start_gemini_api_call(job.id, model=model, stage=stage)

        def call():
            if upload_mode == "files_api":
                return self._analyzer.analyze_video_file(
                    video.buffer,
                    video.mime_type,
                    video.size_bytes,
                    on_event=on_event,
                    before_call=before_call,
                )
            self._repository.update_job(
                job.id,
                status=JOB_STATUS_ANALYZING,
                progress=60,
                thumbnail_url=thumbnail_url,
                error_code=None,
                error_message=None,
                ai_provider="gemini",
                ai_model=self._analyzer.model,
            )
            self._repository.insert_event(
                job.id,
                event_type="gemini_inline_video_started",
                message="Gemini inline video analysis started.",
                metadata={
                    "model": self._analyzer.model,
                    "stage": "video_observation",
                    "mime_type": video.mime_type,
                    "size_bytes": video.size_bytes,
                },
            )
            return self._analyzer.analyze_video_bytes(
                video.buffer.getvalue(),
                video.mime_type,
                on_event=on_event,
                before_call=before_call,
            )

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(call)
        try:
            outcome = future.result(timeout=settings.social_video_gemini_timeout_seconds)
            self._repository.insert_event(
                job.id,
                event_type="gemini_video_call_completed",
                message="Gemini social video call completed.",
                metadata={
                    **metadata,
                    "model": outcome.model,
                    "elapsed_ms": int((perf_counter() - started_at) * 1000),
                },
            )
            return outcome
        except FutureTimeoutError as exc:
            cancelled.set()
            self._repository.insert_event(
                job.id,
                event_type="gemini_video_call_timeout",
                message="Gemini social video call timed out.",
                metadata={
                    **metadata,
                    "elapsed_ms": int((perf_counter() - started_at) * 1000),
                    "error_code": "gemini_video_timeout",
                    "exception_type": type(exc).__name__,
                },
            )
            raise PipelineFailure(
                "gemini_video_timeout",
                "Video analysis took too long. Please try a shorter video or paste the caption.",
                {
                    "stage": "video_observation",
                    "final_model": self._analyzer.model,
                    "elapsed_ms": int((perf_counter() - started_at) * 1000),
                },
            ) from exc
        except GeminiCallGuardError:
            cancelled.set()
            raise
        except GeminiAnalysisError as exc:
            cancelled.set()
            elapsed_ms = int((perf_counter() - started_at) * 1000)
            if exc.code in {
                "gemini_video_observation_timeout",
                "gemini_text_normalization_timeout",
                "gemini_timeout",
            }:
                self._repository.insert_event(
                    job.id,
                    event_type="gemini_video_call_timeout",
                    message="Gemini social video call timed out.",
                    metadata={
                        **metadata,
                        "elapsed_ms": elapsed_ms,
                        "error_code": "gemini_video_timeout",
                        "exception_type": exc.provider_error_type,
                    },
                )
                raise PipelineFailure(
                    "gemini_video_timeout",
                    "Video analysis took too long. Please try a shorter video or paste the caption.",
                    {
                        "stage": "video_observation",
                        "final_model": exc.final_model,
                        "elapsed_ms": elapsed_ms,
                    },
                ) from exc
            self._repository.insert_event(
                job.id,
                event_type="gemini_video_call_failed",
                message="Gemini social video call failed.",
                metadata={
                    **metadata,
                    "elapsed_ms": elapsed_ms,
                    "error_code": exc.code,
                    "exception_type": exc.provider_error_type,
                },
            )
            raise
        except Exception as exc:
            cancelled.set()
            self._repository.insert_event(
                job.id,
                event_type="gemini_video_call_failed",
                message="Gemini social video call failed.",
                metadata={
                    **metadata,
                    "elapsed_ms": int((perf_counter() - started_at) * 1000),
                    "error_code": "gemini_video_bytes_failed",
                    "exception_type": type(exc).__name__,
                },
            )
            raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _should_use_tiktok_text_first(
        self,
        info: SocialVideoInfo,
        settings: Settings,
    ) -> bool:
        title = (info.title or "").strip()
        return (
            info.platform == "tiktok"
            and info.resolver == "tikwm"
            and settings.enable_tiktok_text_fallback
            and settings.social_analysis_mode == "text_first"
            and len(title) >= 10
        )

    def _run_social_text_analysis(
        self,
        job: MediaJobRecord,
        info: SocialVideoInfo,
        settings: Settings,
        job_started_at: float,
    ) -> MediaJobRecord:
        self._check_job_runtime(job.id, job_started_at)
        video_duration_exceeded = (
            info.duration_seconds is not None
            and info.duration_seconds > settings.max_video_duration_seconds
        )
        thumbnail_url = self._upload_social_thumbnail_best_effort(
            job,
            info.thumbnail_url,
            settings,
            info=info,
        )
        if settings.ai_analysis_mode == "mock_fixture":
            return self._run_mock_fixture_analysis(
                job,
                info,
                settings,
                job_started_at,
                thumbnail_url,
            )

        if settings.gemini_api_key is None:
            raise PipelineFailure(
                "gemini_not_configured",
                "Gemini video analysis is not configured yet.",
            )
        self._filter_model_candidates(job.id)
        model = settings.effective_gemini_text_model
        job = self._repository.update_job(
            job.id,
            status=JOB_STATUS_ANALYZING,
            progress=60,
            thumbnail_url=thumbnail_url,
            error_code=None,
            error_message=None,
            ai_provider="gemini",
            ai_model=model,
        )
        self._repository.insert_event(
            job.id,
            event_type="social_text_analysis_started",
            message="TikTok caption/title text analysis started.",
            metadata={
                "platform": info.platform,
                "resolver": info.resolver,
                "title_present": True,
                "analysis_mode": "text_first",
                "video_duration_exceeded": video_duration_exceeded,
            },
        )

        api_key = settings.gemini_api_key.get_secret_value()
        try:
            self._start_gemini_api_call(
                job.id,
                model=model,
                stage="social_text_analysis",
            )
        except GeminiCallGuardError as exc:
            raise PipelineFailure(exc.code, exc.message, exc.metadata) from exc

        call_started_at = perf_counter()
        try:
            parsed = _gemini_caption_to_recipe_sync(
                api_key=api_key,
                model=model,
                caption=(info.title or "").strip(),
                platform=info.platform,
                duration_seconds=info.duration_seconds,
                timeout_seconds=settings.social_text_gemini_timeout_seconds,
            )
        except requests.Timeout:
            elapsed_ms = int((perf_counter() - call_started_at) * 1000)
            self._repository.insert_event(
                job.id,
                event_type="gemini_api_call_failed",
                message="Gemini API call failed.",
                metadata={
                    "model": model,
                    "stage": "social_text_analysis",
                    "elapsed_ms": elapsed_ms,
                    "error_code": "gemini_text_timeout",
                    "exception_type": "Timeout",
                },
            )
            raise PipelineFailure(
                "gemini_text_timeout",
                "Recipe analysis took too long. Please try again or paste the caption manually.",
                {"platform": info.platform, "stage": "social_text_analysis", "model": model},
            )
        except RuntimeError as exc:
            error_msg = str(exc)
            elapsed_ms = int((perf_counter() - call_started_at) * 1000)
            if error_msg == "gemini_rate_limited":
                error_code = "gemini_rate_limited"
                user_message = "Recipe analysis is temporarily rate-limited. Please try again."
            elif error_msg.startswith("gemini_api_error_"):
                error_code = "gemini_api_error"
                user_message = "Recipe analysis is temporarily unavailable. Please try again."
            elif error_msg == "gemini_api_key_missing":
                error_code = "gemini_api_key_missing"
                user_message = "Recipe analysis is not configured."
            else:
                error_code = "gemini_api_error"
                user_message = "Recipe analysis is temporarily unavailable. Please try again."
            self._repository.insert_event(
                job.id,
                event_type="gemini_api_call_failed",
                message="Gemini API call failed.",
                metadata={
                    "model": model,
                    "stage": "social_text_analysis",
                    "elapsed_ms": elapsed_ms,
                    "error_code": error_code,
                    "exception_type": type(exc).__name__,
                },
            )
            raise PipelineFailure(
                error_code,
                user_message,
                {"platform": info.platform, "stage": "social_text_analysis", "model": model},
            )
        except ValueError:
            elapsed_ms = int((perf_counter() - call_started_at) * 1000)
            self._repository.insert_event(
                job.id,
                event_type="gemini_api_call_failed",
                message="Gemini API call failed.",
                metadata={
                    "model": model,
                    "stage": "social_text_analysis",
                    "elapsed_ms": elapsed_ms,
                    "error_code": "gemini_api_error",
                    "exception_type": "ValueError",
                },
            )
            raise PipelineFailure(
                "gemini_api_error",
                "Recipe analysis is temporarily unavailable. Please try again.",
                {"platform": info.platform, "stage": "social_text_analysis", "model": model},
            )

        self._repository.insert_event(
            job.id,
            event_type="gemini_api_call_completed",
            message="Gemini API call completed.",
            metadata={
                "model": model,
                "stage": "social_text_analysis",
                "elapsed_ms": int((perf_counter() - call_started_at) * 1000),
            },
        )

        is_recipe = parsed.get("is_recipe")
        if is_recipe is not True:
            raise PipelineFailure(
                "insufficient_recipe_details",
                "The caption does not include enough recipe details. Please paste the recipe caption or try another video.",
                {"platform": info.platform, "stage": "social_text_analysis", "model": model},
            )

        confidence = str(parsed.get("confidence", "medium")).strip().lower()
        if confidence == "low":
            raise PipelineFailure(
                "insufficient_recipe_details",
                "The caption does not include enough recipe details. Please paste the recipe caption or try another video.",
                {"platform": info.platform, "stage": "social_text_analysis", "model": model},
            )

        ingredients = parsed.get("ingredients") or []
        steps = parsed.get("steps") or []
        if not isinstance(ingredients, list) or len(ingredients) == 0:
            raise PipelineFailure(
                "insufficient_recipe_details",
                "The caption does not include enough recipe details. Please paste the recipe caption or try another video.",
                {"platform": info.platform, "stage": "social_text_analysis", "model": model},
            )
        if not isinstance(steps, list) or len(steps) == 0:
            raise PipelineFailure(
                "insufficient_recipe_details",
                "The caption does not include enough recipe details. Please paste the recipe caption or try another video.",
                {"platform": info.platform, "stage": "social_text_analysis", "model": model},
            )

        result = _caption_result_to_recipe_result(parsed)

        self._check_job_runtime(job.id, job_started_at)
        job = self._repository.update_job(
            job.id,
            status=JOB_STATUS_ANALYZING,
            progress=85,
            thumbnail_url=thumbnail_url,
            error_code=None,
            error_message=None,
        )
        self._repository.insert_event(
            job.id,
            event_type="recipe_card_saving_started",
            message="Recipe card save started.",
            metadata={"stage": "recipe_card_saving", "progress": 85},
        )
        if job.recipe_item_id is None:
            raise PipelineFailure(
                "recipe_item_required",
                "A saved recipe is required before analysis can be completed.",
            )
        recipe_card_id = self._repository.insert_recipe_card(
            job=job,
            result=result,
            model=model,
        )
        self._repository.insert_event(
            job.id,
            event_type="recipe_card_saved",
            message="Recipe card saved.",
            metadata={"stage": "recipe_card_saving"},
        )
        job = self._repository.update_job(
            job.id,
            status=JOB_STATUS_COMPLETED,
            progress=100,
            result_recipe_card_id=recipe_card_id,
            ai_provider="gemini",
            ai_model=model,
            thumbnail_url=thumbnail_url,
            completed_at=datetime.now(timezone.utc).isoformat(),
            error_code=None,
            error_message=None,
        )
        self._repository.insert_event(
            job.id,
            event_type="completed",
            message="Social text recipe analysis completed.",
            metadata={
                "platform": info.platform,
                "resolver": info.resolver,
                "model": model,
                "status": JOB_STATUS_COMPLETED,
                "analysis_mode": "text_first",
            },
        )
        return job

    def _run_mock_fixture_analysis(
        self,
        job: MediaJobRecord,
        info: SocialVideoInfo,
        settings: Settings,
        job_started_at: float,
        thumbnail_url: str | None,
    ) -> MediaJobRecord:
        model = "mock-recipe-fixture"
        fixture_path = settings.mock_recipe_fixture_path
        if settings.mock_video_processing_mode == "download_then_mock":
            file_path: str | None = None
            self._check_job_runtime(job.id, job_started_at)
            job = self._repository.update_job(
                job.id,
                status=JOB_STATUS_DOWNLOADING,
                progress=35,
                thumbnail_url=thumbnail_url,
                error_code=None,
                error_message=None,
            )
            self._repository.insert_event(
                job.id,
                event_type="social_video_download_started",
                message="Social video download started.",
                metadata={
                    "platform": info.platform,
                    "resolver": info.resolver,
                    "stage": "mock_video_download",
                    "ai_analysis_mode": "mock_fixture",
                },
            )
            download_started_at = perf_counter()
            try:
                file_path, _mime_type, size_bytes = _download_tiktok_to_temp(
                    info,
                    str(job.id),
                    settings,
                )
            except SocialResolverError as exc:
                self._repository.insert_event(
                    job.id,
                    event_type="social_video_download_failed",
                    message="Social video download failed.",
                    metadata={
                        "platform": info.platform,
                        "resolver": info.resolver,
                        "error_code": exc.code,
                        "exception_type": type(exc).__name__,
                        "stage": "mock_video_download",
                        **safe_event_metadata(exc.metadata),
                    },
                )
                raise PipelineFailure(
                    exc.code,
                    exc.message,
                    {"platform": info.platform, **exc.metadata},
                ) from exc
            finally:
                if file_path:
                    _safe_unlink(file_path)
                    try:
                        self._repository.insert_event(
                            job.id,
                            event_type="temp_video_deleted",
                            message="Temporary video file deleted.",
                            metadata={"stage": "mock_video_download"},
                        )
                    except Exception:
                        pass
            self._repository.insert_event(
                job.id,
                event_type="social_video_download_completed",
                message="Social video download completed.",
                metadata={
                    "platform": info.platform,
                    "resolver": info.resolver,
                    "size_bytes": size_bytes,
                    "stage": "mock_video_download",
                    "elapsed_ms": int((perf_counter() - download_started_at) * 1000),
                },
            )

        job = save_mock_recipe_card_for_job(
            repository=self._repository,
            job=job,
            settings=settings,
            platform=info.platform,
            resolver=info.resolver,
            thumbnail_url=thumbnail_url,
            job_started_at=job_started_at,
            check_job_runtime=self._check_job_runtime,
        )
        self._repository.insert_event(
            job.id,
            event_type="completed",
            message="Mock fixture recipe analysis completed.",
            metadata={
                "platform": info.platform,
                "resolver": info.resolver,
                "model": model,
                "status": JOB_STATUS_COMPLETED,
                "ai_analysis_mode": "mock_fixture",
            },
        )
        return job

    def _run_tiktok_video_fallback(
        self,
        job: MediaJobRecord,
        info: SocialVideoInfo,
        settings: Settings,
        job_started_at: float,
    ) -> MediaJobRecord:
        api_key = settings.gemini_api_key.get_secret_value()
        video_model = settings.effective_gemini_video_model
        file_path: str | None = None
        file_name: str | None = None

        try:
            try:
                self._start_gemini_api_call(
                    job.id,
                    model=video_model,
                    stage="video_observation",
                )
            except GeminiCallGuardError as exc:
                raise PipelineFailure(exc.code, exc.message, exc.metadata) from exc

            self._check_job_runtime(job.id, job_started_at)
            job = self._repository.update_job(
                job.id,
                status=JOB_STATUS_DOWNLOADING,
                progress=35,
                error_code=None,
                error_message=None,
            )
            self._repository.insert_event(
                job.id,
                event_type="social_video_download_started",
                message="TikTok video download started.",
                metadata={
                    "platform": "tiktok",
                    "resolver": info.resolver,
                    "stage": "video_download",
                },
            )
            download_started_at = perf_counter()
            try:
                file_path, mime_type, size_bytes = _download_tiktok_to_temp(
                    info,
                    str(job.id),
                    settings,
                )
            except SocialResolverError as exc:
                self._repository.insert_event(
                    job.id,
                    event_type="social_video_download_failed",
                    message="TikTok video download failed.",
                    metadata={
                        "platform": "tiktok",
                        "resolver": info.resolver,
                        "error_code": exc.code,
                        "exception_type": type(exc).__name__,
                        "stage": "video_download",
                        **safe_event_metadata(exc.metadata),
                    },
                )
                raise PipelineFailure(
                    exc.code,
                    exc.message,
                    {"platform": "tiktok", **exc.metadata},
                ) from exc
            self._repository.insert_event(
                job.id,
                event_type="social_video_download_completed",
                message="TikTok video download completed.",
                metadata={
                    "platform": "tiktok",
                    "resolver": info.resolver,
                    "size_bytes": size_bytes,
                    "stage": "video_download",
                    "elapsed_ms": int((perf_counter() - download_started_at) * 1000),
                },
            )

            self._check_job_runtime(job.id, job_started_at)
            job = self._repository.update_job(
                job.id,
                status=JOB_STATUS_ANALYZING,
                progress=50,
                error_code=None,
                error_message=None,
                ai_provider="gemini",
                ai_model=video_model,
            )
            self._repository.insert_event(
                job.id,
                event_type="gemini_video_upload_started",
                message="Gemini video upload started.",
                metadata={
                    "model": video_model,
                    "stage": "video_upload",
                    "size_bytes": size_bytes,
                },
            )
            upload_started_at = perf_counter()
            try:
                media_reference, file_name = _gemini_upload_video_rest(
                    api_key=api_key,
                    file_path=file_path,
                    mime_type=mime_type,
                    timeout_seconds=settings.gemini_files_upload_timeout_seconds,
                )
            except (requests.Timeout, RuntimeError) as exc:
                error_msg = str(exc)
                error_code = "gemini_api_error"
                if isinstance(exc, requests.Timeout):
                    error_code = "gemini_video_timeout"
                elif error_msg == "gemini_rate_limited":
                    error_code = "gemini_rate_limited"
                self._repository.insert_event(
                    job.id,
                    event_type="gemini_video_upload_failed",
                    message="Gemini video upload failed.",
                    metadata={
                        "model": video_model,
                        "stage": "video_upload",
                        "error_code": error_code,
                        "exception_type": type(exc).__name__,
                    },
                )
                raise PipelineFailure(
                    error_code,
                    "Video analysis is temporarily unavailable. Please try again.",
                    {"platform": "tiktok", "stage": "video_upload", "model": video_model},
                ) from exc
            self._repository.insert_event(
                job.id,
                event_type="gemini_video_upload_completed",
                message="Gemini video upload completed.",
                metadata={
                    "model": video_model,
                    "stage": "video_upload",
                    "elapsed_ms": int((perf_counter() - upload_started_at) * 1000),
                },
            )
            self._repository.insert_event(
                job.id,
                event_type="gemini_video_file_active",
                message="Gemini video file is active.",
                metadata={
                    "model": video_model,
                    "stage": "video_upload",
                },
            )

            self._check_job_runtime(job.id, job_started_at)
            self._repository.insert_event(
                job.id,
                event_type="gemini_video_analysis_started",
                message="Gemini video analysis started.",
                metadata={
                    "model": video_model,
                    "stage": "video_observation",
                },
            )
            analysis_started_at = perf_counter()
            try:
                parsed = _gemini_video_to_recipe_rest(
                    api_key=api_key,
                    model=video_model,
                    media_reference=media_reference,
                    mime_type=mime_type,
                    prompt=VIDEO_RECIPE_PROMPT,
                    timeout_seconds=settings.social_video_gemini_timeout_seconds,
                )
            except requests.Timeout:
                elapsed_ms = int((perf_counter() - analysis_started_at) * 1000)
                self._repository.insert_event(
                    job.id,
                    event_type="gemini_video_analysis_failed",
                    message="Gemini video analysis failed.",
                    metadata={
                        "model": video_model,
                        "stage": "video_observation",
                        "elapsed_ms": elapsed_ms,
                        "error_code": "gemini_video_timeout",
                        "exception_type": "Timeout",
                    },
                )
                raise PipelineFailure(
                    "gemini_video_timeout",
                    "Video analysis took too long. Please try a shorter video or paste the caption.",
                    {"platform": "tiktok", "stage": "video_observation", "model": video_model},
                )
            except RuntimeError as exc:
                error_msg = str(exc)
                elapsed_ms = int((perf_counter() - analysis_started_at) * 1000)
                if error_msg == "gemini_rate_limited":
                    error_code = "gemini_rate_limited"
                    user_message = "Recipe analysis is temporarily rate-limited. Please try again."
                elif error_msg.startswith("gemini_api_error_"):
                    error_code = "gemini_api_error"
                    user_message = "Recipe analysis is temporarily unavailable. Please try again."
                else:
                    error_code = "gemini_api_error"
                    user_message = "Recipe analysis is temporarily unavailable. Please try again."
                self._repository.insert_event(
                    job.id,
                    event_type="gemini_video_analysis_failed",
                    message="Gemini video analysis failed.",
                    metadata={
                        "model": video_model,
                        "stage": "video_observation",
                        "elapsed_ms": elapsed_ms,
                        "error_code": error_code,
                        "exception_type": type(exc).__name__,
                    },
                )
                raise PipelineFailure(
                    error_code,
                    user_message,
                    {"platform": "tiktok", "stage": "video_observation", "model": video_model},
                )
            except ValueError:
                elapsed_ms = int((perf_counter() - analysis_started_at) * 1000)
                self._repository.insert_event(
                    job.id,
                    event_type="gemini_video_analysis_failed",
                    message="Gemini video analysis failed.",
                    metadata={
                        "model": video_model,
                        "stage": "video_observation",
                        "elapsed_ms": elapsed_ms,
                        "error_code": "gemini_api_error",
                        "exception_type": "ValueError",
                    },
                )
                raise PipelineFailure(
                    "gemini_api_error",
                    "Recipe analysis is temporarily unavailable. Please try again.",
                    {"platform": "tiktok", "stage": "video_observation", "model": video_model},
                )
            self._repository.insert_event(
                job.id,
                event_type="gemini_video_analysis_completed",
                message="Gemini video analysis completed.",
                metadata={
                    "model": video_model,
                    "stage": "video_observation",
                    "elapsed_ms": int((perf_counter() - analysis_started_at) * 1000),
                },
            )

            is_recipe = parsed.get("is_recipe")
            if is_recipe is not True:
                raise PipelineFailure(
                    "insufficient_recipe_details",
                    "The video does not contain enough recipe details. Please paste the recipe caption or try another video.",
                    {"platform": "tiktok", "stage": "video_observation", "model": video_model},
                )

            confidence = str(parsed.get("confidence", "medium")).strip().lower()
            if confidence == "low":
                raise PipelineFailure(
                    "insufficient_recipe_details",
                    "The video does not contain enough recipe details. Please paste the recipe caption or try another video.",
                    {"platform": "tiktok", "stage": "video_observation", "model": video_model},
                )

            ingredients = parsed.get("ingredients") or []
            steps = parsed.get("steps") or []
            if not isinstance(ingredients, list) or len(ingredients) == 0:
                raise PipelineFailure(
                    "insufficient_recipe_details",
                    "The video does not contain enough recipe details. Please paste the recipe caption or try another video.",
                    {"platform": "tiktok", "stage": "video_observation", "model": video_model},
                )
            if not isinstance(steps, list) or len(steps) == 0:
                raise PipelineFailure(
                    "insufficient_recipe_details",
                    "The video does not contain enough recipe details. Please paste the recipe caption or try another video.",
                    {"platform": "tiktok", "stage": "video_observation", "model": video_model},
                )

            result = _caption_result_to_recipe_result(parsed)

            self._check_job_runtime(job.id, job_started_at)
            job = self._repository.update_job(
                job.id,
                status=JOB_STATUS_ANALYZING,
                progress=85,
                error_code=None,
                error_message=None,
            )
            self._repository.insert_event(
                job.id,
                event_type="recipe_card_saving_started",
                message="Recipe card save started.",
                metadata={"stage": "recipe_card_saving", "progress": 85},
            )
            if job.recipe_item_id is None:
                raise PipelineFailure(
                    "recipe_item_required",
                    "A saved recipe is required before analysis can be completed.",
                )
            recipe_card_id = self._repository.insert_recipe_card(
                job=job,
                result=result,
                model=video_model,
            )
            self._repository.insert_event(
                job.id,
                event_type="recipe_card_saved",
                message="Recipe card saved.",
                metadata={"stage": "recipe_card_saving"},
            )
            job = self._repository.update_job(
                job.id,
                status=JOB_STATUS_COMPLETED,
                progress=100,
                result_recipe_card_id=recipe_card_id,
                ai_provider="gemini",
                ai_model=video_model,
                completed_at=datetime.now(timezone.utc).isoformat(),
                error_code=None,
                error_message=None,
            )
            self._repository.insert_event(
                job.id,
                event_type="completed",
                message="TikTok video recipe analysis completed.",
                metadata={
                    "platform": "tiktok",
                    "resolver": info.resolver,
                    "model": video_model,
                    "status": JOB_STATUS_COMPLETED,
                    "analysis_mode": "video_fallback",
                },
            )
            return job
        finally:
            if file_name:
                _gemini_delete_file_rest(
                    api_key=api_key,
                    file_name=file_name,
                    timeout_seconds=settings.gemini_files_upload_timeout_seconds,
                )
                try:
                    self._repository.insert_event(
                        job.id,
                        event_type="gemini_video_file_deleted",
                        message="Gemini video file deleted.",
                        metadata={"stage": "video_upload"},
                    )
                except Exception:
                    pass
            if file_path:
                _safe_unlink(file_path)
                try:
                    self._repository.insert_event(
                        job.id,
                        event_type="temp_video_deleted",
                        message="Temporary video file deleted.",
                        metadata={"stage": "video_download"},
                    )
                except Exception:
                    pass

    def _upload_social_thumbnail_best_effort(
        self,
        job: MediaJobRecord,
        thumbnail_url: str | None,
        settings: Settings,
        *,
        info: SocialVideoInfo | None = None,
    ) -> str | None:
        thumbnail_source = (
            info.thumbnail_source if info and info.thumbnail_source else "existing_platform_thumbnail"
        )
        event_metadata = {
            "platform": job.platform,
            "thumbnail_source": thumbnail_source,
            "thumbnail_provider": info.thumbnail_provider if info and info.thumbnail_provider else job.platform,
            "thumbnail_strategy": info.thumbnail_strategy if info and info.thumbnail_strategy else "platform_first_v2",
            "candidate_count": info.thumbnail_candidate_count if info and info.thumbnail_candidate_count is not None else 1,
            "thumbnail_selected": bool(thumbnail_url),
        }
        if info is not None and info.duration_seconds is not None:
            event_metadata["duration_seconds"] = info.duration_seconds
        self._repository.insert_event(job.id, event_type="thumbnail_upload_started", message="Thumbnail upload started.", metadata=event_metadata)
        try:
            public_url = upload_source_thumbnail(thumbnail_url, user_id=job.user_id, media_job_id=job.id, repository=self._repository, settings=settings)
            if public_url:
                self._repository.insert_event(job.id, event_type="thumbnail_uploaded", message="Thumbnail uploaded.", metadata={**event_metadata, "thumbnail_selected": True})
                self._update_recipe_thumbnail_best_effort(
                    job,
                    public_url,
                    thumbnail_source=thumbnail_source,
                    event_metadata=event_metadata,
                )
                if info is not None:
                    self._save_source_metadata_best_effort(
                        job,
                        info,
                        uploaded_thumbnail_url=public_url,
                        private_metadata_saved=(
                            job.id in self._private_metadata_saved_job_ids
                        ),
                    )
            return public_url
        except Exception:
            self._repository.insert_event(job.id, event_type="thumbnail_upload_failed", message="Thumbnail upload failed.", metadata={**event_metadata, "thumbnail_selected": False})
            return None

    def _update_social_metadata_best_effort(self, job: MediaJobRecord, info: SocialVideoInfo) -> None:
        private_metadata_saved = self._save_private_metadata_best_effort(job, info)
        self._save_source_metadata_best_effort(
            job,
            info,
            private_metadata_saved=private_metadata_saved,
        )
        values = {
            "media_duration_seconds": int(info.duration_seconds) if info.duration_seconds is not None else None,
            "media_size_bytes": info.filesize_approx,
            "source_title": info.title,
            "source_thumbnail_url": info.thumbnail_url,
            "thumbnail_url": info.thumbnail_url,
        }
        for key, value in values.items():
            if value is None:
                continue
            try:
                self._repository.update_job_metadata_best_effort(job.id, {key: value})
            except Exception as exc:
                logger.warning("social_metadata_update_failed", extra={"media_job_id": str(job.id), "exception_type": type(exc).__name__})
                try:
                    self._repository.insert_event(
                        job.id,
                        event_type="social_metadata_update_failed",
                        message="Social metadata update failed.",
                        metadata={
                            "stage": "social_metadata_update",
                            "exception_type": type(exc).__name__,
                            **safe_exception_metadata(exc),
                        },
                    )
                except Exception:
                    pass

    def _save_source_metadata_best_effort(
        self,
        job: MediaJobRecord,
        info: SocialVideoInfo,
        *,
        uploaded_thumbnail_url: str | None = None,
        private_metadata_saved: bool | None = None,
    ) -> None:
        metadata = social_metadata_event_metadata(info)
        try:
            self._repository.insert_event(
                job.id,
                event_type="source_metadata_update_started",
                message="Source metadata update started.",
                metadata=metadata,
            )
            self._repository.update_source_metadata(
                job.id,
                build_public_source_metadata(
                    info,
                    uploaded_thumbnail_url=uploaded_thumbnail_url,
                    private_metadata_saved=private_metadata_saved,
                ),
            )
            self._repository.insert_event(
                job.id,
                event_type="source_metadata_updated",
                message="Source metadata updated.",
                metadata=metadata,
            )
        except Exception:
            try:
                self._repository.insert_event(
                    job.id,
                    event_type="source_metadata_update_failed",
                    message="Source metadata update failed.",
                    metadata=metadata,
                )
            except Exception:
                pass

    def _save_private_metadata_best_effort(
        self,
        job: MediaJobRecord,
        info: SocialVideoInfo,
    ) -> bool:
        metadata = social_metadata_event_metadata(info)
        try:
            self._repository.upsert_private_metadata(
                job.id,
                platform=info.platform,
                resolver=info.resolver,
                cdn_url=info.stream_url or None,
                raw_metadata=info.raw_metadata,
            )
            self._repository.insert_event(
                job.id,
                event_type="private_metadata_saved",
                message="Private source metadata saved.",
                metadata=metadata,
            )
            self._private_metadata_saved_job_ids.add(job.id)
            return True
        except Exception:
            try:
                self._repository.insert_event(
                    job.id,
                    event_type="private_metadata_save_failed",
                    message="Private source metadata save failed.",
                    metadata=metadata,
                )
            except Exception:
                pass
            return False

    def _run_youtube(self, job: MediaJobRecord, job_started_at: float) -> MediaJobRecord:
        media_job_id = job.id
        video_id = extract_youtube_video_id(job.source_url)
        if video_id is None:
            raise PipelineFailure(
                "invalid_youtube_url",
                "The YouTube link is invalid or unsupported.",
            )

        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        thumbnail_url = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
        self._repository.insert_event(
            media_job_id,
            event_type="youtube_detected",
            message="Public YouTube source detected.",
            metadata={"platform": "youtube"},
        )

        job = self._repository.update_job(
            media_job_id,
            status=JOB_STATUS_RESOLVING,
            progress=20,
            thumbnail_url=thumbnail_url,
            error_code=None,
            error_message=None,
        )
        self._update_recipe_thumbnail_best_effort(
            job,
            thumbnail_url,
            thumbnail_source="youtube_deterministic",
            event_metadata={
                "platform": "youtube",
                "thumbnail_source": "youtube_deterministic",
                "thumbnail_provider": "youtube",
                "thumbnail_strategy": "platform_first_v2",
                "candidate_count": 1,
                "thumbnail_selected": True,
            },
        )
        self._repository.insert_event(
            media_job_id,
            event_type="thumbnail_resolved",
            message="YouTube thumbnail resolved.",
            metadata={
                "platform": "youtube",
                "thumbnail_source": "youtube_deterministic",
                "thumbnail_provider": "youtube",
                "thumbnail_strategy": "platform_first_v2",
                "candidate_count": 1,
                "thumbnail_selected": True,
            },
        )

        if self._analyzer is None:
            raise PipelineFailure(
                "gemini_not_configured",
                "Gemini video analysis is not configured yet.",
            )

        self._filter_model_candidates(media_job_id)
        self._check_job_runtime(media_job_id, job_started_at)
        job = self._repository.update_job(
            media_job_id,
            status=JOB_STATUS_ANALYZING,
            progress=60,
            error_code=None,
            error_message=None,
            ai_provider="gemini",
            ai_model=self._analyzer.model,
        )
        self._repository.insert_event(
            media_job_id,
            event_type="gemini_analysis_started",
            message="Gemini video analysis started.",
            metadata={"model": self._analyzer.model, "status": JOB_STATUS_ANALYZING},
        )
        self._repository.insert_event(
            media_job_id,
            event_type="video_observation_started",
            message="Gemini video observation started.",
            metadata={"model": self._analyzer.model},
        )

        try:
            outcome = self._analyzer.analyze(
                canonical_url,
                on_event=lambda event_type, metadata: self._handle_gemini_event(
                    media_job_id,
                    event_type,
                    metadata,
                    thumbnail_url,
                ),
                before_call=lambda model, stage: self._start_gemini_api_call(
                    media_job_id,
                    model=model,
                    stage=stage,
                ),
            )
            result = outcome.result
        except GeminiCallGuardError as exc:
            raise PipelineFailure(exc.code, exc.message, exc.metadata) from exc
        except GeminiAnalysisError as exc:
            raise PipelineFailure(
                exc.code,
                gemini_user_message(exc.code),
                {
                    "stage": "gemini_analysis",
                    "attempted_models": exc.attempted_models,
                    "final_model": exc.final_model,
                    "attempts_count": exc.attempts_count,
                    "provider_error_type": exc.provider_error_type,
                },
            ) from exc

        self._repository.insert_event(
            media_job_id,
            event_type="gemini_analysis_completed",
            message="Gemini video analysis completed.",
            metadata={
                "model": outcome.model,
            },
        )

        self._check_job_runtime(media_job_id, job_started_at)
        job = self._repository.update_job(
            media_job_id,
            status=JOB_STATUS_ANALYZING,
            progress=85,
            error_code=None,
            error_message=None,
        )
        if job.recipe_item_id is None:
            raise PipelineFailure(
                "recipe_item_required",
                "A saved recipe is required before analysis can be completed.",
            )

        try:
            recipe_card_id = self._repository.insert_recipe_card(
                job=job,
                result=result,
                model=outcome.model,
            )
        except Exception as exc:
            raise PipelineFailure(
                "recipe_card_insert_failed",
                "Recipe analysis completed, but the recipe card could not be saved.",
            ) from exc
        self._repository.insert_event(
            media_job_id,
            event_type="recipe_card_saving_started",
            message="Recipe card save started.",
            metadata={
                "stage": "recipe_card_saving",
                "progress": 85,
            },
        )
        self._repository.insert_event(
            media_job_id,
            event_type="recipe_card_saved",
            message="Recipe card saved.",
            metadata={
                "stage": "recipe_card_saving",
            },
        )

        job = self._repository.update_job(
            media_job_id,
            status=JOB_STATUS_COMPLETED,
            progress=100,
            result_recipe_card_id=recipe_card_id,
            ai_provider="gemini",
            ai_model=outcome.model,
            thumbnail_url=thumbnail_url,
            completed_at=datetime.now(timezone.utc).isoformat(),
            error_code=None,
            error_message=None,
        )
        self._repository.insert_event(
            media_job_id,
            event_type="completed",
            message="YouTube recipe analysis completed.",
            metadata={
                "platform": "youtube",
                "model": outcome.model,
                "status": JOB_STATUS_COMPLETED,
            },
        )
        return job

    def _start_gemini_api_call(
        self,
        media_job_id: UUID,
        *,
        model: str,
        stage: str,
    ) -> dict[str, object]:
        job_count = self._repository.count_job_gemini_calls(media_job_id)
        if job_count >= self._gemini_max_calls_per_job:
            raise GeminiCallGuardError(
                "gemini_job_call_limit_reached",
                "Gemini call limit reached for this job. Try again later.",
                {
                    "error_code": "gemini_job_call_limit_reached",
                    "call_count": job_count,
                    "max_calls_per_job": self._gemini_max_calls_per_job,
                },
            )

        budget_start = self._budget_window_start()
        daily_count = self._repository.count_daily_gemini_calls(budget_start)
        if (
            self._app_env == "development"
            and daily_count >= self._gemini_daily_dev_call_limit
        ):
            raise GeminiCallGuardError(
                "gemini_daily_dev_limit_reached",
                (
                    "Daily development Gemini limit reached. "
                    "Try again after quota resets."
                ),
                {
                    "error_code": "gemini_daily_dev_limit_reached",
                    "call_count": daily_count,
                    "daily_dev_limit": self._gemini_daily_dev_call_limit,
                },
            )

        metadata: dict[str, object] = {
            "model": model,
            "stage": stage,
            "call_number": job_count + 1,
            "max_calls_per_job": self._gemini_max_calls_per_job,
            "timeout_seconds": self._gemini_timeout_for_stage(stage),
        }
        self._repository.insert_event(
            media_job_id,
            event_type="gemini_api_call_started",
            message="Gemini API call started.",
            metadata=metadata,
        )
        return metadata

    def _handle_gemini_event(
        self,
        media_job_id: UUID,
        event_type: str,
        metadata: dict[str, object],
        thumbnail_url: str | None,
    ) -> None:
        if event_type in {"gemini_video_upload_completed", "gemini_video_file_active"}:
            self._repository.update_job(
                media_job_id,
                status=JOB_STATUS_ANALYZING,
                progress=60,
                thumbnail_url=thumbnail_url,
                error_code=None,
                error_message=None,
            )
        elif event_type == "recipe_normalization_started":
            self._repository.update_job(
                media_job_id,
                status=JOB_STATUS_ANALYZING,
                progress=75,
                thumbnail_url=thumbnail_url,
                error_code=None,
                error_message=None,
            )
        self._repository.insert_event(
            media_job_id,
            event_type=event_type,
            message=event_type.replace("_", " ").capitalize() + ".",
            metadata=safe_gemini_metadata(metadata),
        )

    def _check_job_runtime(self, media_job_id: UUID, job_started_at: float) -> None:
        settings = self._settings
        max_runtime = settings.max_job_runtime_seconds if settings is not None else 240
        elapsed_ms = int((perf_counter() - job_started_at) * 1000)
        if elapsed_ms <= max_runtime * 1000:
            return
        metadata = {
            "stage": "job_runtime",
            "elapsed_ms": elapsed_ms,
            "timeout_seconds": max_runtime,
            "error_code": "job_runtime_timeout",
        }
        self._repository.insert_event(
            media_job_id,
            event_type="job_runtime_timeout",
            message="Job runtime timeout.",
            metadata=metadata,
        )
        raise PipelineFailure(
            "job_runtime_timeout",
            "Video analysis took too long. Please try a shorter video.",
            metadata,
        )

    def _gemini_timeout_for_stage(self, stage: str) -> int:
        settings = self._settings
        if settings is None:
            return 120 if stage == "video_observation" else 60
        if stage == "social_text_analysis":
            return settings.social_text_gemini_timeout_seconds
        if stage == "recipe_normalization":
            return settings.gemini_text_normalization_timeout_seconds
        return settings.gemini_video_observation_timeout_seconds

    def _filter_model_candidates(self, media_job_id: UUID) -> None:
        if self._analyzer is None:
            return
        available: list[str] = []
        skipped: list[dict[str, object]] = []
        budget_start = self._budget_window_start()
        for model in self._analyzer.models:
            if model in self._gemini_disabled_models:
                skipped.append({"model": model, "reason": "disabled"})
                continue
            limit = self._gemini_daily_dev_model_limits.get(model)
            count = self._repository.count_daily_gemini_calls(
                budget_start,
                model=model,
            )
            if self._app_env == "development" and limit is not None and count >= limit:
                metadata = {
                    "model": model,
                    "reason": "model_daily_dev_limit_reached",
                    "count": count,
                    "limit": limit,
                }
                skipped.append(metadata)
                self._repository.insert_event(
                    media_job_id,
                    event_type="gemini_model_skipped",
                    message=(
                        "Gemini model skipped because development model "
                        "budget is reached."
                    ),
                    metadata=metadata,
                )
                continue
            available.append(model)
        self._analyzer.models = available
        if not available:
            raise PipelineFailure(
                "gemini_no_model_budget_available",
                (
                    "No Gemini model budget is available for this job. "
                    "Try again later or change model settings."
                ),
                {"stage": "gemini_analysis", "model_budgets": skipped},
            )

    def _budget_window_start(self) -> datetime:
        if self._gemini_dev_budget_reset_at:
            try:
                return datetime.fromisoformat(
                    self._gemini_dev_budget_reset_at.replace("Z", "+00:00")
                )
            except ValueError:
                pass
        return datetime.now(timezone.utc).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

    def _run_skeleton(self, job: MediaJobRecord) -> MediaJobRecord:
        stages = (
            (JOB_STATUS_RESOLVING, 10, "Media worker started resolving the job."),
            (JOB_STATUS_ANALYZING, 30, "Media worker checked the job."),
            (JOB_STATUS_FAILED, 100, SKELETON_ERROR_MESSAGE),
        )
        for status, progress, message in stages:
            is_terminal = status == JOB_STATUS_FAILED
            job = self._repository.update_job(
                job.id,
                status=status,
                progress=progress,
                error_code=SKELETON_ERROR_CODE if is_terminal else None,
                error_message=SKELETON_ERROR_MESSAGE if is_terminal else None,
            )
            self._repository.insert_event(
                job.id,
                event_type=status,
                message=message,
                metadata={
                    "platform": job.platform,
                    "status": status,
                    "progress": progress,
                },
            )
        return job

    def _update_recipe_thumbnail_best_effort(
        self,
        job: MediaJobRecord,
        thumbnail_url: str,
        *,
        thumbnail_source: str = "existing_platform_thumbnail",
        event_metadata: dict[str, object] | None = None,
    ) -> None:
        if job.recipe_item_id is None:
            return
        try:
            existing_url = self._repository.get_recipe_thumbnail(
                recipe_item_id=job.recipe_item_id,
                user_id=job.user_id,
            )
            will_update = should_update_thumbnail(
                existing_url=existing_url,
                existing_source=None,
                new_source=thumbnail_source,
            )
            self._repository.insert_event(
                job.id,
                event_type="thumbnail_selected",
                message="Thumbnail update decision made.",
                metadata={
                    **(event_metadata or {
                        "platform": job.platform,
                        "thumbnail_source": thumbnail_source,
                        "thumbnail_provider": job.platform,
                        "thumbnail_strategy": "platform_first_v2",
                        "candidate_count": 1,
                        "thumbnail_selected": True,
                    }),
                    "used_existing_thumbnail": bool(existing_url and not will_update),
                    "will_update_recipe_item_thumbnail": will_update,
                },
            )
            if not will_update:
                return
            self._repository.update_recipe_thumbnail(
                recipe_item_id=job.recipe_item_id,
                user_id=job.user_id,
                thumbnail_url=thumbnail_url,
            )
        except Exception as exc:
            logger.warning(
                "recipe_thumbnail_update_failed",
                extra={
                    "media_job_id": str(job.id),
                    "exception_type": type(exc).__name__,
                },
            )

    def _record_failure(
        self,
        media_job_id: UUID,
        *,
        error_code: str,
        error_message: str,
        exception_type: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        try:
            self._repository.update_job(
                media_job_id,
                status=JOB_STATUS_FAILED,
                progress=100,
                error_code=error_code,
                error_message=error_message,
                ai_model=(
                    str(metadata["final_model"])
                    if metadata and metadata.get("final_model")
                    else None
                ),
            )
        except Exception as recovery_exc:
            self._log_recovery_failure(
                media_job_id,
                "worker_failure_status_update_failed",
                recovery_exc,
            )

        try:
            self._repository.insert_event(
                media_job_id,
                event_type="worker_error",
                message=error_message,
                metadata={
                    "error_code": error_code,
                    "exception_type": exception_type,
                    **(metadata or {}),
                },
            )
        except Exception as recovery_exc:
            self._log_recovery_failure(
                media_job_id,
                "worker_failure_event_insert_failed",
                recovery_exc,
            )

    @staticmethod
    def _log_job_failure(media_job_id: UUID, exception_type: str) -> None:
        logger.error(
            "worker_job_failed",
            extra={
                "media_job_id": str(media_job_id),
                "exception_type": exception_type,
            },
        )

    @staticmethod
    def _log_recovery_failure(
        media_job_id: UUID,
        event: str,
        exception: Exception,
    ) -> None:
        logger.error(
            event,
            extra={
                "media_job_id": str(media_job_id),
                "exception_type": type(exception).__name__,
            },
        )

    @staticmethod
    def _to_response(job: MediaJobRecord) -> MediaJobData:
        return MediaJobData(
            media_job_id=job.id,
            status=job.status,
            progress=job.progress,
            error_code=job.error_code,
            accepted=False,
        )


def extract_youtube_video_id(source_url: str) -> str | None:
    try:
        parsed = urlparse(source_url)
    except ValueError:
        return None

    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return None

    host = (parsed.hostname or "").lower().rstrip(".")
    video_id: str | None = None

    if host in {"youtu.be", "www.youtu.be"}:
        video_id = first_path_segment(parsed.path)
    elif host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
    }:
        if parsed.path.rstrip("/") == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        elif parsed.path.startswith("/shorts/"):
            video_id = first_path_segment(parsed.path.removeprefix("/shorts/"))

    if video_id and YOUTUBE_VIDEO_ID_PATTERN.fullmatch(video_id):
        return video_id
    return None


def first_path_segment(path: str) -> str | None:
    segment = path.strip("/").split("/", maxsplit=1)[0]
    return segment or None


def platform_for_job(job: MediaJobRecord) -> str:
    platform = (job.platform or "").strip().lower()
    if platform in {"youtube", "tiktok", "instagram", "facebook"}:
        return platform
    detected = detect_social_platform(job.source_url)
    if detected in {"youtube", "tiktok", "instagram", "facebook"}:
        return detected
    return "unknown"


def gemini_user_message(error_code: str) -> str:
    if error_code in {
        "gemini_video_observation_timeout",
        "gemini_text_normalization_timeout",
    }:
        return "Video analysis took too long. Please try a shorter video."
    if error_code == "gemini_text_timeout":
        return (
            "Recipe analysis took too long. Please try again or paste the "
            "caption manually."
        )
    if error_code == "gemini_model_overloaded":
        return (
            "Gemini video analysis is temporarily busy. "
            "Please try again in a few minutes."
        )
    if error_code == "gemini_rate_limited":
        return "Video analysis limit was reached. Please try again later."
    if error_code == "gemini_youtube_unavailable":
        return "This YouTube video could not be accessed for analysis."
    if error_code == "gemini_invalid_json":
        return GEMINI_INVALID_JSON_MESSAGE
    if error_code == "gemini_empty_observation":
        return "Gemini could not extract useful recipe details from this video."
    if error_code == "gemini_inline_video_not_supported":
        return "Inline video analysis is not supported by the current worker configuration."
    if error_code == "gemini_video_bytes_failed":
        return "Social video analysis failed. Please try again."
    return "Video recipe analysis failed. Please try again."


def _social_metadata(info: SocialVideoInfo) -> dict[str, object]:
    return {
        "platform": info.platform,
        "resolver": info.resolver,
        "video_id_present": bool(info.video_id),
        "title_present": bool(info.title),
        "duration_known": info.duration_seconds is not None,
        "duration_seconds": info.duration_seconds,
        "has_thumbnail": bool(info.thumbnail_url),
        "thumbnail_source": info.thumbnail_source or "existing_platform_thumbnail",
        "thumbnail_provider": info.thumbnail_provider or info.platform,
        "thumbnail_strategy": info.thumbnail_strategy or "platform_first_v2",
        "thumbnail_candidate_count": info.thumbnail_candidate_count or 0,
        "thumbnail_selected": bool(info.thumbnail_selected),
        "ext": info.ext,
        "mime_type": info.mime_type,
        "has_stream_headers": bool(info.stream_headers),
    }


def build_public_source_metadata(
    info: SocialVideoInfo,
    *,
    uploaded_thumbnail_url: str | None = None,
    private_metadata_saved: bool | None = None,
) -> dict[str, object]:
    title = info.title or ""
    description = info.description or title
    has_stream = bool(info.stream_url)
    return {
        "platform": info.platform,
        "resolver": info.resolver,
        "title": title,
        "description": description,
        "author_username": info.uploader or "",
        "duration_seconds": info.duration_seconds,
        "thumbnail_source_present": bool(info.thumbnail_url),
        "thumbnail_source": info.thumbnail_source or "existing_platform_thumbnail",
        "thumbnail_provider": info.thumbnail_provider or info.platform,
        "thumbnail_strategy": info.thumbnail_strategy or "platform_first_v2",
        "thumbnail_candidate_count": info.thumbnail_candidate_count or 0,
        "thumbnail_selected": bool(info.thumbnail_selected),
        "uploaded_thumbnail_url": uploaded_thumbnail_url,
        "media": {
            "has_cdn_url": has_stream,
            "cdn_url_stored_private": (
                has_stream
                if private_metadata_saved is None
                else has_stream and private_metadata_saved
            ),
            "mime_type": info.mime_type,
            "size_bytes": info.filesize_approx,
            "width": info.width,
            "height": info.height,
        },
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }


def social_metadata_event_metadata(info: SocialVideoInfo) -> dict[str, object]:
    title = info.title or ""
    description = info.description or title
    return {
        "platform": info.platform,
        "resolver": info.resolver,
        "title_present": bool(title),
        "description_present": bool(description),
        "duration_seconds": info.duration_seconds,
        "has_thumbnail": bool(info.thumbnail_url),
        "thumbnail_source": info.thumbnail_source or "existing_platform_thumbnail",
        "thumbnail_provider": info.thumbnail_provider or info.platform,
        "thumbnail_strategy": info.thumbnail_strategy or "platform_first_v2",
        "thumbnail_candidate_count": info.thumbnail_candidate_count or 0,
        "thumbnail_selected": bool(info.thumbnail_selected),
        "has_cdn_url": bool(info.stream_url),
        "mime_type": info.mime_type,
        "size_bytes": info.filesize_approx,
    }


def expected_resolver_for_platform(platform: str) -> str:
    if platform == "tiktok":
        return "tikwm"
    if platform == "youtube":
        return "ytdlp"
    if platform == "instagram":
        return "snapinsta"
    if platform == "facebook":
        return "snapsave"
    return "social"


def configured_resolver_for_platform(platform: str, settings: Settings) -> str:
    if platform == "tiktok":
        return "tikwm"
    if platform == "youtube":
        return settings.youtube_resolver
    if platform == "instagram":
        return settings.instagram_resolver
    if platform == "facebook":
        return settings.facebook_resolver
    return "social"


def safe_event_metadata(metadata: dict[str, object]) -> dict[str, object]:
    forbidden = {"stream_url", "source_url", "url", "cookies", "cookie", "authorization"}
    return {
        key: value
        for key, value in metadata.items()
        if key.lower() not in forbidden
    }


def is_postgrest_api_error(exception: Exception) -> bool:
    return type(exception).__name__ == "APIError"


def safe_exception_metadata(exception: Exception) -> dict[str, object]:
    code = getattr(exception, "code", None)
    if code is None:
        details = getattr(exception, "details", None)
        if isinstance(details, dict):
            code = details.get("code")
    if code is None and exception.args and isinstance(exception.args[0], dict):
        code = exception.args[0].get("code")
    return {"postgrest_code": str(code)} if code else {}


def safe_gemini_metadata(metadata: dict[str, object]) -> dict[str, object]:
    allowed = {
        "stage",
        "model",
        "elapsed_ms",
        "mime_type",
        "size_bytes",
        "timeout_seconds",
        "file_state",
        "error_code",
        "exception_type",
        "call_number",
        "max_calls_per_job",
    }
    return {
        key: value
        for key, value in metadata.items()
        if key in allowed and value is not None
    }
