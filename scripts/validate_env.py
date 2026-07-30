from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_DIR = REPO_ROOT / "env"
CONFIG_DIR = REPO_ROOT / "config"
DEV_SUPABASE_REF = "xvsiuwjidmkdqzkswoki"
VALID_ENVIRONMENTS = {"development", "production"}
ENV_ONLY_REQUIRED_KEYS = [
    "APP_ENV",
    "SUPABASE_URL",
    "SUPABASE_SERVICE_ROLE_KEY",
    "GEMINI_API_KEY",
    "LOG_LEVEL",
]
ENV_ONLY_OPTIONAL_KEYS = [
    "MEDIA_WORKER_SHARED_SECRET",
    "WORKER_API_SECRET",
    "INSTAGRAM_COOKIES_B64",
    "YOUTUBE_COOKIES_B64",
    "FACEBOOK_COOKIES_B64",
    "TIKTOK_COOKIES_B64",
    "INSTAGRAM_RAPIDAPI_KEY",
    "TIKTOK_AKNG_ACCESS_TOKEN",
    "SENTRY_DSN",
    "SENTRY_DEV_CANARY",
    "SENTRY_DEV_TEST",
]
ENV_ONLY_EXPORT_KEYS = sorted(
    set(ENV_ONLY_REQUIRED_KEYS + ENV_ONLY_OPTIONAL_KEYS) - {"APP_ENV", "LOG_LEVEL"}
)
CONFIG_ONLY_KEYS = [
    "AI_ANALYSIS_MODE",
    "ALLOW_PRIVATE_METADATA_FAILURE",
    "ALLOW_RAW_URL_LOGS",
    "ENABLE_INSTAGRAM_COOKIES",
    "ENABLE_METADATA_ONLY_MOCK_FALLBACK",
    "ENABLE_QUEUE_WAKE_ENDPOINT",
    "ENABLE_RENDER_INSTAGRAM_RESOLVER",
    "ENABLE_TIKWM_RESOLVER",
    "ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR",
    "ENABLE_YTDLP_COOKIES",
    "GEMINI_EXTRACTION_MODE",
    "GEMINI_FILE_MAX_WAIT_SECONDS",
    "GEMINI_FILE_MAX_SIZE_BYTES",
    "GEMINI_FILE_POLL_INTERVAL_SECONDS",
    "GEMINI_FILE_UPLOAD_TIMEOUT_SECONDS",
    "GEMINI_JSON_FALLBACK_ENABLED",
    "GEMINI_JSON_RETRY_ENABLED",
    "GEMINI_PRIMARY_MODEL",
    "GEMINI_SAVE_MODE",
    "GEMINI_TEXT_MODEL",
    "GEMINI_VIDEO_ANALYSIS_MODE",
    "GEMINI_VIDEO_INPUT_MODE",
    "GEMINI_VIDEO_MODEL",
    "LOCAL_BRIEF_PARSER_ENABLED",
    "MAX_SOURCE_VIDEO_DURATION_SECONDS",
    "MAX_VIDEO_HEIGHT",
    "MAX_VIDEO_SIZE_MB",
    "MEDIA_ADAPTIVE_MAX_ATTEMPTS",
    "MEDIA_EXTRACT_OUTPUT_MODE",
    "MIN_VIDEO_HEIGHT",
    "MOCK_ANALYSIS_DELAY_MS",
    "MOCK_RECIPE_FIXTURE_PATH",
    "MOCK_VIDEO_PROCESSING_MODE",
    "QUEUE_EMPTY_SLEEP_SECONDS",
    "QUEUE_POLL_INTERVAL_SECONDS",
    "PREFERRED_VIDEO_SIZE_MB",
    "REAL_AI_FALLBACK_TO_MOCK",
    "RECIPE_CARD_NORMALIZATION_ENABLED",
    "TEMP_MEDIA_DIR",
    "THUMBNAIL_BUCKET",
    "TIKWM_API_URL",
    "WORKER_EXECUTION_MODE",
    "YOUTUBE_AI_INPUT_MODE",
    "YOUTUBE_METADATA_MODE",
]
PLACEHOLDER_MARKERS = (
    "replace-with-",
    "your-dev-project-ref",
    "your-prod-project-ref",
)


