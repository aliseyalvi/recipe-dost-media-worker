from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"


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
    path = REPO_ROOT / ".env"
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


@lru_cache
def load_worker_config(app_env: str | None = None) -> dict[str, Any]:
    environment = _normalized_environment(app_env or os.getenv("APP_ENV") or _dotenv_app_env())
    merged = _read_json(CONFIG_DIR / "worker.defaults.json")
    merged.update(_read_json(CONFIG_DIR / f"worker.{environment}.json"))
    return merged


def worker_config_value(field_name: str, default: Any) -> Any:
    key = field_name.upper()
    return load_worker_config().get(key, default)
