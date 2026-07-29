from __future__ import annotations

from time import perf_counter
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, model_validator

from app.ai.gemini_reliability import (
    StructuredResponseError,
    gemini_latency_bucket,
    safe_gemini_provider_error,
)
from app.config import Settings
from app.queue_worker import (
    _GEMINI_CALL_CONTEXT,
    build_youtube_gemini_input_part,
    create_gemini_client,
    extract_gemini_text,
    guarded_generate_content,
    run_structured_extraction_call,
)
from app.social.platforms import detect_social_platform
from app.queue_worker import extract_youtube_video_id

GeminiDiagnosticModel = Literal["gemini-3.1-flash-lite", "gemini-2.5-flash"]
GeminiDiagnosticLevel = Literal[
    "plain_text",
    "tiny_schema",
    "recipe_schema_text",
    "youtube_production_shape",
]

DIAGNOSTIC_VERSION = "gd6_v1"
_PLAIN_TEXT_PROMPT = "Reply with the single word OK."
_TINY_SCHEMA_PROMPT = "Return JSON indicating success."
_RECIPE_TEXT_INPUT = (
    "A cook combines rice, water, and salt, simmers until tender, and serves it."
)


class TinyDiagnosticSchema(BaseModel):
    ok: bool


class GeminiRequestLadderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: GeminiDiagnosticModel
    level: GeminiDiagnosticLevel
    youtube_url: str | None = None

    @model_validator(mode="after")
    def validate_youtube_input(self) -> "GeminiRequestLadderRequest":
        if self.level == "youtube_production_shape":
            if not _valid_https_youtube_url(self.youtube_url):
                raise ValueError("A valid HTTPS YouTube URL is required for this level.")
        elif self.youtube_url is not None:
            raise ValueError("youtube_url is accepted only for the YouTube level.")
        return self


class GeminiRequestLadderResponse(BaseModel):
    diagnostic_version: Literal["gd6_v1"] = DIAGNOSTIC_VERSION
    model: GeminiDiagnosticModel
    level: GeminiDiagnosticLevel
    success: bool
    provider_request_attempted: bool
    provider_http_status: int | None
    provider_status_name: str | None
    provider_error_category: str | None
    sdk_exception_type: str | None
    retryable: bool | None
    failure_stage: str | None
    response_received: bool
    structured_parse_succeeded: bool | None
    latency_bucket: str


def run_gemini_request_ladder(
    request: GeminiRequestLadderRequest,
    settings: Settings,
) -> GeminiRequestLadderResponse:
    started_at = perf_counter()
    provider_call_started = False
    response_received = False
    structured_parse_succeeded: bool | None = (
        None if request.level == "plain_text" else False
    )
    failure_stage = "client_configuration"
    context_token = _GEMINI_CALL_CONTEXT.set(None)
    try:
        if settings.gemini_api_key is None:
            raise ValueError("Gemini diagnostic credential is not configured.")
        client = create_gemini_client(
            settings.gemini_api_key.get_secret_value(),
            settings.gemini_file_upload_timeout_seconds,
        )

        failure_stage = "provider_request"
        if request.level == "plain_text":
            provider_call_started = True
            guarded_generate_content(
                client,
                model=request.model,
                contents=[_PLAIN_TEXT_PROMPT],
                config={"temperature": 0.0, "max_output_tokens": 32},
                stage="gemini_request_ladder",
                call_purpose="analysis",
            )
            response_received = True
        elif request.level == "tiny_schema":
            provider_call_started = True
            response = guarded_generate_content(
                client,
                model=request.model,
                contents=[_TINY_SCHEMA_PROMPT],
                config={
                    "temperature": 0.0,
                    "max_output_tokens": 64,
                    "response_mime_type": "application/json",
                    "response_schema": TinyDiagnosticSchema,
                },
                stage="gemini_request_ladder",
                call_purpose="analysis",
            )
            response_received = True
            failure_stage = "response_parse"
            _validate_tiny_response(response)
            structured_parse_succeeded = True
        elif request.level == "recipe_schema_text":
            provider_call_started = True
            run_structured_extraction_call(
                client=client,
                input_part=_RECIPE_TEXT_INPUT,
                model=request.model,
                settings=settings,
                source_metadata={},
                platform="youtube",
            )
            response_received = True
            structured_parse_succeeded = True
        else:
            failure_stage = "request_construction"
            youtube_part = build_youtube_gemini_input_part(request.youtube_url or "")
            failure_stage = "provider_request"
            provider_call_started = True
            run_structured_extraction_call(
                client=client,
                input_part=youtube_part,
                model=request.model,
                settings=settings,
                source_metadata={},
                platform="youtube",
            )
            response_received = True
            structured_parse_succeeded = True

        return GeminiRequestLadderResponse(
            model=request.model,
            level=request.level,
            success=True,
            provider_request_attempted=True,
            provider_http_status=None,
            provider_status_name=None,
            provider_error_category=None,
            sdk_exception_type=None,
            retryable=None,
            failure_stage=None,
            response_received=response_received,
            structured_parse_succeeded=structured_parse_succeeded,
            latency_bucket=gemini_latency_bucket(perf_counter() - started_at),
        )
    except Exception as exc:
        safe_error = safe_gemini_provider_error(
            exc,
            provider_request_attempted=provider_call_started,
            failure_stage=failure_stage,
            model=request.model,
            input_mode=(
                "youtube_url"
                if request.level == "youtube_production_shape"
                else "text"
            ),
            request_profile=request.level,
            latency_bucket=gemini_latency_bucket(perf_counter() - started_at),
        )
        if (
            safe_error["provider_error_category"] == "local_request_validation"
            and safe_error["provider_request_attempted"] is False
        ):
            safe_error["failure_stage"] = "request_validation"
        elif safe_error["provider_error_category"] == "response_parse_error":
            safe_error["failure_stage"] = "response_parse"
        response_received = bool(safe_error["provider_http_status"]) or (
            safe_error["provider_error_category"] == "response_parse_error"
        )
        return GeminiRequestLadderResponse(
            model=request.model,
            level=request.level,
            success=False,
            provider_request_attempted=bool(
                safe_error["provider_request_attempted"]
            ),
            provider_http_status=_optional_int(
                safe_error["provider_http_status"]
            ),
            provider_status_name=_optional_str(
                safe_error["provider_status_name"]
            ),
            provider_error_category=str(safe_error["provider_error_category"]),
            sdk_exception_type=str(safe_error["sdk_exception_type"]),
            retryable=_optional_bool(safe_error["retryable"]),
            failure_stage=str(safe_error["failure_stage"]),
            response_received=response_received,
            structured_parse_succeeded=structured_parse_succeeded,
            latency_bucket=str(safe_error["latency_bucket"]),
        )
    finally:
        _GEMINI_CALL_CONTEXT.reset(context_token)


def _validate_tiny_response(response: object) -> None:
    parsed = getattr(response, "parsed", None)
    try:
        if parsed is not None:
            TinyDiagnosticSchema.model_validate(parsed)
            return
        TinyDiagnosticSchema.model_validate_json(extract_gemini_text(response))
    except Exception:
        raise StructuredResponseError("gemini_json_parse_failed") from None


def _valid_https_youtube_url(value: str | None) -> bool:
    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and detect_social_platform(str(value)) == "youtube"
        and extract_youtube_video_id(str(value)) is not None
    )


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None