class EnvValidationError(Exception):
    pass


def env_path(environment: str) -> Path:
    return ENV_DIR / f"{environment}.env"


def _strip_optional_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise EnvValidationError(
            f"Missing env file: {path.relative_to(REPO_ROOT)}. "
            f"Create it from env/{path.stem}.example.env. "
            "If the existing root .env points to dev Supabase, migrate it to "
            "env/development.env without printing its contents."
        )

    values: dict[str, str] = {}
    errors: list[str] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            errors.append(f"line {line_number}: expected KEY=value")
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            errors.append(f"line {line_number}: missing key")
            continue
        values[key] = _strip_optional_quotes(value.strip())

    if errors:
        raise EnvValidationError(
            f"Invalid env syntax in {path.relative_to(REPO_ROOT)}: " + "; ".join(errors)
        )
    return values


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise EnvValidationError(f"Missing config file: {path.relative_to(REPO_ROOT)}")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EnvValidationError(
            f"Invalid JSON in {path.relative_to(REPO_ROOT)}: line {exc.lineno}"
        ) from exc
    if not isinstance(parsed, dict):
        raise EnvValidationError(f"{path.relative_to(REPO_ROOT)} must contain an object")
    return {str(key).upper(): value for key, value in parsed.items()}


def merged_config(environment: str) -> dict[str, Any]:
    config = _read_json_object(CONFIG_DIR / "worker.defaults.json")
    environment_path = CONFIG_DIR / f"worker.{environment}.json"
    if environment_path.exists():
        config.update(_read_json_object(environment_path))
    return config


def _require_bool_false(values: dict[str, str], key: str, errors: list[str]) -> None:
    if values.get(key, "").strip().lower() != "false":
        errors.append(f"{key} must be false")


def validate_values(
    environment: str,
    values: dict[str, str],
    *,
    allow_placeholders: bool = False,
) -> None:
    errors: list[str] = []

    missing = [key for key in ENV_ONLY_REQUIRED_KEYS if not values.get(key, "").strip()]
    if not (
        values.get("MEDIA_WORKER_SHARED_SECRET", "").strip()
        or values.get("WORKER_API_SECRET", "").strip()
    ):
        missing.append("MEDIA_WORKER_SHARED_SECRET or WORKER_API_SECRET")
    if missing:
        errors.append("missing required keys: " + ", ".join(missing))
    if not allow_placeholders:
        placeholder_keys = [
            key
            for key in ENV_ONLY_REQUIRED_KEYS
            + ["MEDIA_WORKER_SHARED_SECRET", "WORKER_API_SECRET", "SENTRY_DSN"]
            if any(marker in values.get(key, "") for marker in PLACEHOLDER_MARKERS)
        ]
        if placeholder_keys:
            errors.append(
                "placeholder values must be replaced for: "
                + ", ".join(placeholder_keys)
            )

    app_env = values.get("APP_ENV", "").strip().lower()
    if app_env and app_env != environment:
        errors.append(f"APP_ENV must be {environment}")

    if environment == "production":
        if values.get("LOG_LEVEL", "").strip().lower() != "info":
            errors.append("LOG_LEVEL must be info in production")
        joined_values = "\n".join(value.lower() for value in values.values())
        if DEV_SUPABASE_REF in joined_values:
            errors.append("production env must not contain the known dev Supabase ref")
        if values.get("SENTRY_DEV_TEST", "").strip().lower() not in {"", "false"}:
            errors.append("SENTRY_DEV_TEST must be false in production")
        if values.get("SENTRY_DEV_CANARY", "").strip().lower() not in {"", "false"}:
            errors.append("SENTRY_DEV_CANARY must be false in production")

    sentry_dev_test = values.get("SENTRY_DEV_TEST", "").strip().lower()
    if sentry_dev_test not in {"", "true", "false"}:
        errors.append("SENTRY_DEV_TEST must be true or false")
    if sentry_dev_test == "true" and not values.get("SENTRY_DSN", "").strip():
        errors.append("SENTRY_DEV_TEST=true requires SENTRY_DSN")

    sentry_dev_canary = values.get("SENTRY_DEV_CANARY", "").strip().lower()
    if sentry_dev_canary not in {"", "true", "false"}:
        errors.append("SENTRY_DEV_CANARY must be true or false")
    if sentry_dev_canary == "true" and not values.get("SENTRY_DSN", "").strip():
        errors.append("SENTRY_DEV_CANARY=true requires SENTRY_DSN")

    worker_api_secret = values.get("WORKER_API_SECRET", "").strip()
    media_secret = values.get("MEDIA_WORKER_SHARED_SECRET", "").strip()
    if worker_api_secret and media_secret and worker_api_secret != media_secret:
        errors.append("WORKER_API_SECRET must match MEDIA_WORKER_SHARED_SECRET when set")

    if errors:
        raise EnvValidationError("; ".join(errors))


