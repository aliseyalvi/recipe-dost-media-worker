from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pydantic import ValidationError
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


def settings_for_pipeline_env(environment: str, value: str) -> Settings:
    with patch.dict(
        os.environ,
        {"APP_ENV": environment, "WORKER_PIPELINE_MODE": value},
        clear=True,
    ):
        clear_worker_config_cache()
        return Settings(
            _env_file=None,
            worker_api_secret="test-only",
            supabase_url="https://example.invalid",
            supabase_service_role_key="test-only",
        )


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

    def assert_sequential_loop_started(self, settings: Settings) -> None:
        task = MagicMock()
        task.done.return_value = False
        with patch.object(
            main,
            "run_queue_loop",
            new_callable=MagicMock,
            return_value="sequential-loop",
        ) as run, patch.object(
            main.asyncio,
            "create_task",
            return_value=task,
        ) as create:
            main.ensure_queue_worker_started(settings)
        run.assert_called_once_with(main.build_job_runner)
        create.assert_called_once_with("sequential-loop")
        self.assertIs(main.queue_task, task)

    def test_default_configuration_starts_sequential_queue_loop(self) -> None:
        self.assert_sequential_loop_started(settings_for("production"))

    def test_explicit_sequential_configuration_starts_same_queue_loop(self) -> None:
        self.assert_sequential_loop_started(
            settings_for_pipeline_env("production", "sequential")
        )

    def test_retired_pipeline_values_fail_before_task_creation(self) -> None:
        for value in ("staged", "unsupported"):
            with self.subTest(value=value), patch.object(
                main.asyncio, "create_task"
            ) as create:
                with self.assertRaisesRegex(
                    ValidationError,
                    "WORKER_PIPELINE_MODE is retired; only sequential is supported",
                ):
                    settings_for_pipeline_env("production", value)
                create.assert_not_called()
                self.assertIsNone(main.queue_task)

    def test_app_main_import_does_not_load_w7_runtime_modules(self) -> None:
        code = (
            "import sys; import app.main; "
            "forbidden={'app.pipeline.runtime','app.pipeline.services',"
            "'app.pipeline.contracts','app.pipeline.transport',"
            "'app.pipeline.telemetry','app.pipeline.dispatch',"
            "'app.pipeline.ingest','app.pipeline.analyzer','app.pipeline.finalizer'}; "
            "assert forbidden.isdisjoint(sys.modules)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            env={"APP_ENV": "production", "PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_wake_reaches_sequential_queue_start(self) -> None:
        settings = settings_for("production")
        with patch.object(main, "ensure_queue_worker_started") as start:
            response = asyncio.run(main.wake(settings))
        start.assert_called_once_with(settings)
        self.assertTrue(response["ok"])

    def test_exactly_one_queue_loop_is_created(self) -> None:
        settings = settings_for("production")
        task = MagicMock()
        task.done.return_value = False
        with patch.object(
            main,
            "run_queue_loop",
            new_callable=MagicMock,
            return_value="sequential-loop",
        ) as run, patch.object(
            main.asyncio,
            "create_task",
            return_value=task,
        ) as create:
            main.ensure_queue_worker_started(settings)
            main.ensure_queue_worker_started(settings)
        run.assert_called_once_with(main.build_job_runner)
        create.assert_called_once_with("sequential-loop")

    def test_health_omits_w7_runtime_fields(self) -> None:
        payload = main.health(settings_for("production")).model_dump()
        self.assertEqual(payload["worker_processing_model"], "sequential_queue")
        forbidden = {
            "worker_pipeline_mode",
            "worker_stage_transport",
            "worker_dispatch_concurrency",
            "worker_ingest_concurrency",
            "worker_gemini_analysis_concurrency",
            "worker_finalize_concurrency",
            "worker_ffmpeg_concurrency",
            "worker_gemini_upload_concurrency",
            "worker_ingest_queue_size",
            "worker_analysis_queue_size",
            "worker_finalize_queue_size",
        }
        self.assertTrue(forbidden.isdisjoint(payload))
        self.assertTrue(forbidden.isdisjoint(payload["safe_config"]))

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
