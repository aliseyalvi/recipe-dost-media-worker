from __future__ import annotations

import json
import time
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Callable

from pydantic import ValidationError

from app.schemas import GeminiRecipeResult

PROMPT_VERSION = "m14g3_youtube_v1"

RECIPE_PROMPT = """
Convert the supplied video observations into one valid Recipe Dost JSON object
with exactly these
top-level fields:

{
  "recipe_title_en": "string",
  "recipe_title_ur": "string",
  "short_summary_en": "string",
  "short_summary_ur": "string",
  "confidence": "low|medium|high",
  "prep_time_minutes": null,
  "cook_time_minutes": null,
  "servings": null,
  "difficulty": "easy|medium|hard|unknown",
  "ingredients": [{
    "name_en": "string",
    "name_ur": "string",
    "quantity": "string",
    "notes_en": "string",
    "notes_ur": "string"
  }],
  "steps": [{
    "step_number": 1,
    "instruction_en": "string",
    "instruction_ur": "string",
    "tip_en": "string",
    "tip_ur": "string"
  }],
  "grocery_list": [{
    "name_en": "string",
    "name_ur": "string",
    "quantity": "string"
  }],
  "missing_info_en": ["string"],
  "missing_info_ur": ["string"],
  "storage_tip_en": "string",
  "storage_tip_ur": "string",
  "safety_note_en": "string",
  "safety_note_ur": "string",
  "source_assumptions_en": ["string"],
  "source_assumptions_ur": ["string"]
}

Requirements:
- Write every Urdu field in Urdu script, never Roman Urdu.
- Use simple Urdu suitable for a Pakistani household.
- Understand Urdu, English, and mixed Roman-Urdu/Romlish speech or captions.
- Use null for unknown numeric timing and unknown servings.
- Use empty strings or arrays only when the video provides no safe value.
- Never invent an ingredient, quantity, timing, or step that is unclear.
- Put uncertainty in missing_info and source_assumptions.
- Do not make medical, dietary, calorie, allergy, weight-loss, or health claims.
- Return JSON only, without markdown fences, commentary, or citations.
""".strip()

OBSERVATION_PROMPT = """
Analyze this public YouTube recipe video. Extract observed recipe facts only.
Do not return final app JSON and do not invent. Return concise structured notes
covering the likely recipe name, visible or spoken ingredients and quantities,
cooking actions and order, timing and serving clues, language clues,
uncertainty or missing details, and whether this is actually a recipe video.
""".strip()


class GeminiInvalidJsonError(Exception):
    pass


TEMPORARY_GEMINI_ERRORS = {
    "gemini_model_overloaded",
    "gemini_rate_limited",
    "gemini_timeout",
    "gemini_unknown_error",
}


@dataclass(frozen=True)
class GeminiAnalysisOutcome:
    result: GeminiRecipeResult
    model: str
    attempts_count: int


class GeminiAnalysisError(Exception):
    def __init__(
        self,
        code: str,
        *,
        attempted_models: list[str],
        final_model: str,
        attempts_count: int,
        provider_error_type: str,
        provider_error_message: str,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.attempted_models = attempted_models
        self.final_model = final_model
        self.attempts_count = attempts_count
        self.provider_error_type = provider_error_type
        self.provider_error_message = provider_error_message


class GeminiCallGuardError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        metadata: dict[str, Any],
    ) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.metadata = metadata


class GeminiEmptyResponseError(Exception):
    pass


