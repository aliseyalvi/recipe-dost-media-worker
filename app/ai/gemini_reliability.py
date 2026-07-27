from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

GeminiCallPurpose = Literal[
    "analysis",
    "structuring",
    "repair",
    "model_fallback",
    "translation",
]

GEMINI_FAILURE_CATEGORIES = frozenset(
    {
        "gemini_request_configuration_error",
        "gemini_authentication_error",
        "gemini_rate_limited",
        "gemini_timeout",
        "gemini_server_error",
        "gemini_file_unavailable",
        "gemini_safety_blocked",
        "gemini_empty_response",
        "gemini_no_text_candidate",
        "gemini_truncated_response",
        "gemini_json_parse_failed",
        "gemini_schema_validation_failed",
        "gemini_non_recipe_content",
        "gemini_call_budget_exhausted",
        "gemini_local_preflight_failed",
        "gemini_repair_failed",
        "gemini_fallback_failed",
    }
)


@dataclass(frozen=True)
class GeminiModelCapabilities:
    model: str
    family: str
    supports_json_schema: bool
    supports_json_mime: bool
    schema_field: Literal["response_json_schema", "response_schema"]
    thinking_mode: Literal["level", "budget", "none"]
    max_output_tokens: int


class GeminiRequestConfigurationError(ValueError):
    pass


class StructuredResponseError(ValueError):
    def __init__(self, category: str, *, raw_text: str | None = None) -> None:
        super().__init__(category)
        self.category = category
        self.raw_text = raw_text


def model_capabilities(model: str) -> GeminiModelCapabilities:
    normalized = str(model or "").strip().lower()
    if normalized.startswith(("gemini-3.", "gemini-3-")):
        return GeminiModelCapabilities(
            model=model,
            family="gemini_3",
            supports_json_schema=True,
            supports_json_mime=True,
            schema_field="response_json_schema",
            thinking_mode="level",
            max_output_tokens=65_536,
        )
    if normalized.startswith(("gemini-2.5-", "gemini-2-5-")):
        return GeminiModelCapabilities(
            model=model,
            family="gemini_2_5",
            supports_json_schema=True,
            supports_json_mime=True,
            schema_field="response_schema",
            thinking_mode="budget",
            max_output_tokens=65_536,
        )
    raise GeminiRequestConfigurationError("Unsupported Gemini model family.")


def build_model_generation_config(
    *,
    model: str,
    response_schema: dict[str, object] | None,
    structured_output_enabled: bool,
    thinking_level: str | None,
    max_output_tokens: int,
    temperature: float,
) -> dict[str, object]:
    capabilities = model_capabilities(model)
    config: dict[str, object] = {
        "temperature": min(2.0, max(0.0, float(temperature))),
        "max_output_tokens": min(
            capabilities.max_output_tokens,
            max(512, int(max_output_tokens)),
        ),
    }
    if structured_output_enabled:
        if not capabilities.supports_json_mime:
            raise GeminiRequestConfigurationError(
                "Configured model does not support JSON output."
            )
        config["response_mime_type"] = "application/json"
        if response_schema is not None:
            if not capabilities.supports_json_schema:
                raise GeminiRequestConfigurationError(
                    "Configured model does not support JSON schema."
                )
            config[capabilities.schema_field] = (
                response_schema
                if capabilities.schema_field == "response_json_schema"
                else google_response_schema(response_schema)
            )
    if capabilities.thinking_mode == "level" and thinking_level:
        normalized_level = str(thinking_level).strip().lower()
        if normalized_level not in {"minimal", "low", "medium", "high"}:
            raise GeminiRequestConfigurationError("Invalid Gemini thinking level.")
        config["thinking_config"] = {"thinking_level": normalized_level}
    # Gemini 2.5 uses a model-specific thinking budget rather than the Gemini 3
    # thinking-level contract. Omit it here and retain the model default.
    return config


def google_response_schema(schema: dict[str, object]) -> dict[str, object]:
    """Translate JSON Schema nullable unions to the Gemini Schema dialect.

    ``response_json_schema`` accepts standard JSON Schema. The older
    ``response_schema`` field is parsed by the SDK's ``Schema`` model and
    requires a scalar enum value for ``type`` plus ``nullable=true``.
    """

    def convert(value: object) -> object:
        if isinstance(value, list):
            return [convert(item) for item in value]
        if not isinstance(value, dict):
            return value
        converted: dict[str, object] = {}
        for key, item in value.items():
            if key == "type" and isinstance(item, list):
                non_null = [
                    str(candidate)
                    for candidate in item
                    if str(candidate).lower() != "null"
                ]
                nullable = len(non_null) != len(item)
                if not non_null:
                    raise GeminiRequestConfigurationError(
                        "Gemini response schema has an unsupported type union."
                    )
                if len(non_null) == 1:
                    converted[key] = non_null[0]
                else:
                    converted["anyOf"] = [
                        {"type": candidate} for candidate in non_null
                    ]
                if nullable:
                    converted["nullable"] = True
                continue
            converted[key] = convert(item)
        return converted

    result = convert(schema)
    if not isinstance(result, dict):
        raise GeminiRequestConfigurationError(
            "Gemini response schema must be an object."
        )
    return result


def validate_generation_config(config: dict[str, object]) -> None:
    try:
        from google.genai import types

        types.GenerateContentConfig(**config)
    except (TypeError, ValueError) as exc:
        raise GeminiRequestConfigurationError(
            "Gemini generation configuration was rejected locally."
        ) from exc


def effective_configuration_category(model: str) -> str:
    capabilities = model_capabilities(model)
    if capabilities.family == "gemini_3":
        return "json_schema_thinking_level"
    if capabilities.family == "gemini_2_5":
        return "response_schema_model_default_thinking"
    return "unsupported"


