from __future__ import annotations

import argparse
import sys
from pathlib import Path

from validate_env import (
    ENV_ONLY_EXPORT_KEYS,
    EnvValidationError,
    VALID_ENVIRONMENTS,
    validate_environment,
)

RENDER_SECRET_KEY_MAP = {
    "MEDIA_WORKER_SHARED_SECRET": "WORKER_API_SECRET",
}


def render_values(source_values: dict[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in source_values.items():
        if key not in ENV_ONLY_EXPORT_KEYS:
            continue
        if key in RENDER_SECRET_KEY_MAP:
            values[RENDER_SECRET_KEY_MAP[key]] = value
            continue
        values[key] = value
    return values


def write_render_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={values[key]}" for key in sorted(values)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a validated Render env file.")
    parser.add_argument("environment", choices=sorted(VALID_ENVIRONMENTS))
    parser.add_argument("--output", required=True, help="Output env file path")
    args = parser.parse_args()

    try:
        source_values = validate_environment(args.environment)
    except EnvValidationError as exc:
        print(f"Env validation failed: {exc}", file=sys.stderr)
        return 1

    output_path = Path(args.output).expanduser().resolve()
    values = render_values(source_values)
    write_render_env(output_path, values)

    print(f"Wrote Render env file: {output_path}")
    print("Keys:")
    for key in sorted(values):
        print(f"- {key}")
    print("WARNING: output file contains secrets. Delete it after uploading to Render.")
    print(
        "WARNING: old Render env groups may still contain deprecated non-secret config "
        "vars. Remove them only after this config-based worker is deployed and tested."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
