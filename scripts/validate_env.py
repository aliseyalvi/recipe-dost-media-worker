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
]
ENV_ONLY_EXPORT_KEYS = sorted(set(ENV_ONLY_REQUIRED_KEYS + ENV_ONLY_OPTIONAL_KEYS))
CONFIG_ONLY_KEYS = [
    "AI_ANALYSIS_MODE",
    "ALLOW_PRIVATE_METADATA_FAILURE",
    "ALLOW_RAW_URL_LOGS",
    "ENABLE_INSTAGRAM_COOKIES",
    "ENABLE_METADATA_ONLY_MOCK_FALLBACK",
    "ENABLE_QUEUE_WAKE_ENDPOINT",
    "ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR",
    "ENABLE_YTDLP_COOKIES",
    "GEMINI_FILE_MAX_WAIT_SECONDS",
    "GEMINI_FILE_POLL_INTERVAL_SECONDS",
    "GEMINI_FILE_UPLOAD_TIMEOUT_SECONDS",
    "GEMINI_JSON_FALLBACK_ENABLED",
    "GEMINI_JSON_RETRY_ENABLED",
    "GEMINI_SAVE_MODE",
    "GEMINI_TEXT_MODEL",
    "GEMINI_VIDEO_ANALYSIS_MODE",
    "GEMINI_VIDEO_INPUT_MODE",
    "GEMINI_VIDEO_MODEL",
    "MAX_VIDEO_DURATION_SECONDS",
    "MAX_VIDEO_SIZE_MB",
    "MEDIA_EXTRACT_OUTPUT_MODE",
    "MOCK_ANALYSIS_DELAY_MS",
    "MOCK_RECIPE_FIXTURE_PATH",
    "MOCK_VIDEO_PROCESSING_MODE",
    "QUEUE_EMPTY_SLEEP_SECONDS",
    "QUEUE_POLL_INTERVAL_SECONDS",
    "REAL_AI_FALLBACK_TO_MOCK",
    "RECIPE_CARD_NORMALIZATION_ENABLED",
    "TEMP_MEDIA_DIR",
    "THUMBNAIL_BUCKET",
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
            for key in ENV_ONLY_REQUIRED_KEYS + ["MEDIA_WORKER_SHARED_SECRET", "WORKER_API_SECRET"]
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
        max_duration = int(config.get("MAX_VIDEO_DURATION_SECONDS", 0))
    except (TypeError, ValueError):
        errors.append("config MAX_VIDEO_DURATION_SECONDS must be an integer")
    else:
        if max_duration <= 0:
            errors.append("config MAX_VIDEO_DURATION_SECONDS must be greater than 0")
        if max_duration > 120:
            errors.append("config MAX_VIDEO_DURATION_SECONDS must be <= 120")

    if _config_bool(config.get("ALLOW_RAW_URL_LOGS", False)):
        errors.append("config ALLOW_RAW_URL_LOGS must be false")

    if environment == "production":
        if str(config.get("AI_ANALYSIS_MODE", "")).strip().lower() == "mock_fixture":
            errors.append("production config must not default to mock AI mode")
        if _config_bool(config.get("REAL_AI_FALLBACK_TO_MOCK", False)):
            errors.append("production config must not enable real AI fallback to mock")

    if errors:
        raise EnvValidationError("; ".join(errors))
    return config


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
