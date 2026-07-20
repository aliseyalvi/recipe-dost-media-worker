from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Mapping
from urllib.parse import urlparse

import sentry_sdk

logger = logging.getLogger("recipe_dost.worker")

SERVICE_NAME = "recipe-dost-media-worker"
TRUE_VALUES = {"1", "true", "yes", "on"}
ALLOWED_FEATURES = {
    "worker_runtime",
    "metadata",
    "ai_generation",
    "persistence",
    "worker_loop",
}
ALLOWED_OPERATIONS = {
    "worker_startup",
    "poll_queue",
    "process_job",
    "extract_metadata",
    "generate_recipe",
    "persist_metadata",
    "persist_recipe_card",
    "persist_job_failure",
    "controlled_test",
}
ALLOWED_STAGES = {
    "startup",
    "queue_iteration",
    "queue_claim",
    "unified_extractor",
    "youtube_metadata",
    "metadata_save",
    "private_metadata_save",
    "source_metadata_update",
    "youtube_gemini_url",
    "gemini_file_upload",
    "gemini_file_processing",
    "gemini_structured_extraction",
    "gemini_video_analysis",
    "recipe_card_saving",
    "job_completion_update",
    "terminal_failure",
    "verification",
}
ALLOWED_ERROR_CODES = {
    "sentry_verification",
    "worker_startup_failed",
    "queue_loop_iteration_failed",
    "queue_job_failed",
    "unexpected_worker_error",
    "unified_extractor_failed",
    "private_metadata_save_failed",
    "source_metadata_update_failed",
    "recipe_card_insert_failed",
    "job_completion_update_failed",
    "gemini_file_upload_failed",
    "gemini_file_processing_failed",
    "youtube_gemini_url_failed",
    "gemini_video_analysis_failed",
    "terminal_failure_persistence_failed",
    "media_size_exceeded",
}
ALLOWED_PLATFORMS = {"instagram", "tiktok", "youtube", "unknown"}
EXPECTED_PRODUCT_ERROR_CODES = {
    "invalid_url",
    "invalid_tiktok_url",
    "unsupported_source",
    "unsupported_social_platform",
    "media_unavailable",
    "metadata_unavailable",
    "youtube_oembed_failed",
    "instagram_metadata_unavailable",
    "tiktok_metadata_unavailable",
    "youtube_metadata_unavailable",
    "deleted_media",
    "private_media",
    "source_authentication_required",
    "instagram_cookies_missing",
    "video_duration_exceeded",
    "media_size_exceeded",
    "media_memory_limit_exceeded",
    "media_unsupported_type",
    "video_required_for_ai",
    "gemini_api_key_missing",
    "gemini_auth_error",
    "gemini_rate_limited",
    "gemini_quota_exceeded",
    "gemini_safety_refusal",
    "gemini_structured_extraction_failed",
    "gemini_recipe_json_invalid",
    "recipe_quality_gate_failed",
    "recipe_tags_invalid",
    "recipe_item_required",
}
EXPECTED_ERROR_MARKERS = (
    "invalid_url",
    "unsupported",
    "not_found",
    "unavailable",
    "deleted_media",
    "private_media",
    "authentication_required",
    "access_denied",
    "quota",
    "rate_limited",
    "safety_refusal",
    "schema_validation",
    "quality_gate",
    "tags_invalid",
    "duration_exceeded",
    "size_exceeded",
)
EXPECTED_HTTP_STATUSES = {400, 401, 403, 404, 409, 422, 429}
ALLOWED_TAG_KEYS = {
    "service",
    "flow",
    "feature",
    "operation",
    "stage",
    "error_code",
    "source_platform",
}
ALLOWED_CONTEXT_KEYS = {
    "retry_attempt",
    "retry_exhausted",
    "recoverable",
    "terminal",
    "safe_http_status",
    "duration_bucket",
    "size_bucket",
    "selected_resolution_bucket",
    "adaptive_attempt_count",
    "cookie_retry_used",
}
MEDIA_SIZE_WARNING_MESSAGE = "worker_outcome:media_size_exceeded"
MEDIA_SIZE_FINGERPRINT = ["worker_outcome", "media_size_exceeded"]
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
SAFE_RELEASE = re.compile(r"^recipe-dost-media-worker@[A-Za-z0-9._-]{7,64}$")