def _config_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def validate_config(environment: str) -> dict[str, Any]:
    config = merged_config(environment)
    errors: list[str] = []

    try:
        max_duration = int(config.get("MAX_SOURCE_VIDEO_DURATION_SECONDS", 0))
    except (TypeError, ValueError):
        errors.append(
            "config MAX_SOURCE_VIDEO_DURATION_SECONDS must be an integer"
        )
    else:
        if not 60 <= max_duration <= 900:
            errors.append(
                "config MAX_SOURCE_VIDEO_DURATION_SECONDS must be between 60 and 900"
            )

    if _config_bool(config.get("ALLOW_RAW_URL_LOGS", False)):
        errors.append("config ALLOW_RAW_URL_LOGS must be false")

    try:
        preferred_size = int(config.get("PREFERRED_VIDEO_SIZE_MB", 0))
        hard_size = int(config.get("MAX_VIDEO_SIZE_MB", 0))
        provider_bytes = int(config.get("GEMINI_FILE_MAX_SIZE_BYTES", 0))
        adaptive_attempts = int(config.get("MEDIA_ADAPTIVE_MAX_ATTEMPTS", 0))
        min_height = int(config.get("MIN_VIDEO_HEIGHT", 0))
        max_height = int(config.get("MAX_VIDEO_HEIGHT", 0))
    except (TypeError, ValueError):
        errors.append("config media limits must be integers")
    else:
        if not 0 < preferred_size <= hard_size:
            errors.append(
                "config size limits must satisfy 0 < preferred <= hard"
            )
        if hard_size * 1024 * 1024 > provider_bytes:
            errors.append("config hard media limit must not exceed provider limit")
        if not 1 <= adaptive_attempts <= 4:
            errors.append("config adaptive attempt count must be between 1 and 4")
        if not 360 <= min_height <= max_height <= 720:
            errors.append(
                "config video heights must satisfy 360 <= minimum <= maximum <= 720"
            )

    if environment == "production":
        required_strings = {
            "AI_ANALYSIS_MODE": "gemini_video_poc",
            "GEMINI_EXTRACTION_MODE": "single_pass_structured",
            "GEMINI_PRIMARY_MODEL": "gemini-3.1-flash-lite",
            "MEDIA_EXTRACT_OUTPUT_MODE": "temp_file",
            "WORKER_EXECUTION_MODE": "queue",
            "YOUTUBE_AI_INPUT_MODE": "gemini_url",
            "YOUTUBE_METADATA_MODE": "oembed",
        }
        for key, expected in required_strings.items():
            actual = str(config.get(key, "")).strip().lower()
            if actual != expected:
                errors.append(f"production config {key} must be {expected}")
        required_true = [
            "ENABLE_RENDER_INSTAGRAM_RESOLVER",
            "ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR",
            "GEMINI_STRUCTURED_OUTPUT_ENABLED",
            "RECIPE_QUALITY_GATE_ENABLED",
            "RECIPE_REPAIR_ENABLED",
        ]
        for key in required_true:
            if not _config_bool(config.get(key, False)):
                errors.append(f"production config {key} must be true")
        fallback_false_keys = [
            "COOKIE_MEDIA_FALLBACK_ENABLED",
            "ENABLE_INSTAGRAM_COOKIES",
            "ENABLE_YTDLP_COOKIES",
        ]
        for key in fallback_false_keys:
            if _config_bool(config.get(key, False)):
                errors.append(f"production config {key} must be false")
        required_resolver_strings = {
            "MEDIA_RESOLVER_MODE": "legacy",
            "EXTERNAL_MEDIA_RESOLVER_AUDIENCE": "disabled",
        }
        for key, expected in required_resolver_strings.items():
            actual = str(config.get(key, "")).strip().lower()
            if actual != expected:
                errors.append(f"production config {key} must be {expected}")
        if not _config_bool(config.get("YOUTUBE_DIRECT_GEMINI_ENABLED", False)):
            errors.append("production config YOUTUBE_DIRECT_GEMINI_ENABLED must be true")
        if _config_bool(config.get("LOCAL_BRIEF_PARSER_ENABLED", False)):
            errors.append("production config LOCAL_BRIEF_PARSER_ENABLED must be false")
        if _config_bool(config.get("REAL_AI_FALLBACK_TO_MOCK", False)):
            errors.append("production config must not enable real AI fallback to mock")
        _validate_queue_process_job_compatibility(errors)

    if errors:
        raise EnvValidationError("; ".join(errors))
    return config