def parse_structured_response(
    text: str | None,
    *,
    finish_reason: str | None = None,
    safety_blocked: bool = False,
) -> dict[str, object]:
    if safety_blocked:
        raise StructuredResponseError("gemini_safety_blocked")
    raw = str(text or "").lstrip("\ufeff").strip()
    if not raw:
        raise StructuredResponseError("gemini_empty_response")
    if _looks_like_refusal(raw):
        raise StructuredResponseError("gemini_safety_blocked")

    fenced = re.fullmatch(
        r"\s*```(?:json)?\s*(.*?)\s*```\s*",
        raw,
        flags=re.IGNORECASE | re.DOTALL,
    )
    candidate_text = fenced.group(1).strip() if fenced else raw
    decoded = _decode_json_values(candidate_text)
    if not decoded and candidate_text is not raw:
        decoded = _decode_json_values(raw)
    if not decoded:
        category = (
            "gemini_truncated_response"
            if _looks_truncated(raw, finish_reason)
            else "gemini_json_parse_failed"
        )
        raise StructuredResponseError(category, raw_text=raw[:12_000])
    if len(decoded) != 1:
        raise StructuredResponseError(
            "gemini_json_parse_failed",
            raw_text=raw[:12_000],
        )
    value = decoded[0]
    if not isinstance(value, dict):
        raise StructuredResponseError(
            "gemini_schema_validation_failed",
            raw_text=raw[:12_000],
        )
    return value


def classify_gemini_exception(
    exc: Exception,
    *,
    provider_request_attempted: bool | None = None,
) -> str:
    if isinstance(exc, GeminiRequestConfigurationError):
        return "gemini_request_configuration_error"
    if isinstance(exc, StructuredResponseError):
        return exc.category
    category = getattr(exc, "metadata", {}).get("failure_category")
    if isinstance(category, str) and category in GEMINI_FAILURE_CATEGORIES:
        return category
    code = str(getattr(exc, "code", "") or "").strip().upper()
    status = exception_status(exc)
    name = type(exc).__name__.lower()
    if type(exc).__name__ in {"ValidationError", "SchemaError"}:
        return "gemini_request_configuration_error"
    if status in {401, 403} or code in {
        "UNAUTHENTICATED",
        "PERMISSION_DENIED",
    }:
        return "gemini_authentication_error"
    if status == 429 or code == "RESOURCE_EXHAUSTED":
        return "gemini_rate_limited"
    if status in {408, 499} or (
        status is None
        and any(
        marker in name for marker in ("timeout", "deadline", "cancelled")
        )
    ):
        return "gemini_timeout"
    if status == 404 or code == "NOT_FOUND":
        return "gemini_file_unavailable"
    if status in {400, 409, 412, 422} or code in {
        "INVALID_ARGUMENT",
        "FAILED_PRECONDITION",
    }:
        return "gemini_request_configuration_error"
    if status is not None and status >= 500:
        return "gemini_server_error"
    if code in {"INTERNAL", "UNAVAILABLE"}:
        return "gemini_server_error"
    if "clienterror" in name:
        return "gemini_request_configuration_error"
    if provider_request_attempted is False:
        return "gemini_local_preflight_failed"
    return "gemini_request_configuration_error"


def sdk_exception_category(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    if type(exc).__name__ in {"ValidationError", "SchemaError"}:
        return "local_validation"
    if any(marker in name for marker in ("timeout", "deadline")):
        return "timeout"
    if exception_status(exc) is not None:
        return "provider_status"
    if isinstance(exc, (TypeError, ValueError, AttributeError)):
        return "local_configuration"
    return "unknown"


def exception_status(exc: Exception) -> int | None:
    for name in ("status_code", "status"):
        value = getattr(exc, name, None)
        if isinstance(value, int):
            return value
    value = getattr(exc, "code", None)
    if isinstance(value, int):
        return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def provider_status_class(exc: Exception) -> str:
    status = exception_status(exc)
    return f"{status // 100}xx" if status is not None and 100 <= status <= 599 else "unknown"


def response_length_bucket(length: int) -> str:
    if length <= 0:
        return "empty"
    if length < 512:
        return "under_512"
    if length < 2_048:
        return "512_2047"
    if length < 8_192:
        return "2048_8191"
    return "8192_plus"


def normalize_finish_reason(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return "unknown"
    for category in (
        "STOP",
        "MAX_TOKENS",
        "SAFETY",
        "RECITATION",
        "LANGUAGE",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "MALFORMED_FUNCTION_CALL",
        "IMAGE_SAFETY",
    ):
        if category in text:
            return category.lower()
    return "other"


def _decode_json_values(text: str) -> list[object]:
    decoder = json.JSONDecoder()
    stripped = text.strip()
    try:
        value = json.loads(stripped)
        return [value]
    except json.JSONDecodeError:
        pass
    values: list[object] = []
    index = 0
    while index < len(stripped):
        next_object = stripped.find("{", index)
        next_array = stripped.find("[", index)
        starts = [item for item in (next_object, next_array) if item >= 0]
        if not starts:
            break
        start = min(starts)
        try:
            value, end = decoder.raw_decode(stripped, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        values.append(value)
        index = end
    return values


def _looks_truncated(text: str, finish_reason: str | None) -> bool:
    if "MAX_TOKENS" in str(finish_reason or "").upper():
        return True
    in_string = False
    escaped = False
    stack: list[str] = []
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            stack.append(char)
        elif char in "]}":
            if not stack:
                return False
            stack.pop()
    return bool(stack or in_string)


def _looks_like_refusal(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    return any(
        marker in normalized
        for marker in (
            "i cannot comply",
            "i can't comply",
            "unable to comply",
            "cannot help with that",
            "blocked for safety",
        )
    )
