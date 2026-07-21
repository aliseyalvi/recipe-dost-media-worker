from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.runtime_config import load_worker_config, worker_config_report

VALID_ENVIRONMENTS = {"development", "production"}
EXPECTED_BRANCHES = {
    "development": "develop",
    "production": "main",
}


class EnvironmentCheckError(Exception):
    pass


def _git_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise EnvironmentCheckError(f"could not read git branch: {exc}") from exc
    return result.stdout.strip() or "unknown"


def _strip_optional_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _read_safe_env_fields(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    safe_fields: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        normalized_key = key.strip().upper()
        if normalized_key in {"APP_ENV", "SUPABASE_URL"}:
            safe_fields[normalized_key] = _strip_optional_quotes(value.strip())
    return safe_fields


def _host_only(url: str | None) -> str:
    if not url:
        return "missing"
    parsed = urlparse(url)
    return parsed.netloc or "invalid"


def _effective_value(config: dict[str, Any], key: str) -> Any:
    environment_value = os.environ.get(key)
    if environment_value is None or not environment_value.strip():
        return config.get(key)
    return environment_value


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


def _runtime_checks(
    config: dict[str, Any],
    environment: str,
) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    checks: dict[str, str] = {}

    worker_mode = str(_effective_value(config, "WORKER_EXECUTION_MODE") or "").strip()
    checks["worker_execution_mode"] = worker_mode or "missing"
    if worker_mode.lower() != "queue":
        errors.append("worker_execution_mode must be queue")

    unified_enabled = _bool_value(
        _effective_value(config, "ENABLE_UNIFIED_YTDLP_STREAM_EXTRACTOR")
    )
    checks["enable_unified_ytdlp_stream_extractor"] = str(unified_enabled).lower()
    if not unified_enabled:
        errors.append("enable_unified_ytdlp_stream_extractor must be true")

    raw_url_logging = _bool_value(_effective_value(config, "ALLOW_RAW_URL_LOGS"))
    checks["raw_url_logging_enabled"] = str(raw_url_logging).lower()
    if raw_url_logging:
        errors.append("raw_url_logging_enabled must be false")

    try:
        max_duration = int(_effective_value(config, "MAX_VIDEO_DURATION_SECONDS") or 0)
    except (TypeError, ValueError):
        checks["max_video_duration_seconds"] = "invalid"
        errors.append("max_video_duration_seconds must be an integer")
    else:
        checks["max_video_duration_seconds"] = str(max_duration)
        if max_duration <= 0:
            errors.append("max_video_duration_seconds must be greater than 0")
        if max_duration > 120:
            errors.append("max_video_duration_seconds must be <= 120")
        if environment == "production" and max_duration == 90:
            errors.append("production max_video_duration_seconds still matches fallback 90")

    try:
        preferred_size = int(_effective_value(config, "PREFERRED_VIDEO_SIZE_MB") or 0)
        hard_size = int(_effective_value(config, "MAX_VIDEO_SIZE_MB") or 0)
        adaptive_attempts = int(
            _effective_value(config, "MEDIA_ADAPTIVE_MAX_ATTEMPTS") or 0
        )
        min_height = int(_effective_value(config, "MIN_VIDEO_HEIGHT") or 0)
        max_height = int(_effective_value(config, "MAX_VIDEO_HEIGHT") or 0)
    except (TypeError, ValueError):
        checks["media_size_policy"] = "invalid"
        errors.append("media size policy values must be integers")
    else:
        checks["preferred_video_size_mib"] = str(preferred_size)
        checks["hard_video_size_mib"] = str(hard_size)
        checks["media_adaptive_max_attempts"] = str(adaptive_attempts)
        checks["video_height_range"] = f"{min_height}-{max_height}"
        if not 0 < preferred_size <= hard_size:
            errors.append("media size limits must satisfy 0 < preferred <= hard")
        if not 1 <= adaptive_attempts <= 4:
            errors.append("media_adaptive_max_attempts must be between 1 and 4")
        if not 360 <= min_height <= max_height <= 720:
            errors.append("video height limits must satisfy 360 <= min <= max <= 720")

    return checks, errors


def check_environment(environment: str) -> dict[str, object]:
    if environment not in VALID_ENVIRONMENTS:
        raise EnvironmentCheckError(
            "environment must be one of: " + ", ".join(sorted(VALID_ENVIRONMENTS))
        )

    branch = _git_branch()
    expected_branch = EXPECTED_BRANCHES[environment]
    env_path = REPO_ROOT / "env" / f"{environment}.env"
    env_fields = _read_safe_env_fields(env_path)
    config = load_worker_config(environment)
    config_report = worker_config_report(environment)
    runtime_checks, runtime_errors = _runtime_checks(config, environment)

    errors = list(runtime_errors)
    warnings: list[str] = []

    if environment == "production":
        override = os.environ.get("ALLOW_NON_MAIN_PROD_DEPLOY") == "1"
        if branch != expected_branch and not override:
            errors.append(
                "production deployment requires branch main "
                "unless ALLOW_NON_MAIN_PROD_DEPLOY=1"
            )
    elif branch != expected_branch:
        warnings.append("development deployments should normally run from develop")

    app_env_actual = env_fields.get(
        "APP_ENV",
        "missing" if env_path.exists() else "env_file_missing",
    )
    if env_path.exists() and app_env_actual != environment:
        errors.append(f"env APP_ENV must be {environment}")

    return {
        "branch": branch,
        "requested_environment": environment,
        "expected_branch": expected_branch,
        "env_file_exists": env_path.exists(),
        "app_env_expected": environment,
        "app_env_in_env_file": app_env_actual,
        "supabase_url_host": (
            _host_only(env_fields.get("SUPABASE_URL"))
            if env_path.exists()
            else "env_file_missing"
        ),
        "config_files_loaded": config_report.get("loaded_files", []),
        "config_files_missing": config_report.get("missing_files", []),
        "runtime_checks": runtime_checks,
        "warnings": warnings,
        "errors": errors,
    }


def print_summary(summary: dict[str, object]) -> None:
    print("Environment check summary")
    print(f"current_git_branch={summary['branch']}")
    print(f"requested_environment={summary['requested_environment']}")
    print(f"expected_branch={summary['expected_branch']}")
    print(f"env_file_exists={str(summary['env_file_exists']).lower()}")
    print(f"APP_ENV_expected={summary['app_env_expected']}")
    print(f"APP_ENV_in_env_file={summary['app_env_in_env_file']}")
    print(f"supabase_url_host={summary['supabase_url_host']}")
    print(f"config_files_loaded={summary['config_files_loaded']}")
    print(f"config_files_missing={summary['config_files_missing']}")
    print("runtime_config_key_checks:")
    runtime_checks = summary["runtime_checks"]
    if isinstance(runtime_checks, dict):
        for key in (
            "worker_execution_mode",
            "enable_unified_ytdlp_stream_extractor",
            "max_video_duration_seconds",
            "preferred_video_size_mib",
            "hard_video_size_mib",
            "media_adaptive_max_attempts",
            "raw_url_logging_enabled",
        ):
            print(f"  {key}={runtime_checks.get(key)}")
    warnings = summary.get("warnings", [])
    if isinstance(warnings, list):
        for warning in warnings:
            print(f"warning={warning}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe worker environment release check.")
    parser.add_argument("environment", choices=sorted(VALID_ENVIRONMENTS))
    args = parser.parse_args()

    try:
        summary = check_environment(args.environment)
    except (EnvironmentCheckError, RuntimeError, ValueError) as exc:
        print(f"Environment check failed: {exc}", file=sys.stderr)
        return 1

    print_summary(summary)
    errors = summary.get("errors", [])
    if isinstance(errors, list) and errors:
        sys.stdout.flush()
        for error in errors:
            print(f"error={error}", file=sys.stderr)
        return 1

    print("Environment check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
