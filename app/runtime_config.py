from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _config_dir() -> Path:
    configured = os.getenv("WORKER_CONFIG_DIR")
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path
    return PROJECT_ROOT / "config"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return {str(key).upper(): value for key, value in data.items()}


def _normalized_environment(value: str | None) -> str:
    environment = (value or "development").strip().lower()
    if environment == "prod":
        return "production"
    if environment not in {"development", "production"}:
        return "development"
    return environment


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


def _load_worker_config_with_report(
    app_env: str | None = None,
) -> tuple[dict[str, Any], dict[str, object]]:
    environment = _normalized_environment(
        app_env or os.getenv("APP_ENV") or _dotenv_app_env()
    )
    config_dir = _config_dir()
    defaults_name, environment_name = _config_file_names(environment)
    defaults_path = config_dir / defaults_name
    environment_path = config_dir / environment_name

    loaded_files: list[str] = []
    missing_files: list[str] = []
    merged: dict[str, Any] = {}

    defaults = _read_json(defaults_path)
    if defaults:
        loaded_files.append(defaults_name)
        merged.update(defaults)
    else:
        missing_files.append(defaults_name)

    environment_config = _read_json(environment_path)
    if environment_config:
        loaded_files.append(environment_name)
        merged.update(environment_config)
    else:
        missing_files.append(environment_name)

    if environment == "production" and environment_name not in loaded_files:
        raise RuntimeError(
            "Production worker config is missing or empty: "
            f"{environment_name}. Package config/ into the runtime image."
        )

    env_override_keys_count = sum(1 for key in merged if key in os.environ)
    report: dict[str, object] = {
        "app_env": environment,
        "config_dir": str(config_dir),
        "config_dir_exists": config_dir.exists(),
        "loaded_files": loaded_files,
        "missing_files": missing_files,
        "defaults_config_loaded": defaults_name in loaded_files,
        "production_config_loaded": "worker.production.json" in loaded_files,
        "env_override_keys_count": env_override_keys_count,
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
