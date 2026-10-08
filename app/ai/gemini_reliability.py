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


SAFE_PROVIDER_ERROR_CATEGORIES = frozenset(
    {
        "local_request_validation",
        "invalid_argument",
        "failed_precondition",
        "authentication_failed",
        "permission_denied",
        "model_or_endpoint_not_found",
        "conflict",
        "rate_limited",
        "provider_internal",
        "provider_unavailable",
        "timeout",
        "connection_error",
        "response_parse_error",
        "unknown_provider_error",
    }
)

_RESPONSE_PARSE_FAILURES = frozenset(
    {
        "gemini_empty_response",
        "gemini_no_text_candidate",
        "gemini_truncated_response",
        "gemini_json_parse_failed",
        "gemini_schema_validation_failed",
        "gemini_safety_blocked",
    }
)

_RETRYABLE_PROVIDER_ERRORS = frozenset(
    {
        "rate_limited",
        "provider_internal",
        "provider_unavailable",
        "timeout",
        "connection_error",
    }
)

_TIMEOUT_EXCEPTION_NAMES = frozenset(
    {
        "Timeout",
        "TimeoutError",
        "TimeoutException",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "GeminiTimeoutError",
    }
)

_CONNECTION_EXCEPTION_NAMES = frozenset(
    {
        "ConnectionError",
        "ConnectError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "ConnectionRefusedError",
        "NetworkError",
        "ProxyError",
        "TransportError",
    }
)


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
    """Convert JSON Schema nullable unions to the Gemini 2.5 Schema dialect."""

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
                if len(non_null) != len(item):
                    converted["nullable"] = True
                continue
            converted[key] = convert(item)
        return converted

    converted = convert(schema)
    if not isinstance(converted, dict):
        raise GeminiRequestConfigurationError(
            "Gemini response schema must be an object."
        )
    return converted


def validate_generation_config(config: dict[str, object]) -> None:
    try:
        from google.genai import types

        types.GenerateContentConfig(**config)
    except (TypeError, ValueError) as exc:
        raise GeminiRequestConfigurationError(
            "Gemini generation configuration was rejected locally."
        ) from exc


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


