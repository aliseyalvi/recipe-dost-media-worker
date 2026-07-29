from __future__ import annotations

import json
from datetime import datetime, timezone
from time import sleep
from typing import Callable
from uuid import UUID

from app.config import Settings
from app.schemas import (
    GeminiRecipeResult,
    GroceryItem,
    MediaJobRecord,
    RecipeIngredient,
    RecipeStep,
)
from app.social.platforms import detect_social_platform
from app.supabase_client import MediaJobRepository

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RESOLVING = "resolving"
JOB_STATUS_DOWNLOADING = "downloading"
JOB_STATUS_ANALYZING = "analyzing"
JOB_STATUS_COMPLETED = "completed"
JOB_STATUS_FAILED = "failed"


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
    with open(fixture_path, encoding="utf-8") as fixture_file:
        value = json.load(fixture_file)
    if not isinstance(value, dict):
        raise ValueError("Mock recipe fixture must be a JSON object.")
    return value


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


def platform_for_job(job: MediaJobRecord) -> str:
    platform = (job.platform or "").strip().lower()
    if platform in {"youtube", "tiktok", "instagram", "facebook"}:
        return platform
    detected = detect_social_platform(job.source_url)
    if detected in {"youtube", "tiktok", "instagram", "facebook"}:
        return detected
    return "unknown"
