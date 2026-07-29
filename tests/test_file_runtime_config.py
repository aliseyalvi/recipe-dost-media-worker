from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app.config import Settings, validate_external_resolver_runtime_config
from app.main import safe_config_summary
from app.runtime_config import (
    PROJECT_ROOT,
    clear_worker_config_cache,
    load_worker_config,
    worker_config_report,
)
from scripts.validate_env import ENV_ONLY_EXPORT_KEYS


def build_settings() -> Settings:
    return Settings(
        _env_file=None,
        worker_api_secret="test-only",
        supabase_url="https://example.invalid",
        supabase_service_role_key="test-only",
    )


class FileRuntimeConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_worker_config_cache()

    @contextmanager
    def isolated_environment(self, **values: str):
        environment = {"APP_ENV": values.pop("APP_ENV", "development"), **values}
        with patch.dict(os.environ, environment, clear=True):
            clear_worker_config_cache()
            yield
        clear_worker_config_cache()

    @contextmanager
    def copied_config(self, environment: str):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(
                PROJECT_ROOT / "config" / "worker.defaults.json",
                root / "worker.defaults.json",
            )
            shutil.copyfile(
                PROJECT_ROOT / "config" / f"worker.{environment}.json",
                root / f"worker.{environment}.json",
            )
            yield root

    def test_actual_development_file_supplies_resolver_configuration(self) -> None:
        with self.isolated_environment(
            APP_ENV="development",
            INSTAGRAM_RAPIDAPI_KEY="dummy-instagram",
            TIKTOK_AKNG_ACCESS_TOKEN="dummy-tiktok",
        ):
            settings = build_settings()
            validate_external_resolver_runtime_config(settings)
            report = worker_config_report("development")
        self.assertEqual(settings.media_resolver_mode, "external")
        self.assertEqual(settings.external_media_resolver_audience, "all_authorized_ai")
        self.assertEqual(
            settings.instagram_resolver_chain,
            ["rapidapi_instagram_unified_v1"],
        )
        self.assertNotIn("instagram_reels13_enabled", Settings.model_fields)
        self.assertEqual(settings.tiktok_resolver_chain, ["akng_tiktok"])
        self.assertFalse(settings.cookie_media_fallback_enabled)
        self.assertTrue(settings.youtube_direct_gemini_enabled)
        safe_instagram = safe_config_summary(settings)[
            "instagram_external_resolver"
        ]
        self.assertEqual(
            set(safe_instagram),
            {
                "adapter_identifier",
                "enabled",
                "endpoint_configured",
                "credential_present",
                "approved_host_suffix_categories",
            },
        )
        self.assertEqual(
            safe_instagram["adapter_identifier"],
            "rapidapi_instagram_unified_v1",
        )
        self.assertTrue(safe_instagram["enabled"])
        self.assertTrue(safe_instagram["endpoint_configured"])
        self.assertTrue(safe_instagram["credential_present"])
        self.assertEqual(
            safe_instagram["approved_host_suffix_categories"],
            ["meta_fbcdn", "instagram_cdn"],
        )
        self.assertNotIn(
            settings.instagram_rapidapi_host,
            json.dumps(safe_instagram),
        )
        self.assertEqual(
            report["selected_config_display_path"],
            "config/worker.development.json",
        )

    def test_render_export_keeps_secrets_not_file_configuration(self) -> None:
        self.assertIn("INSTAGRAM_RAPIDAPI_KEY", ENV_ONLY_EXPORT_KEYS)
        self.assertIn("TIKTOK_AKNG_ACCESS_TOKEN", ENV_ONLY_EXPORT_KEYS)
        self.assertNotIn("APP_ENV", ENV_ONLY_EXPORT_KEYS)
        self.assertNotIn("LOG_LEVEL", ENV_ONLY_EXPORT_KEYS)
        for key in (
            "MEDIA_RESOLVER_MODE",
            "EXTERNAL_MEDIA_RESOLVER_AUDIENCE",
            "INSTAGRAM_RESOLVER_CHAIN",
            "TIKTOK_RESOLVER_CHAIN",
            "COOKIE_MEDIA_FALLBACK_ENABLED",
            "YOUTUBE_DIRECT_GEMINI_ENABLED",
        ):
            self.assertNotIn(key, ENV_ONLY_EXPORT_KEYS)

    def test_actual_production_file_is_external_disabled_without_provider_secrets(self) -> None:
        with self.isolated_environment(APP_ENV="production"):
            settings = build_settings()
            validate_external_resolver_runtime_config(settings)
        self.assertEqual(settings.app_env, "production")
        self.assertEqual(settings.media_resolver_mode, "legacy")
        self.assertEqual(settings.external_media_resolver_audience, "disabled")
        self.assertEqual(settings.instagram_resolver_chain, [])
        self.assertEqual(settings.tiktok_resolver_chain, [])
        self.assertFalse(settings.cookie_media_fallback_enabled)
        self.assertIsNone(settings.instagram_rapidapi_key)
        self.assertIsNone(settings.tiktok_akng_access_token)

    def test_missing_environment_file_fails_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(
                PROJECT_ROOT / "config" / "worker.defaults.json",
                root / "worker.defaults.json",
            )
            with self.isolated_environment(
                APP_ENV="development",
                WORKER_CONFIG_DIR=str(root),
            ):
                with self.assertRaisesRegex(RuntimeError, "worker.development.json"):
                    load_worker_config()

    def test_missing_config_directory_fails_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "not-present"
            with self.isolated_environment(
                APP_ENV="development",
                WORKER_CONFIG_DIR=str(missing),
            ):
                with self.assertRaisesRegex(RuntimeError, "Package config/"):
                    load_worker_config()

    def test_malformed_environment_file_fails_safely(self) -> None:
        with self.copied_config("development") as root:
            (root / "worker.development.json").write_text("{", encoding="utf-8")
            with self.isolated_environment(
                APP_ENV="development",
                WORKER_CONFIG_DIR=str(root),
            ):
                with self.assertRaisesRegex(RuntimeError, "malformed"):
                    load_worker_config()

    def test_chain_key_typo_is_rejected(self) -> None:
        with self.copied_config("development") as root:
            path = root / "worker.development.json"
            values = json.loads(path.read_text(encoding="utf-8"))
            values["INSTAGRAM_RESOLVER_CHAIM"] = values.pop("INSTAGRAM_RESOLVER_CHAIN")
            path.write_text(json.dumps(values), encoding="utf-8")
            with self.isolated_environment(
                APP_ENV="development",
                WORKER_CONFIG_DIR=str(root),
            ):
                with self.assertRaisesRegex(RuntimeError, "unknown keys"):
                    load_worker_config()

    def test_file_chain_requires_array_and_normalizes_array(self) -> None:
        with self.isolated_environment(
            APP_ENV="development",
            INSTAGRAM_RAPIDAPI_KEY="dummy-instagram",
            TIKTOK_AKNG_ACCESS_TOKEN="dummy-tiktok",
        ):
            self.assertEqual(
                build_settings().instagram_resolver_chain,
                ["rapidapi_instagram_unified_v1"],
            )
        with self.copied_config("development") as root:
            path = root / "worker.development.json"
            values = json.loads(path.read_text(encoding="utf-8"))
            values["INSTAGRAM_RESOLVER_CHAIN"] = "rapidapi_instagram_unified_v1"
            path.write_text(json.dumps(values), encoding="utf-8")
            with self.isolated_environment(
                APP_ENV="development",
                WORKER_CONFIG_DIR=str(root),
            ):
                with self.assertRaisesRegex(RuntimeError, "JSON array"):
                    load_worker_config()

    def test_comma_environment_override_normalizes(self) -> None:
        with self.isolated_environment(
            APP_ENV="development",
            INSTAGRAM_RESOLVER_CHAIN="rapidapi_instagram_unified_v1",
            TIKTOK_RESOLVER_CHAIN="akng_tiktok",
        ):
            settings = build_settings()
        self.assertEqual(
            settings.instagram_resolver_chain,
            ["rapidapi_instagram_unified_v1"],
        )
        self.assertEqual(settings.tiktok_resolver_chain, ["akng_tiktok"])

    def test_blank_environment_override_preserves_file_value(self) -> None:
        with self.isolated_environment(
            APP_ENV="development",
            INSTAGRAM_RESOLVER_CHAIN="   ",
            TIKTOK_RESOLVER_CHAIN="",
        ):
            settings = build_settings()
        self.assertEqual(
            settings.instagram_resolver_chain,
            ["rapidapi_instagram_unified_v1"],
        )
        self.assertEqual(settings.tiktok_resolver_chain, ["akng_tiktok"])

    def test_unknown_adapter_fails_closed(self) -> None:
        with self.isolated_environment(
            APP_ENV="development",
            INSTAGRAM_RESOLVER_CHAIN="unknown_adapter",
        ):
            with self.assertRaisesRegex(ValidationError, "unknown adapter identifier"):
                build_settings()

    def test_active_adapter_requires_corresponding_secret(self) -> None:
        with self.isolated_environment(APP_ENV="development"):
            settings = build_settings()
            with self.assertRaisesRegex(ValueError, "Instagram.*credentials"):
                validate_external_resolver_runtime_config(settings)

    def test_branch_and_environment_mismatch_fails_closed(self) -> None:
        with self.isolated_environment(
            APP_ENV="development",
            RENDER="true",
            RENDER_GIT_BRANCH="main",
        ):
            with self.assertRaisesRegex(RuntimeError, "conflicts with RENDER_GIT_BRANCH"):
                load_worker_config()

    def test_render_branch_selects_environment_without_app_env(self) -> None:
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
            settings = build_settings()
            report = worker_config_report()
            validate_external_resolver_runtime_config(settings)
        self.assertEqual(settings.app_env, "development")
        self.assertEqual(report["environment_selection_source"], "RENDER_GIT_BRANCH")
        self.assertEqual(
            settings.instagram_resolver_chain,
            ["rapidapi_instagram_unified_v1"],
        )

        with patch.dict(
            os.environ,
            {"RENDER": "true", "RENDER_GIT_BRANCH": "main"},
            clear=True,
        ):
            clear_worker_config_cache()
            production = build_settings()
            production_report = worker_config_report()
            validate_external_resolver_runtime_config(production)
        self.assertEqual(production.app_env, "production")
        self.assertEqual(
            production_report["environment_selection_source"],
            "RENDER_GIT_BRANCH",
        )
        self.assertEqual(production.external_media_resolver_audience, "disabled")

    def test_retired_reels13_configuration_is_rejected_and_flag_is_ignored(
        self,
    ) -> None:
        with self.isolated_environment(
            APP_ENV="development",
            INSTAGRAM_RESOLVER_CHAIN="rapidapi_instagram_reels13",
            INSTAGRAM_REELS13_ENABLED="true",
            INSTAGRAM_RAPIDAPI_KEY="dummy-instagram",
        ):
            with self.assertRaisesRegex(
                ValidationError,
                "rapidapi_instagram_reels13 is retired and cannot be enabled",
            ):
                build_settings()

        with self.isolated_environment(
            APP_ENV="development",
            INSTAGRAM_REELS13_ENABLED="true",
            INSTAGRAM_RAPIDAPI_KEY="dummy-instagram",
        ):
            settings = build_settings()

        self.assertEqual(
            settings.instagram_resolver_chain,
            ["rapidapi_instagram_unified_v1"],
        )
        self.assertNotIn("instagram_reels13_enabled", Settings.model_fields)
        defaults = json.loads(
            (PROJECT_ROOT / "config" / "worker.defaults.json").read_text(
                encoding="utf-8"
            )
        )
        development = json.loads(
            (PROJECT_ROOT / "config" / "worker.development.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertNotIn("INSTAGRAM_REELS13_ENABLED", defaults)
        self.assertNotIn("INSTAGRAM_REELS13_ENABLED", development)
        self.assertNotIn(
            "rapidapi_instagram_reels13",
            defaults["INSTAGRAM_RESOLVER_CHAIN"],
        )


if __name__ == "__main__":
    unittest.main()
