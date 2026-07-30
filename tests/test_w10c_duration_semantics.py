from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from app.config import Settings
from app.main import safe_config_summary
from app.metadata_preview import preview_from_mapping
from app.runtime_config import PROJECT_ROOT, clear_worker_config_cache
from app.social.stream_download import try_ytdlp_extract
from app.supabase_client import derive_recipe_total_minutes


def build_settings(**values: object) -> Settings:
    return Settings(
        _env_file=None,
        worker_api_secret="test-only",
        supabase_url="https://example.invalid",
        supabase_service_role_key="test-only",
        **values,
    )


class _FakeYoutubeDL:
    duration_seconds = 180

    def __init__(self, _options: dict[str, object]) -> None:
        pass

    def __enter__(self) -> "_FakeYoutubeDL":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, _url: str, *, download: bool) -> dict[str, object]:
        assert download is False
        return {
            "id": "safe-id",
            "duration": self.duration_seconds,
            "formats": [],
        }


class W10CDurationSemanticsTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_worker_config_cache()

    def test_default_and_json_inheritance_are_180_seconds(self) -> None:
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
        production = json.loads(
            (PROJECT_ROOT / "config" / "worker.production.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(defaults["MAX_SOURCE_VIDEO_DURATION_SECONDS"], 180)
        self.assertNotIn("MAX_SOURCE_VIDEO_DURATION_SECONDS", development)
        self.assertNotIn("MAX_SOURCE_VIDEO_DURATION_SECONDS", production)
        with patch.dict(os.environ, {"APP_ENV": "development"}, clear=True):
            clear_worker_config_cache()
            development_settings = build_settings()
        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=True):
            clear_worker_config_cache()
            production_settings = build_settings()
        self.assertEqual(
            development_settings.max_source_video_duration_seconds,
            180,
        )
        self.assertEqual(
            production_settings.max_source_video_duration_seconds,
            180,
        )

    def test_optional_override_accepts_only_60_through_900(self) -> None:
        for value in ("60", "900"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {
                    "APP_ENV": "development",
                    "MAX_SOURCE_VIDEO_DURATION_SECONDS": value,
                },
                clear=True,
            ):
                clear_worker_config_cache()
                self.assertEqual(
                    build_settings().max_source_video_duration_seconds,
                    int(value),
                )
        for value in ("59", "901"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {
                    "APP_ENV": "development",
                    "MAX_SOURCE_VIDEO_DURATION_SECONDS": value,
                },
                clear=True,
            ):
                clear_worker_config_cache()
                with self.assertRaises(ValidationError):
                    build_settings()

    def test_health_exposes_the_effective_safe_source_limit(self) -> None:
        settings = build_settings(max_source_video_duration_seconds=240)
        safe = safe_config_summary(settings)
        self.assertEqual(safe["max_source_video_duration_seconds"], 240)
        self.assertEqual(safe["max_video_duration_seconds"], 240)

    def test_180_is_accepted_and_181_has_bounded_error_metadata(self) -> None:
        settings = build_settings(max_source_video_duration_seconds=180)
        with patch("yt_dlp.YoutubeDL", _FakeYoutubeDL), patch(
            "app.social.stream_download.extract_to_temp_file",
            return_value={"success": True},
        ):
            _FakeYoutubeDL.duration_seconds = 180
            accepted = try_ytdlp_extract(
                "https://source.invalid/video",
                "instagram",
                "job",
                settings,
                {},
                cookie_mode="none",
                resolver="ytdlp_stream",
                output_mode="temp_file",
            )
            _FakeYoutubeDL.duration_seconds = 181
            rejected = try_ytdlp_extract(
                "https://source.invalid/video",
                "instagram",
                "job",
                settings,
                {},
                cookie_mode="none",
                resolver="ytdlp_stream",
                output_mode="temp_file",
            )
        self.assertTrue(accepted["success"])
        self.assertEqual(rejected["error_code"], "video_duration_exceeded")
        self.assertEqual(rejected["actual_duration_seconds"], 181)
        self.assertEqual(rejected["max_duration_seconds"], 180)
        self.assertEqual(
            rejected["duration_validation_source"],
            "validated_resolver_metadata",
        )
        self.assertFalse(rejected["retryable"])

    def test_preview_uses_the_same_limit_and_marks_trusted_source(self) -> None:
        settings = build_settings(max_source_video_duration_seconds=180)
        at_limit = preview_from_mapping(
            platform="instagram",
            resolver="instagram_ytdlp",
            source={"duration_seconds": 180},
            settings=settings,
        )
        over_limit = preview_from_mapping(
            platform="instagram",
            resolver="instagram_ytdlp",
            source={"duration_seconds": 181},
            settings=settings,
        )
        self.assertFalse(at_limit.is_too_long)
        self.assertTrue(over_limit.is_too_long)
        self.assertEqual(
            at_limit.duration_validation_source,
            "validated_resolver_metadata",
        )

    def test_recipe_total_preserves_explicit_or_requires_prep_and_cook(self) -> None:
        self.assertEqual(derive_recipe_total_minutes(10, 20, 45), 45)
        self.assertEqual(derive_recipe_total_minutes(10, 20), 30)
        self.assertIsNone(derive_recipe_total_minutes(10, None))
        self.assertIsNone(derive_recipe_total_minutes(None, 20))
        self.assertIsNone(derive_recipe_total_minutes(None, None))
        self.assertEqual(derive_recipe_total_minutes(10, 20, 0), 30)

    def test_backend_source_policy_has_no_two_minute_copy_or_legacy_json_key(self) -> None:
        source_files = [
            PROJECT_ROOT / "app" / "config.py",
            PROJECT_ROOT / "app" / "job_runner.py",
            PROJECT_ROOT / "app" / "metadata_preview.py",
            PROJECT_ROOT / "app" / "social" / "stream_download.py",
            PROJECT_ROOT / "config" / "worker.defaults.json",
            PROJECT_ROOT / "config" / "worker.production.json",
        ]
        combined = "\n".join(path.read_text(encoding="utf-8") for path in source_files)
        self.assertNotIn("2 minutes", combined)
        self.assertNotIn("90 second limit", combined)
        defaults = json.loads(source_files[-2].read_text(encoding="utf-8"))
        self.assertNotIn("MAX_VIDEO_DURATION_SECONDS", defaults)


if __name__ == "__main__":
    unittest.main()
