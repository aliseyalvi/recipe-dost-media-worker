from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VALID_WORKER_ENVIRONMENTS = {"development", "production"}
RENDER_BRANCH_ENVIRONMENTS = {
    "develop": "development",
    "main": "production",
}
CANONICAL_LIST_KEYS = {
    "INSTAGRAM_RESOLVER_CHAIN",
    "TIKTOK_RESOLVER_CHAIN",
    "INSTAGRAM_MEDIA_HOST_SUFFIXES",
    "TIKTOK_MEDIA_HOST_SUFFIXES",
}
CANONICAL_BOOLEAN_KEYS = {
    "COOKIE_MEDIA_FALLBACK_ENABLED",
    "YOUTUBE_DIRECT_GEMINI_ENABLED",
}


def _config_dir() -> Path:
    configured = os.getenv("WORKER_CONFIG_DIR")
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path
    return PROJECT_ROOT / "config"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(
            f"Worker config file is missing: {_display_path(path)}. "
            "Package config/ into the runtime image."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Worker config file is malformed: {_display_path(path)} "
            f"(line {exc.lineno})."
        ) from exc
    if not isinstance(data, dict):
        raise RuntimeError(
            f"Worker config file must contain a JSON object: {_display_path(path)}."
        )
    if not data:
        raise RuntimeError(f"Worker config file is empty: {_display_path(path)}.")
    normalized = {str(key).upper(): value for key, value in data.items()}
    _validate_canonical_file_types(path, normalized)
    return normalized


def _normalized_environment(value: str, *, source: str) -> str:
    environment = value.strip().lower()
    if environment == "prod":
        return "production"
    if environment not in VALID_WORKER_ENVIRONMENTS:
        raise RuntimeError(
            f"Invalid worker environment from {source}; expected development or production."
        )
    return environment


def _nonblank_environment_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _dotenv_app_env() -> str | None:
    path = PROJECT_ROOT / ".env"
    if not path.exists():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, value = line.partition("=")
        if separator and key.strip().upper() == "APP_ENV":
            return value.strip().strip("'\"")
    return None


def _config_file_names(environment: str) -> tuple[str, str]:
    return "worker.defaults.json", f"worker.{environment}.json"


def selected_worker_environment(requested: str | None = None) -> tuple[str, str]:
    """Resolve the file environment and fail on conflicting Render identity."""
    explicit = (
        _normalized_environment(requested, source="requested environment")
        if requested is not None
        else None
    )
    app_env_value = _nonblank_environment_value("APP_ENV")
    app_environment = (
        _normalized_environment(app_env_value, source="APP_ENV")
        if app_env_value is not None
        else None
    )
    branch = _nonblank_environment_value("RENDER_GIT_BRANCH")
    branch_environment = RENDER_BRANCH_ENVIRONMENTS.get((branch or "").lower())
    on_render = (_nonblank_environment_value("RENDER") or "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if branch and branch_environment is None:
        raise RuntimeError(
            "Ambiguous Render branch for worker config selection; expected develop or main."
        )
    if on_render and branch_environment is None:
        raise RuntimeError(
            "Render worker config selection requires RENDER_GIT_BRANCH=develop or main."
        )

    selected = explicit or app_environment or branch_environment
    source = (
        "APP_ENV"
        if app_environment is not None and (explicit is None or explicit == app_environment)
        else "RENDER_GIT_BRANCH"
        if branch_environment is not None and (explicit is None or explicit == branch_environment)
        else "requested_environment"
        if explicit is not None
        else "dotenv_APP_ENV"
    )
    if selected is None:
        dotenv_value = _dotenv_app_env()
        if dotenv_value and dotenv_value.strip():
            selected = _normalized_environment(dotenv_value, source=".env APP_ENV")
        else:
            selected = "development"
            source = "local_development_default"

    if app_environment is not None and explicit is not None and app_environment != explicit:
        raise RuntimeError(
            "Requested worker environment conflicts with APP_ENV."
        )
    if branch_environment is not None and selected != branch_environment:
        raise RuntimeError(
            "Worker environment conflicts with RENDER_GIT_BRANCH; "
            f"{branch} requires {branch_environment}."
        )
    return selected, source


def _load_worker_config_with_report(
    app_env: str | None = None,
) -> tuple[dict[str, Any], dict[str, object]]:
    environment, selection_source = selected_worker_environment(app_env)
    config_dir = _config_dir()
    defaults_name, environment_name = _config_file_names(environment)
    defaults_path = config_dir / defaults_name
    environment_path = config_dir / environment_name

    defaults = _read_json(defaults_path)
    environment_config = _read_json(environment_path)
    unknown_environment_keys = sorted(set(environment_config) - set(defaults))
    if unknown_environment_keys:
        raise RuntimeError(
            f"Worker config file {_display_path(environment_path)} contains unknown keys: "
            + ", ".join(unknown_environment_keys)
        )
    merged = {**defaults, **environment_config}
    env_override_keys = sorted(
        key
        for key in merged
        if _nonblank_environment_value(key) is not None
    )
    field_sources = {
        key: (
            "environment_override"
            if key in env_override_keys
            else f"config/{environment_name}"
            if key in environment_config
            else "config/worker.defaults.json"
        )
        for key in merged
    }
    report: dict[str, object] = {
        "app_env": environment,
        "environment_selection_source": selection_source,
        "config_dir": str(config_dir),
        "config_dir_exists": config_dir.exists(),
        "selected_config_file": str(environment_path),
        "selected_config_display_path": _display_path(environment_path),
        "selected_config_exists": environment_path.exists(),
        "selected_config_loaded": True,
        "loaded_files": [defaults_name, environment_name],
        "missing_files": [],
        "defaults_config_loaded": True,
        "production_config_loaded": environment == "production",
        "env_override_keys_count": len(env_override_keys),
        "field_sources": field_sources,
    }
    return merged, report


@lru_cache
def load_worker_config(app_env: str | None = None) -> dict[str, Any]:
    merged, _report = _load_worker_config_with_report(app_env)
    return merged


@lru_cache
def worker_config_report(app_env: str | None = None) -> dict[str, object]:
    _merged, report = _load_worker_config_with_report(app_env)
    return report


def worker_config_value(field_name: str, default: Any) -> Any:
    key = field_name.upper()
    return load_worker_config().get(key, default)


def worker_settings_file_source() -> dict[str, Any]:
    """Return JSON config using Settings field names for Pydantic."""
    environment, _source = selected_worker_environment()
    values = {key.lower(): value for key, value in load_worker_config(environment).items()}
    values["app_env"] = environment
    return values


def clear_worker_config_cache() -> None:
    load_worker_config.cache_clear()
    worker_config_report.cache_clear()


def _validate_canonical_file_types(path: Path, values: dict[str, Any]) -> None:
    for key in CANONICAL_LIST_KEYS:
        if key not in values:
            continue
        value = values[key]
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise RuntimeError(
                f"Worker config field {key} in {_display_path(path)} "
                "must be a JSON array of non-empty strings."
            )
    for key in CANONICAL_BOOLEAN_KEYS:
        if key in values and not isinstance(values[key], bool):
            raise RuntimeError(
                f"Worker config field {key} in {_display_path(path)} must be a JSON boolean."
            )


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT.resolve()))
    except ValueError:
        return str(path)
