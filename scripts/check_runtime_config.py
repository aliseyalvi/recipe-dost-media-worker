from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.runtime_config import load_worker_config, worker_config_report


class RuntimeConfigCheckError(Exception):
    pass


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _effective_value(config: dict[str, Any], key: str) -> Any:
    environment_value = os.environ.get(key)
    if environment_value is None or not environment_value.strip():
        return config.get(key)
    return environment_value


def _read_environment_config(environment: str) -> dict[str, Any]:
    path = REPO_ROOT / "config" / f"worker.{environment}.json"
    if not path.exists():
        return {}
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeConfigCheckError(f"{path.name} must contain a JSON object")
    return {str(key).upper(): value for key, value in parsed.items()}


def check_runtime_config(environment: str) -> dict[str, object]:
    config = load_worker_config(environment)
    report = worker_config_report(environment)
    errors: list[str] = []

    if not report.get("defaults_config_loaded"):
        errors.append("worker.defaults.json was not loaded")

    if environment == "production":
        if not report.get("production_config_loaded"):
            errors.append("worker.production.json was not loaded")

        expected_strings = {
            "WORKER_EXECUTION_MODE": "queue",
            "AI_ANALYSIS_MODE": "gemini_video_poc",
            "MEDIA_EXTRACT_OUTPUT_MODE": "temp_file",
        }
        for key, expected in expected_strings.items():
            actual = str(_effective_value(config, key) or "").strip().lower()
            if actual != expected:
                errors.append(f"{key} effective value must be {expected}")

        for key in ("ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR",):
            if not _bool_value(_effective_value(config, key)):
                errors.append(f"{key} effective value must be true")

        if not _bool_value(
            _effective_value(config, "COOKIE_MEDIA_FALLBACK_ENABLED")
        ):
            errors.append(
                "COOKIE_MEDIA_FALLBACK_ENABLED effective value must be true"
            )

        for key in ("ENABLE_INSTAGRAM_COOKIES", "ENABLE_YTDLP_COOKIES"):
            if _bool_value(_effective_value(config, key)):
                errors.append(f"{key} effective value must be false")

        expected_resolver_strings = {
            "MEDIA_RESOLVER_MODE": "hybrid",
            "EXTERNAL_MEDIA_RESOLVER_AUDIENCE": "all_authorized_ai",
        }
        for key, expected in expected_resolver_strings.items():
            actual = str(_effective_value(config, key) or "").strip().lower()
            if actual != expected:
                errors.append(f"{key} effective value must be {expected}")

        if _effective_value(config, "INSTAGRAM_RESOLVER_CHAIN") != [
            "rapidapi_instagram_unified_v1"
        ]:
            errors.append(
                "INSTAGRAM_RESOLVER_CHAIN effective value must select "
                "rapidapi_instagram_unified_v1"
            )

        if not _bool_value(_effective_value(config, "YOUTUBE_DIRECT_GEMINI_ENABLED")):
            errors.append("YOUTUBE_DIRECT_GEMINI_ENABLED effective value must be true")

        if _bool_value(_effective_value(config, "ALLOW_RAW_URL_LOGS")):
            errors.append("ALLOW_RAW_URL_LOGS effective value must be false")

        try:
            max_duration = int(
                _effective_value(
                    config,
                    "MAX_SOURCE_VIDEO_DURATION_SECONDS",
                )
                or 0
            )
        except (TypeError, ValueError):
            errors.append(
                "MAX_SOURCE_VIDEO_DURATION_SECONDS effective value must be an integer"
            )
        else:
            if not 60 <= max_duration <= 900:
                errors.append(
                    "MAX_SOURCE_VIDEO_DURATION_SECONDS effective value must be "
                    "between 60 and 900"
                )

        try:
            preferred_size = int(_effective_value(config, "PREFERRED_VIDEO_SIZE_MB") or 0)
            hard_size = int(_effective_value(config, "MAX_VIDEO_SIZE_MB") or 0)
            provider_bytes = int(
                _effective_value(config, "GEMINI_FILE_MAX_SIZE_BYTES") or 0
            )
            min_height = int(_effective_value(config, "MIN_VIDEO_HEIGHT") or 0)
            max_height = int(_effective_value(config, "MAX_VIDEO_HEIGHT") or 0)
        except (TypeError, ValueError):
            errors.append("effective media size limits must be integers")
        else:
            if not 0 < preferred_size <= hard_size:
                errors.append("effective size limits must satisfy 0 < preferred <= hard")
            if hard_size * 1024 * 1024 > provider_bytes:
                errors.append("effective hard media limit exceeds provider limit")
            if not 360 <= min_height <= max_height <= 720:
                errors.append("effective video height limits are invalid")

        production_config = _read_environment_config("production")
        for key in ("ENABLE_TIKWM_RESOLVER", "TIKWM_API_URL"):
            if key in production_config:
                errors.append(f"{key} must not be a production-primary override")

    if errors:
        raise RuntimeConfigCheckError("; ".join(errors))

    return {
        "environment": environment,
        "config_dir_exists": bool(report.get("config_dir_exists")),
        "loaded_files": report.get("loaded_files", []),
        "missing_files": report.get("missing_files", []),
        "defaults_config_loaded": bool(report.get("defaults_config_loaded")),
        "production_config_loaded": bool(report.get("production_config_loaded")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check worker runtime config loading.")
    parser.add_argument("environment", choices=("development", "production"))
    args = parser.parse_args()

    try:
        summary = check_runtime_config(args.environment)
    except RuntimeConfigCheckError as exc:
        print(f"Runtime config check failed: {exc}", file=sys.stderr)
        return 1

    print("Runtime config check passed")
    for key, value in summary.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
