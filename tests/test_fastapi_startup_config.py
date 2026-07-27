from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from app import main
from app.config import Settings
from app.runtime_config import clear_worker_config_cache


def settings_for(environment: str, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "worker_api_secret": "test-only",
        "supabase_url": "https://example.invalid",
        "supabase_service_role_key": "test-only",
    }
    values.update(overrides)
    with patch.dict(os.environ, {"APP_ENV": environment}, clear=True):
        clear_worker_config_cache()
        return Settings(_env_file=None, **values)


class FastApiStartupConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_worker_config_cache()
        main.queue_task = None

    def run_startup(self, settings: Settings) -> None:
        with patch.object(main, "get_settings", return_value=settings), patch.object(
            main, "ensure_queue_worker_started"
        ) as start, patch.object(
            main, "cleanup_stale_media", return_value=0
        ), patch.object(main, "report_worker_error"):
            asyncio.run(main.start_queue_worker())
        start.assert_called_once_with(settings)

    def test_development_file_and_dummy_secrets_start(self) -> None:
        with patch.dict(
            os.environ,
            {
                "RENDER": "true",
                "RENDER_GIT_BRANCH": "develop",
                "INSTAGRAM_RAPIDAPI_KEY": "dummy-instagram",
                "TIKTOK_AKNG_ACCESS_TOKEN": "dummy-tiktok",
            },
            clear=True,
        ):
            clear_worker_config_cache()
            settings = Settings(
                _env_file=None,
                worker_api_secret="test-only",
                supabase_url="https://example.invalid",
                supabase_service_role_key="test-only",
            )
            self.run_startup(settings)

    def test_development_missing_chains_has_actionable_failure(self) -> None:
        settings = settings_for(
            "development",
            instagram_resolver_chain=[],
            tiktok_resolver_chain=[],
            instagram_rapidapi_key="dummy-instagram",
            tiktok_akng_access_token="dummy-tiktok",
        )
        with patch.object(main, "get_settings", return_value=settings), patch.object(
            main, "report_worker_error"
        ):
            with self.assertRaisesRegex(
                ValueError,
                "worker.development.json.*INSTAGRAM_RESOLVER_CHAIN",
            ):
                asyncio.run(main.start_queue_worker())

    def test_production_external_disabled_starts_without_provider_secrets(self) -> None:
        with patch.dict(
            os.environ,
            {"RENDER": "true", "RENDER_GIT_BRANCH": "main"},
            clear=True,
        ):
            clear_worker_config_cache()
            settings = Settings(
                _env_file=None,
                worker_api_secret="test-only",
                supabase_url="https://example.invalid",
                supabase_service_role_key="test-only",
            )
            self.run_startup(settings)

    def test_wrong_file_for_render_branch_fails_before_start(self) -> None:
        with patch.dict(
            os.environ,
            {"APP_ENV": "development", "RENDER": "true", "RENDER_GIT_BRANCH": "main"},
            clear=True,
        ):
            clear_worker_config_cache()
            with self.assertRaisesRegex(RuntimeError, "conflicts with RENDER_GIT_BRANCH"):
                Settings(
                    _env_file=None,
                    worker_api_secret="test-only",
                    supabase_url="https://example.invalid",
                    supabase_service_role_key="test-only",
                )

    def test_missing_config_directory_fails_before_start(self) -> None:
        with patch.dict(
            os.environ,
            {"APP_ENV": "development", "WORKER_CONFIG_DIR": "/missing/config/path"},
            clear=True,
        ):
            clear_worker_config_cache()
            with self.assertRaisesRegex(RuntimeError, "Package config/"):
                Settings(
                    _env_file=None,
                    worker_api_secret="test-only",
                    supabase_url="https://example.invalid",
                    supabase_service_role_key="test-only",
                )


if __name__ == "__main__":
    unittest.main()