@dataclass(frozen=True)
class SentryRuntimeConfig:
    enabled: bool
    environment: str | None
    release: str | None
    dsn: str | None
    controlled_test: bool
    reason: str


_state_lock = threading.Lock()
_initialization_attempted = False
_initialized = False
_reported_error_ids: set[int] = set()


def _env_true(value: str | None) -> bool:
    return bool(value and value.strip().lower() in TRUE_VALUES)


def _is_render(environment: Mapping[str, str]) -> bool:
    return _env_true(environment.get("RENDER"))


def is_valid_sentry_dsn(value: str | None) -> bool:
    if not value or not value.strip():
        return False
    try:
        parsed = urlparse(value.strip())
        _ = parsed.port
    except (TypeError, ValueError):
        return False
    project_id = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.username
        and project_id
        and not parsed.fragment
    )


def compute_sentry_config(
    *,
    controlled_test: bool = False,
    environment: Mapping[str, str] | None = None,
) -> SentryRuntimeConfig:
    values = environment if environment is not None else os.environ
    worker_env = values.get("APP_ENV", "").strip().lower()
    dsn = values.get("SENTRY_DSN")
    render_branch = values.get("RENDER_GIT_BRANCH", "").strip()
    on_render = _is_render(values)
    commit = values.get("RENDER_GIT_COMMIT", "").strip()
    release = (
        f"{SERVICE_NAME}@{commit}"
        if commit and re.fullmatch(r"[A-Za-z0-9._-]{7,64}", commit)
        else None
    )

    if not is_valid_sentry_dsn(dsn):
        return SentryRuntimeConfig(False, None, release, None, controlled_test, "dsn_missing_or_invalid")
    if not on_render:
        return SentryRuntimeConfig(False, None, release, None, controlled_test, "render_service_required")

    if controlled_test:
        if worker_env != "development":
            return SentryRuntimeConfig(False, None, release, None, True, "test_requires_development")
        if not _env_true(values.get("SENTRY_DEV_TEST")):
            return SentryRuntimeConfig(False, None, release, None, True, "test_flag_disabled")
        if render_branch != "develop":
            return SentryRuntimeConfig(False, None, release, None, True, "render_branch_mismatch")
        return SentryRuntimeConfig(True, "development-test", release, dsn, True, "enabled")

    if worker_env != "production":
        return SentryRuntimeConfig(False, None, release, None, False, "runtime_requires_production")
    if render_branch != "main":
        return SentryRuntimeConfig(False, None, release, None, False, "render_branch_mismatch")
    return SentryRuntimeConfig(True, "production", release, dsn, False, "enabled")


def initialize_sentry(*, controlled_test: bool = False) -> bool:
    global _initialization_attempted, _initialized
    with _state_lock:
        if _initialized:
            return True
        config = compute_sentry_config(controlled_test=controlled_test)
        if not config.enabled:
            return False
        if _initialization_attempted:
            return False
        _initialization_attempted = True
        try:
            sentry_sdk.init(
                dsn=config.dsn,
                environment=config.environment,
                release=config.release,
                send_default_pii=False,
                traces_sample_rate=0.0,
                profiles_sample_rate=0.0,
                include_local_variables=False,
                include_source_context=False,
                max_request_body_size="never",
                default_integrations=False,
                auto_enabling_integrations=False,
                enable_logs=False,
                before_send=before_send,
                before_breadcrumb=before_breadcrumb,
            )
        except Exception as exc:
            logger.warning(
                "sentry_initialization_failed",
                extra={"exception_type": type(exc).__name__},
            )
            return False
        _initialized = True
        return True


