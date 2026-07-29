from __future__ import annotations

import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app import main
from app.config import Settings
from app.runtime_config import clear_worker_config_cache


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_RUNTIME = {
    "google_genai": "2.13.0",
    "pydantic": "2.13.4",
    "pydantic_core": "2.46.4",
}


def settings() -> Settings:
    with patch.dict(os.environ, {"APP_ENV": "production"}, clear=True):
        clear_worker_config_cache()
        return Settings(
            _env_file=None,
            worker_api_secret="worker-secret-must-not-appear",
            supabase_url="https://example.invalid",
            supabase_service_role_key="service-secret-must-not-appear",
            gemini_api_key="gemini-secret-must-not-appear",
        )


class RuntimeDependencyTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_worker_config_cache()
        main.queue_task = None

    def test_requirements_use_exact_verified_runtime_pins(self) -> None:
        lines = {
            line.strip()
            for line in REPOSITORY_ROOT.joinpath("requirements.txt")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn("google-genai==2.13.0", lines)
        self.assertIn("pydantic==2.13.4", lines)
        self.assertIn("pydantic-core==2.46.4", lines)
        self.assertFalse(
            any(
                line.startswith(("google-genai>", "google-genai<", "pydantic>", "pydantic<"))
                for line in lines
            )
        )

    def test_installed_runtime_and_safe_health_match_verified_versions(self) -> None:
        versions = main.runtime_dependency_versions()
        for name, expected in EXPECTED_RUNTIME.items():
            self.assertEqual(versions[name], expected)
        self.assertRegex(versions["pydantic_settings"], r"^\d+\.\d+\.\d+")
        self.assertRegex(versions["python"], r"^3\.12\.\d+$")

        payload = main.health(settings()).model_dump(mode="json")
        self.assertEqual(payload["safe_config"]["runtime_dependencies"], versions)
        serialized = json.dumps(payload)
        for forbidden in (
            "worker-secret-must-not-appear",
            "service-secret-must-not-appear",
            "gemini-secret-must-not-appear",
            "fingerprint",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_startup_fingerprint_is_once_safe_and_nonblocking(self) -> None:
        active = settings()
        with patch.object(main, "get_settings", return_value=active), patch.object(
            main, "validate_external_resolver_runtime_config"
        ), patch.object(
            main, "cleanup_stale_media", return_value=0
        ), patch.object(
            main, "ensure_queue_worker_started"
        ) as start, patch.object(
            main.logger, "info"
        ) as info, patch.object(
            main, "report_worker_error"
        ):
            asyncio.run(main.start_queue_worker())

        start.assert_called_once_with(active)
        fingerprint_calls = [
            call
            for call in info.call_args_list
            if call.args == ("runtime_dependency_versions",)
        ]
        self.assertEqual(len(fingerprint_calls), 1)
        self.assertEqual(
            set(fingerprint_calls[0].kwargs["extra"]),
            {
                "google_genai",
                "pydantic",
                "pydantic_core",
                "pydantic_settings",
                "python",
                "worker_processing_model",
            },
        )

        with patch.object(main.logger, "info", side_effect=RuntimeError("telemetry")):
            main.log_runtime_dependency_versions()


if __name__ == "__main__":
    unittest.main()
