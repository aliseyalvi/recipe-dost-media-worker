from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.runtime_config import (
    clear_worker_config_cache,
    selected_worker_environment,
    worker_config_report,
)

RESOLVER_KEYS = (
    "MEDIA_RESOLVER_MODE",
    "EXTERNAL_MEDIA_RESOLVER_AUDIENCE",
    "INSTAGRAM_RESOLVER_CHAIN",
    "TIKTOK_RESOLVER_CHAIN",
    "COOKIE_MEDIA_FALLBACK_ENABLED",
    "YOUTUBE_DIRECT_GEMINI_ENABLED",
)


def _safe_validation_reason(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        if not errors:
            return "settings validation failed"
        first = errors[0]
        location = ".".join(str(value) for value in first.get("loc", ())) or "settings"
        return f"{location}: {first.get('msg', 'invalid value')}"
    if isinstance(exc, (RuntimeError, ValueError)):
        return str(exc)
    return f"{type(exc).__name__}: configuration validation failed"


def check_external_resolver_config(environment: str) -> dict[str, object]:
    has_runtime_selector = bool(
        (os.environ.get("APP_ENV") or "").strip()
        or (os.environ.get("RENDER_GIT_BRANCH") or "").strip()
        or (os.environ.get("RENDER") or "").strip()
    )
    selected, selection_source = selected_worker_environment(
        None if has_runtime_selector else environment
    )
    if selected != environment:
        raise RuntimeError(
            f"Requested {environment} diagnostic, but runtime selected {selected}."
        )
    os.environ["APP_ENV"] = selected
    clear_worker_config_cache()

    from app.config import Settings, validate_external_resolver_runtime_config

    settings = Settings(
        _env_file=None,
        worker_api_secret="diagnostic-only",
        supabase_url="https://diagnostic.invalid",
        supabase_service_role_key="diagnostic-only",
    )
    validate_external_resolver_runtime_config(settings)
    report = worker_config_report(selected)
    source_values = report.get("field_sources")
    sources = source_values if isinstance(source_values, dict) else {}
    return {
        "requested_environment": environment,
        "selected_environment": selected,
        "environment_selection_source": selection_source,
        "selected_config_file": report.get("selected_config_display_path"),
        "selected_config_exists": bool(report.get("selected_config_exists")),
        "selected_config_loaded": bool(report.get("selected_config_loaded")),
        "resolver_mode": settings.media_resolver_mode,
        "external_audience": settings.external_media_resolver_audience,
        "instagram_adapter_ids": list(settings.instagram_resolver_chain),
        "tiktok_adapter_ids": list(settings.tiktok_resolver_chain),
        "cookie_fallback_enabled": settings.cookie_media_fallback_enabled,
        "youtube_direct_enabled": settings.youtube_direct_gemini_enabled,
        "instagram_secret_configured": settings.instagram_rapidapi_key is not None,
        "tiktok_secret_configured": settings.tiktok_akng_access_token is not None,
        "field_sources": {
            key.lower(): sources.get(key, "model_default") for key in RESOLVER_KEYS
        },
        "validation": "passed",
    }


def print_summary(summary: dict[str, object]) -> None:
    for key in (
        "requested_environment",
        "selected_environment",
        "environment_selection_source",
        "selected_config_file",
        "selected_config_exists",
        "selected_config_loaded",
        "resolver_mode",
        "external_audience",
        "instagram_adapter_ids",
        "tiktok_adapter_ids",
        "cookie_fallback_enabled",
        "youtube_direct_enabled",
        "instagram_secret_configured",
        "tiktok_secret_configured",
        "validation",
    ):
        print(f"{key}={summary[key]}")
    sources = summary.get("field_sources", {})
    if isinstance(sources, dict):
        for key in sorted(sources):
            print(f"source.{key}={sources[key]}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate file-based external resolver configuration safely."
    )
    parser.add_argument("environment", choices=("development", "production"))
    args = parser.parse_args()
    try:
        summary = check_external_resolver_config(args.environment)
    except Exception as exc:
        print(f"validation=failed", file=sys.stderr)
        print(f"safe_failure_reason={_safe_validation_reason(exc)}", file=sys.stderr)
        return 1
    print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