def before_breadcrumb(_breadcrumb: dict[str, Any], _hint: dict[str, Any]) -> None:
    # Current-flow reporting is intentionally breadcrumb-free. This drops HTTP,
    # logging/console, database, AI, and any future automatic breadcrumb types.
    return None


def _safe_identifier(value: object, fallback: str) -> str:
    text = value if isinstance(value, str) else ""
    return text if SAFE_IDENTIFIER.fullmatch(text) else fallback


def _sanitize_filename(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("\\", "/")
    app_index = normalized.rfind("/app/")
    if app_index >= 0:
        normalized = normalized[app_index + 1 :]
    elif not normalized.startswith("app/"):
        normalized = PurePath(normalized).name
    return normalized if re.fullmatch(r"[A-Za-z0-9_./-]{1,180}", normalized) else None


def _sanitize_frames(stacktrace: object) -> dict[str, list[dict[str, object]]] | None:
    if not isinstance(stacktrace, dict) or not isinstance(stacktrace.get("frames"), list):
        return None
    frames: list[dict[str, object]] = []
    for raw_frame in stacktrace["frames"][-50:]:
        if not isinstance(raw_frame, dict):
            continue
        frame: dict[str, object] = {}
        filename = _sanitize_filename(raw_frame.get("filename"))
        if filename:
            frame["filename"] = filename
        function = raw_frame.get("function")
        if isinstance(function, str) and SAFE_IDENTIFIER.fullmatch(function):
            frame["function"] = function
        module = raw_frame.get("module")
        if isinstance(module, str) and SAFE_IDENTIFIER.fullmatch(module):
            frame["module"] = module
        for key in ("lineno", "colno"):
            value = raw_frame.get(key)
            if isinstance(value, int) and value >= 0:
                frame[key] = value
        if isinstance(raw_frame.get("in_app"), bool):
            frame["in_app"] = raw_frame["in_app"]
        if frame:
            frames.append(frame)
    return {"frames": frames} if frames else None


def _sanitize_exception(event: Mapping[str, Any], error_code: str) -> dict[str, object] | None:
    exception = event.get("exception")
    if not isinstance(exception, dict) or not isinstance(exception.get("values"), list):
        return None
    values: list[dict[str, object]] = []
    for raw_value in exception["values"][-5:]:
        if not isinstance(raw_value, dict):
            continue
        value: dict[str, object] = {
            "type": _safe_identifier(raw_value.get("type"), "WorkerError"),
            # Never retain the original exception message. It may contain a URL,
            # token, prompt, recipe content, response body, or provider payload.
            "value": error_code,
        }
        module = raw_value.get("module")
        if isinstance(module, str) and SAFE_IDENTIFIER.fullmatch(module):
            value["module"] = module
        stacktrace = _sanitize_frames(raw_value.get("stacktrace"))
        if stacktrace:
            value["stacktrace"] = stacktrace
        values.append(value)
    return {"values": values} if values else None


def _sanitize_tags(event: Mapping[str, Any]) -> dict[str, str] | None:
    raw_tags = event.get("tags")
    if not isinstance(raw_tags, dict):
        return None
    tags = {
        key: str(raw_tags[key])[:128]
        for key in ALLOWED_TAG_KEYS
        if key in raw_tags and isinstance(raw_tags[key], (str, int, bool))
    }
    if tags.get("service") != SERVICE_NAME:
        return None
    if tags.get("flow") not in {"current", "process"}:
        return None
    if tags.get("feature") not in ALLOWED_FEATURES:
        return None
    if tags.get("operation") not in ALLOWED_OPERATIONS:
        return None
    if tags.get("stage") not in ALLOWED_STAGES:
        return None
    if tags.get("error_code") not in ALLOWED_ERROR_CODES:
        return None
    if tags.get("source_platform", "unknown") not in ALLOWED_PLATFORMS:
        return None
    if tags.get("flow") == "process" and tags.get("feature") != "worker_runtime":
        return None
    return tags


def _sanitize_worker_context(event: Mapping[str, Any]) -> dict[str, object] | None:
    contexts = event.get("contexts")
    if not isinstance(contexts, dict) or not isinstance(contexts.get("worker"), dict):
        return None
    worker = contexts["worker"]
    sanitized: dict[str, object] = {}
    for key in ALLOWED_CONTEXT_KEYS:
        value = worker.get(key)
        if key in {
            "retry_exhausted",
            "recoverable",
            "terminal",
            "cookie_retry_used",
        } and isinstance(value, bool):
            sanitized[key] = value
        elif key in {
            "retry_attempt",
            "safe_http_status",
            "adaptive_attempt_count",
        } and isinstance(value, int) and 0 <= value <= 1000:
            sanitized[key] = value
        elif key in {
            "duration_bucket",
            "size_bucket",
            "selected_resolution_bucket",
        } and isinstance(value, str) and SAFE_IDENTIFIER.fullmatch(value):
            sanitized[key] = value
    return sanitized or None


def _sanitize_message(event: Mapping[str, Any], error_code: str) -> str | None:
    message = event.get("message")
    if (
        error_code == "media_size_exceeded"
        and message == MEDIA_SIZE_WARNING_MESSAGE
        and event.get("level") == "warning"
    ):
        return MEDIA_SIZE_WARNING_MESSAGE
    return None


def before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
    tags = _sanitize_tags(event)
    if tags is None:
        return None
    exception = _sanitize_exception(event, tags["error_code"])
    message = _sanitize_message(event, tags["error_code"])
    if exception is None and message is None:
        return None

    sanitized: dict[str, Any] = {
        "platform": "python",
        "level": "warning" if message is not None else "error",
        "tags": tags,
    }
    if exception is not None:
        sanitized["exception"] = exception
    if message is not None:
        sanitized["message"] = message
        if event.get("fingerprint") == MEDIA_SIZE_FINGERPRINT:
            sanitized["fingerprint"] = list(MEDIA_SIZE_FINGERPRINT)
    event_id = event.get("event_id")
    if isinstance(event_id, str) and re.fullmatch(r"[a-fA-F0-9]{32}", event_id):
        sanitized["event_id"] = event_id
    timestamp = event.get("timestamp")
    if timestamp is not None:
        sanitized["timestamp"] = timestamp
    environment = event.get("environment")
    if environment in {"production", "development-test"}:
        sanitized["environment"] = environment
    release = event.get("release")
    if isinstance(release, str) and SAFE_RELEASE.fullmatch(release):
        sanitized["release"] = release
    worker_context = _sanitize_worker_context(event)
    if worker_context:
        sanitized["contexts"] = {"worker": worker_context}
    return sanitized


def _http_status_from_error(error: BaseException) -> int | None:
    for value in (
        getattr(error, "status_code", None),
        getattr(error, "code", None),
        getattr(getattr(error, "response", None), "status_code", None),
    ):
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    return None


def is_expected_product_failure(error_code: str, safe_http_status: int | None = None) -> bool:
    normalized = error_code.strip().lower()
    return bool(
        normalized in EXPECTED_PRODUCT_ERROR_CODES
        or any(marker in normalized for marker in EXPECTED_ERROR_MARKERS)
        or safe_http_status in EXPECTED_HTTP_STATUSES
    )


def report_worker_error(
    error: object,
    *,
    feature: str,
    operation: str,
    stage: str,
    error_code: str,
    source_platform: str | None = None,
    retry_attempt: int | None = None,
    retry_exhausted: bool | None = None,
    recoverable: bool | None = None,
    terminal: bool | None = None,
    safe_http_status: int | None = None,
) -> str | None:
    if feature not in ALLOWED_FEATURES:
        return None
    if operation not in ALLOWED_OPERATIONS or stage not in ALLOWED_STAGES:
        return None
    if retry_attempt is not None and retry_exhausted is not True:
        return None
    if terminal is not True and retry_exhausted is not True and feature not in {
        "worker_loop",
        "worker_runtime",
    }:
        return None

    exception = error if isinstance(error, BaseException) else RuntimeError("non_exception_error")
    status = safe_http_status or _http_status_from_error(exception)
    if is_expected_product_failure(error_code, status):
        return None
    normalized_error_code = (
        error_code if error_code in ALLOWED_ERROR_CODES else "unexpected_worker_error"
    )
    platform = (source_platform or "unknown").strip().lower()
    if platform not in ALLOWED_PLATFORMS:
        platform = "unknown"
    if not initialize_sentry():
        return None

    error_id = id(exception)
    with _state_lock:
        if error_id in _reported_error_ids:
            return None
        if len(_reported_error_ids) >= 1024:
            _reported_error_ids.clear()
        _reported_error_ids.add(error_id)

    tags = {
        "service": SERVICE_NAME,
        "flow": "current",
        "feature": feature,
        "operation": operation,
        "stage": stage,
        "error_code": normalized_error_code,
        "source_platform": platform,
    }
    worker_context = {
        key: value
        for key, value in {
            "retry_attempt": retry_attempt,
            "retry_exhausted": retry_exhausted,
            "recoverable": recoverable,
            "terminal": terminal,
            "safe_http_status": status,
        }.items()
        if value is not None
    }
    try:
        with sentry_sdk.new_scope() as scope:
            scope.set_tags(tags)
            scope.set_context("worker", worker_context)
            event_id = sentry_sdk.capture_exception(exception)
        return str(event_id) if event_id else None
    except Exception as capture_error:
        logger.warning(
            "sentry_capture_failed",
            extra={"exception_type": type(capture_error).__name__},
        )
        return None


def report_terminal_worker_outcome(
    *,
    error_code: str,
    source_platform: str | None,
    duration_bucket: str | None,
    size_bucket: str | None,
    selected_resolution_bucket: str | None,
    adaptive_attempt_count: int,
    cookie_retry_used: bool,
) -> str | None:
    if error_code != "media_size_exceeded" or not initialize_sentry():
        return None
    platform = (source_platform or "unknown").strip().lower()
    if platform not in ALLOWED_PLATFORMS:
        platform = "unknown"
    tags = {
        "service": SERVICE_NAME,
        "flow": "current",
        "feature": "metadata",
        "operation": "extract_metadata",
        "stage": "unified_extractor",
        "error_code": "media_size_exceeded",
        "source_platform": platform,
    }
    worker_context = {
        "duration_bucket": duration_bucket or "unknown",
        "size_bucket": size_bucket or "unknown",
        "selected_resolution_bucket": selected_resolution_bucket or "unknown",
        "adaptive_attempt_count": max(0, min(adaptive_attempt_count, 4)),
        "cookie_retry_used": bool(cookie_retry_used),
        "terminal": True,
    }
    try:
        with sentry_sdk.new_scope() as scope:
            scope.set_tags(tags)
            scope.set_context("worker", worker_context)
            scope.fingerprint = list(MEDIA_SIZE_FINGERPRINT)
            event_id = sentry_sdk.capture_message(
                MEDIA_SIZE_WARNING_MESSAGE,
                level="warning",
            )
        return str(event_id) if event_id else None
    except Exception as capture_error:
        logger.warning(
            "sentry_capture_failed",
            extra={"exception_type": type(capture_error).__name__},
        )
        return None


def flush_sentry(timeout: float = 2.0) -> None:
    if not _initialized:
        return
    try:
        sentry_sdk.flush(timeout=max(0.0, min(timeout, 5.0)))
    except Exception as exc:
        logger.warning(
            "sentry_flush_failed",
            extra={"exception_type": type(exc).__name__},
        )


def _reset_state_for_tests() -> None:
    global _initialization_attempted, _initialized
    with _state_lock:
        _initialization_attempted = False
        _initialized = False
        _reported_error_ids.clear()