def classify_gemini_exception(exc: Exception) -> str:
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
    if status in {401, 403} or code in {
        "UNAUTHENTICATED",
        "PERMISSION_DENIED",
    }:
        return "gemini_authentication_error"
    if status == 429 or code == "RESOURCE_EXHAUSTED":
        return "gemini_rate_limited"
    if status in {408, 499, 504} or any(
        marker in name for marker in ("timeout", "deadline", "cancelled")
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
    if code in {"INTERNAL", "UNAVAILABLE"} or any(
        marker in name for marker in ("servererror", "internalserver")
    ):
        return "gemini_server_error"
    if "clienterror" in name:
        return "gemini_request_configuration_error"
    return "gemini_server_error"


def safe_gemini_provider_error(
    exc: Exception,
    *,
    provider_request_attempted: bool,
    failure_stage: str,
    model: str | None = None,
    input_mode: str | None = None,
    request_profile: str | None = None,
    latency_bucket: str | None = None,
) -> dict[str, object]:
    """Return bounded provider diagnostics without exception text or response data."""
    try:
        return _safe_gemini_provider_error(
            exc,
            provider_request_attempted=provider_request_attempted,
            failure_stage=failure_stage,
            model=model,
            input_mode=input_mode,
            request_profile=request_profile,
            latency_bucket=latency_bucket,
        )
    except Exception:
        result: dict[str, object] = {
            "sdk_exception_type": _safe_identifier(type(exc).__name__, "Exception"),
            "provider_http_status": None,
            "provider_status_name": None,
            "provider_error_category": "unknown_provider_error",
            "provider_request_attempted": provider_request_attempted,
            "retryable": None,
            "failure_stage": _safe_identifier(failure_stage, "unknown"),
        }
        for field, value in (
            ("model", model),
            ("input_mode", input_mode),
            ("request_profile", request_profile),
            ("latency_bucket", latency_bucket),
        ):
            if value is not None:
                result[field] = _safe_identifier(value, "unknown")
        return result


def _safe_gemini_provider_error(
    exc: Exception,
    *,
    provider_request_attempted: bool,
    failure_stage: str,
    model: str | None,
    input_mode: str | None,
    request_profile: str | None,
    latency_bucket: str | None,
) -> dict[str, object]:
    chain = list(_exception_chain(exc))
    status = next(
        (candidate for item in chain if (candidate := exception_status(item)) is not None),
        None,
    )
    status_name = next(
        (
            candidate
            for item in chain
            if (candidate := _safe_provider_status_name(item)) is not None
        ),
        None,
    )
    attempted = provider_request_attempted
    if any(isinstance(item, GeminiRequestConfigurationError) for item in chain):
        attempted = False
    if any(_is_pydantic_validation_error(item) for item in chain):
        attempted = False
    for item in chain:
        metadata = getattr(item, "metadata", None)
        if isinstance(metadata, dict) and metadata.get("provider_request_attempted") is False:
            attempted = False
            break

    category = _safe_provider_category(
        chain,
        status=status,
        status_name=status_name,
        provider_request_attempted=attempted,
    )
    result: dict[str, object] = {
        "sdk_exception_type": _safe_identifier(type(exc).__name__, "Exception"),
        "provider_http_status": status,
        "provider_status_name": status_name,
        "provider_error_category": category,
        "provider_request_attempted": attempted,
        "retryable": (
            True
            if category in _RETRYABLE_PROVIDER_ERRORS
            else None
            if category == "unknown_provider_error"
            else False
        ),
        "failure_stage": _safe_identifier(failure_stage, "unknown"),
    }
    for field, value in (
        ("model", model),
        ("input_mode", input_mode),
        ("request_profile", request_profile),
        ("latency_bucket", latency_bucket),
    ):
        if value is not None:
            result[field] = _safe_identifier(value, "unknown")
    return result


def gemini_latency_bucket(elapsed_seconds: float) -> str:
    milliseconds = max(0, int(elapsed_seconds * 1000))
    if milliseconds < 1_000:
        return "under_1s"
    if milliseconds < 5_000:
        return "1s_5s"
    if milliseconds < 15_000:
        return "5s_15s"
    if milliseconds < 30_000:
        return "15s_30s"
    if milliseconds < 60_000:
        return "30s_60s"
    return "60s_plus"


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


def _safe_provider_category(
    chain: list[Exception],
    *,
    status: int | None,
    status_name: str | None,
    provider_request_attempted: bool,
) -> str:
    failure_category = next(
        (
            value
            for item in chain
            if isinstance((metadata := getattr(item, "metadata", None)), dict)
            and isinstance((value := metadata.get("failure_category")), str)
        ),
        None,
    )
    if isinstance(failure_category, str) and failure_category in _RESPONSE_PARSE_FAILURES:
        return "response_parse_error"
    if any(isinstance(item, StructuredResponseError) for item in chain):
        return "response_parse_error"
    if any(type(item).__name__ in _TIMEOUT_EXCEPTION_NAMES for item in chain):
        return "timeout"
    if any(type(item).__name__ in _CONNECTION_EXCEPTION_NAMES for item in chain):
        return "connection_error"
    if any(isinstance(item, GeminiRequestConfigurationError) for item in chain):
        return "local_request_validation"
    if any(_is_pydantic_validation_error(item) for item in chain):
        return "local_request_validation"
    if not provider_request_attempted and any(
        isinstance(item, (TypeError, ValueError)) for item in chain
    ):
        return "local_request_validation"

    if status_name == "INVALID_ARGUMENT":
        return "invalid_argument"
    if status_name == "FAILED_PRECONDITION":
        return "failed_precondition"
    if status_name == "UNAUTHENTICATED":
        return "authentication_failed"
    if status_name == "PERMISSION_DENIED":
        return "permission_denied"
    if status_name == "NOT_FOUND":
        return "model_or_endpoint_not_found"
    if status_name in {"ABORTED", "ALREADY_EXISTS"}:
        return "conflict"
    if status_name == "RESOURCE_EXHAUSTED":
        return "rate_limited"
    if status_name == "INTERNAL":
        return "provider_internal"
    if status_name == "UNAVAILABLE":
        return "provider_unavailable"
    if status_name in {"DEADLINE_EXCEEDED", "CANCELLED"}:
        return "timeout"

    return {
        400: "invalid_argument",
        401: "authentication_failed",
        403: "permission_denied",
        404: "model_or_endpoint_not_found",
        409: "conflict",
        429: "rate_limited",
        500: "provider_internal",
        503: "provider_unavailable",
    }.get(status, "unknown_provider_error")


def _safe_provider_status_name(exc: Exception) -> str | None:
    for attribute in ("status", "status_name"):
        value = getattr(exc, attribute, None)
        if hasattr(value, "name"):
            value = getattr(value, "name")
        normalized = str(value or "").strip().upper().replace(" ", "_")
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", normalized):
            return normalized
    code = getattr(exc, "code", None)
    if not isinstance(code, int):
        if hasattr(code, "name"):
            code = getattr(code, "name")
        normalized = str(code or "").strip().upper().replace(" ", "_")
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", normalized):
            return normalized
    return None


def _is_pydantic_validation_error(exc: Exception) -> bool:
    return (
        type(exc).__name__ in {"ValidationError", "SchemaError"}
        and type(exc).__module__.startswith(("pydantic", "pydantic_core"))
    )


def _exception_chain(exc: Exception) -> tuple[Exception, ...]:
    chain: list[Exception] = []
    current: BaseException | None = exc
    while isinstance(current, Exception) and current not in chain and len(chain) < 5:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _safe_identifier(value: object, fallback: str) -> str:
    normalized = str(value or "").strip()
    return (
        normalized
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", normalized)
        else fallback
    )


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