def _validate_queue_process_job_compatibility(errors: list[str]) -> None:
    main_source = REPO_ROOT / "app" / "main.py"
    try:
        source = main_source.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"could not read {main_source.relative_to(REPO_ROOT)}: {exc}")
        return

    required_markers = [
        'settings.worker_execution_mode == "queue"',
        "process_job_queue_wake_requested",
        "ensure_queue_worker_started(settings)",
        "run_job_background",
    ]
    missing = [marker for marker in required_markers if marker not in source]
    if missing:
        errors.append(
            "production queue compatibility missing app/main.py markers: "
            + ", ".join(missing)
        )
    queue_marker = source.find('settings.worker_execution_mode == "queue"')
    background_marker = source.find("background_tasks.add_task(")
    if queue_marker == -1 or background_marker == -1 or queue_marker > background_marker:
        errors.append(
            "production queue mode /process-job must wake the queue before any "
            "legacy background task scheduling branch"
        )


def validate_environment(environment: str, *, example: bool = False) -> dict[str, str]:
    if environment not in VALID_ENVIRONMENTS:
        raise EnvValidationError(
            "environment must be one of: " + ", ".join(sorted(VALID_ENVIRONMENTS))
        )
    path = (
        ENV_DIR / f"{environment}.example.env"
        if example
        else env_path(environment)
    )
    values = read_env_file(path)
    validate_values(environment, values, allow_placeholders=example)
    validate_config(environment)
    return values


def print_masked_summary(
    environment: str,
    values: dict[str, str],
    *,
    example: bool,
) -> None:
    suffix = ".example.env" if example else ".env"
    print(f"Validated env/{environment}{suffix}")
    for key in sorted(values):
        print(f"{key}=***")
    config = validate_config(environment)
    print(f"Validated config/worker.defaults.json + config/worker.{environment}.json")
    for key in sorted(config):
        print(f"config:{key}=***")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate worker env source files.")
    parser.add_argument("environment", choices=sorted(VALID_ENVIRONMENTS))
    parser.add_argument(
        "--example",
        action="store_true",
        help="Validate env/<environment>.example.env and allow placeholder values.",
    )
    args = parser.parse_args()

    try:
        values = validate_environment(args.environment, example=args.example)
    except EnvValidationError as exc:
        print(f"Env validation failed: {exc}", file=sys.stderr)
        return 1

    print_masked_summary(args.environment, values, example=args.example)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