class GeminiRecipeAnalyzer:
    def __init__(
        self,
        api_key: str,
        model: str,
        text_model: str | None = None,
        fallback_models: list[str] | None = None,
        max_retries: int = 3,
        retry_base_seconds: float = 2.0,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.text_model = text_model or "gemini-2.5-flash"
        self.models = unique_models([model, *(fallback_models or [])])
        self.max_retries = max(1, max_retries)
        self.retry_base_seconds = max(0.0, retry_base_seconds)

    def analyze(
        self,
        youtube_url: str,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        before_call: Callable[[str, str], dict[str, Any]] | None = None,
    ) -> GeminiAnalysisOutcome:
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError("Google Gen AI SDK is not installed.") from exc

        attempts_count = 0
        attempted_models: list[str] = []
        last_error: Exception | None = None
        last_code = "gemini_unknown_error"

        with genai.Client(api_key=self._api_key) as client:
            for model_index, model in enumerate(self.models):
                attempted_models.append(model)
                if model_index > 0:
                    emit(on_event, "gemini_model_fallback_started", {"model": model})

                for attempt in range(1, self.max_retries + 1):
                    call_metadata = (
                        before_call(model, "video_observation")
                        if before_call is not None
                        else {}
                    )
                    attempts_count += 1
                    emit(
                        on_event,
                        "gemini_model_attempt_started",
                        {
                            "model": model,
                            "attempt": attempt,
                            "max_retries": self.max_retries,
                        },
                    )
                    try:
                        started_at = perf_counter()
                        interaction = client.interactions.create(
                            model=model,
                            input=[
                                {"type": "video", "uri": youtube_url},
                                {"type": "text", "text": OBSERVATION_PROMPT},
                            ],
                        )
                        observations = extract_gemini_text(interaction)
                        emit(
                            on_event,
                            "gemini_api_call_completed",
                            {
                                **call_metadata,
                                "model": model,
                                "stage": "video_observation",
                                "duration_ms": int(
                                    (perf_counter() - started_at) * 1000
                                ),
                            },
                        )
                        emit(
                            on_event,
                            "video_observation_completed",
                            {
                                "model": model,
                                "observation_char_count": len(observations),
                            },
                        )
                        emit(
                            on_event,
                            "recipe_normalization_started",
                            {"model": self.text_model},
                        )
                        normalization_metadata = (
                            before_call(self.text_model, "recipe_normalization")
                            if before_call is not None
                            else {}
                        )
                        call_metadata = normalization_metadata
                        model = self.text_model
                        current_stage = "recipe_normalization"
                        started_at = perf_counter()
                        normalized = client.interactions.create(
                            model=self.text_model,
                            input=[
                                {
                                    "type": "text",
                                    "text": f"{RECIPE_PROMPT}\n\nOBSERVATIONS:\n{observations}",
                                }
                            ],
                        )
                        normalized_text = extract_gemini_text(normalized)
                        result = parse_recipe_response(normalized_text)
                        emit(
                            on_event,
                            "gemini_api_call_completed",
                            {
                                **normalization_metadata,
                                "model": self.text_model,
                                "stage": "recipe_normalization",
                                "duration_ms": int(
                                    (perf_counter() - started_at) * 1000
                                ),
                            },
                        )
                        emit(
                            on_event,
                            "recipe_normalization_completed",
                            {
                                "model": self.text_model,
                                "response_char_count": len(normalized_text),
                            },
                        )
                        return GeminiAnalysisOutcome(
                            result,
                            self.text_model,
                            attempts_count + 1,
                        )
                    except GeminiCallGuardError:
                        raise
                    except Exception as exc:
                        last_error = exc
                        last_code = classify_gemini_error(exc)
                        emit(
                            on_event,
                            "gemini_api_call_failed",
                            {
                                **locals().get("call_metadata", {}),
                                "model": model,
                                "stage": locals().get(
                                    "current_stage",
                                    "video_observation",
                                ),
                                "error_code": last_code,
                                "provider_error_message": sanitize_error_message(exc),
                                "duration_ms": int(
                                    (perf_counter() - started_at) * 1000
                                )
                                if "started_at" in locals()
                                else 0,
                            },
                        )
                        emit(
                            on_event,
                            "gemini_model_attempt_failed",
                            {
                                "model": model,
                                "attempt": attempt,
                                "max_retries": self.max_retries,
                                "error_code": last_code,
                                "provider_error_message": sanitize_error_message(exc),
                            },
                        )
                        if last_code not in TEMPORARY_GEMINI_ERRORS:
                            raise build_analysis_error(
                                exc,
                                last_code,
                                attempted_models,
                                model,
                                attempts_count,
                            ) from exc
                        if attempt < self.max_retries:
                            time.sleep(
                                self.retry_base_seconds * (2 ** (attempt - 1))
                            )

        assert last_error is not None
        raise build_analysis_error(
            last_error,
            last_code,
            attempted_models,
            attempted_models[-1],
            attempts_count,
        ) from last_error

    def analyze_video_bytes(
        self,
        video_bytes: bytes,
        mime_type: str,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        before_call: Callable[[str, str], dict[str, Any]] | None = None,
    ) -> GeminiAnalysisOutcome:
        try:
            from google import genai
        except ImportError as exc:
            raise RuntimeError("Google Gen AI SDK is not installed.") from exc

        attempts_count = 0
        attempted_models: list[str] = []
        last_error: Exception | None = None
        last_code = "gemini_video_bytes_failed"

        with genai.Client(api_key=self._api_key) as client:
            for model in self.models:
                attempted_models.append(model)
                for attempt in range(1, self.max_retries + 1):
                    call_metadata = before_call(model, "video_observation") if before_call else {}
                    attempts_count += 1
                    try:
                        started_at = perf_counter()
                        interaction = client.interactions.create(
                            model=model,
                            input=[
                                {"type": "video", "data": video_bytes, "mime_type": mime_type},
                                {"type": "text", "text": OBSERVATION_PROMPT},
                            ],
                        )
                        observations = extract_gemini_text(interaction)
                        emit(on_event, "gemini_api_call_completed", {**call_metadata, "model": model, "stage": "video_observation", "duration_ms": int((perf_counter() - started_at) * 1000)})
                        emit(on_event, "video_observation_completed", {"model": model, "observation_char_count": len(observations)})

                        emit(on_event, "recipe_normalization_started", {"model": self.text_model})
                        normalization_metadata = before_call(self.text_model, "recipe_normalization") if before_call else {}
                        started_at = perf_counter()
                        normalized = client.interactions.create(
                            model=self.text_model,
                            input=[{"type": "text", "text": f"{RECIPE_PROMPT}\n\nOBSERVATIONS:\n{observations}"}],
                        )
                        normalized_text = extract_gemini_text(normalized)
                        result = parse_recipe_response(normalized_text)
                        emit(on_event, "gemini_api_call_completed", {**normalization_metadata, "model": self.text_model, "stage": "recipe_normalization", "duration_ms": int((perf_counter() - started_at) * 1000)})
                        emit(on_event, "recipe_normalization_completed", {"model": self.text_model, "response_char_count": len(normalized_text)})
                        return GeminiAnalysisOutcome(result, self.text_model, attempts_count + 1)
                    except GeminiCallGuardError:
                        raise
                    except Exception as exc:
                        last_error = exc
                        last_code = classify_gemini_error(exc)
                        if last_code == "gemini_invalid_request" and "data" in sanitize_error_message(exc).lower():
                            last_code = "gemini_inline_video_not_supported"
                        emit(on_event, "gemini_api_call_failed", {"model": model, "stage": "video_observation", "error_code": last_code, "provider_error_message": sanitize_error_message(exc)})
                        if last_code not in TEMPORARY_GEMINI_ERRORS:
                            raise build_analysis_error(exc, last_code, attempted_models, model, attempts_count) from exc
                        if attempt < self.max_retries:
                            time.sleep(self.retry_base_seconds * (2 ** (attempt - 1)))

        assert last_error is not None
        raise build_analysis_error(last_error, last_code, attempted_models, attempted_models[-1], attempts_count) from last_error


def parse_recipe_response(response_text: Any) -> GeminiRecipeResult:
    if not isinstance(response_text, str) or not response_text.strip():
        raise GeminiInvalidJsonError

    try:
        payload = parse_json_lenient(response_text)
        if not isinstance(payload, dict):
            raise ValueError("Expected a JSON object.")
        return GeminiRecipeResult.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
        raise GeminiInvalidJsonError from exc


def extract_gemini_text(response: Any) -> str:
    for field in ("text", "output_text"):
        value = (
            response.get(field)
            if isinstance(response, dict)
            else getattr(response, field, None)
        )
        if isinstance(value, str) and value.strip():
            return value.strip()
    candidates = (
        response.get("candidates", [])
        if isinstance(response, dict)
        else getattr(response, "candidates", [])
    )
    texts: list[str] = []
    for candidate in candidates or []:
        content = (
            candidate.get("content", {})
            if isinstance(candidate, dict)
            else getattr(candidate, "content", None)
        )
        parts = (
            content.get("parts", [])
            if isinstance(content, dict)
            else getattr(content, "parts", [])
        )
        for part in parts or []:
            text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    if texts:
        return "\n".join(texts)
    raise GeminiEmptyResponseError


def parse_json_lenient(text: str) -> dict[str, Any]:
    for candidate in (text, strip_markdown_code_fences(text), extract_first_json_object(text)):
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            continue
    raise ValueError("No valid JSON object found.")


def strip_markdown_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        first_newline = stripped.find("\n")
        return stripped[first_newline + 1 : -3].strip()
    return stripped


def extract_first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\" and in_string:
            escaped = True
        elif char == '"':
            in_string = not in_string
        elif not in_string:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
    return None


def unique_models(models: list[str]) -> list[str]:
    return list(dict.fromkeys(model.strip() for model in models if model.strip()))


def classify_gemini_error(error: Exception) -> str:
    if isinstance(error, GeminiEmptyResponseError):
        return "gemini_empty_observation"
    if isinstance(error, GeminiInvalidJsonError):
        return "gemini_invalid_json"
    message = sanitize_error_message(error).lower()
    status = str(
        getattr(error, "status_code", "")
        or getattr(error, "code", "")
        or getattr(error, "status", "")
    ).lower()
    combined = f"{status} {message}"
    if "high demand" in combined or "overloaded" in combined or "503" in combined or "unavailable" in combined:
        return "gemini_model_overloaded"
    if "429" in combined or "resource_exhausted" in combined or "rate limit" in combined:
        return "gemini_rate_limited"
    if "timeout" in combined or "deadline" in combined or "504" in combined:
        return "gemini_timeout"
    if "401" in combined or "403" in combined or "api key" in combined or "permission" in combined or "auth" in combined:
        return "gemini_auth_error"
    if "404" in combined or "model not found" in combined or "not_found" in combined:
        return "gemini_model_not_found"
    if "invalid argument" in combined or "invalid request" in combined or "schema" in combined or "malformed" in combined or "400" in combined:
        return "gemini_invalid_request"
    if "private" in combined or "unlisted" in combined or "cannot access" in combined or "youtube unavailable" in combined or "unsupported video" in combined:
        return "gemini_youtube_unavailable"
    return "gemini_unknown_error"


def sanitize_error_message(error: Exception) -> str:
    return " ".join(str(error).split())[:250]


def build_analysis_error(
    error: Exception,
    code: str,
    attempted_models: list[str],
    final_model: str,
    attempts_count: int,
) -> GeminiAnalysisError:
    return GeminiAnalysisError(
        code,
        attempted_models=list(attempted_models),
        final_model=final_model,
        attempts_count=attempts_count,
        provider_error_type=type(error).__name__,
        provider_error_message=sanitize_error_message(error),
    )


def emit(
    callback: Callable[[str, dict[str, Any]], None] | None,
    event_type: str,
    metadata: dict[str, Any],
) -> None:
    if callback is not None:
        callback(event_type, metadata)
