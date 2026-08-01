from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import check_instagram_media_candidate as diagnostic


class InstagramLiveDiagnosticTests(unittest.TestCase):
    def test_guard_failure_never_prompts_or_calls_provider(self) -> None:
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), patch.object(
            diagnostic.getpass, "getpass"
        ) as prompt, patch.object(
            diagnostic, "resolve_external_media"
        ) as resolve, redirect_stdout(output):
            exit_code = diagnostic.main()
        self.assertEqual(exit_code, 1)
        prompt.assert_not_called()
        resolve.assert_not_called()
        self.assertIn("failure_stage=input_validation", output.getvalue())

    def test_success_output_is_allowlisted_and_cleanup_is_required(self) -> None:
        source_url = "https://instagram.example/reel/private-fixture"
        credential = "dummy-development-credential"
        with tempfile.TemporaryDirectory() as directory:
            media_directory = Path(directory) / "external-fixture"
            media_directory.mkdir()
            final_path = media_directory / "video-input.mp4"
            final_path.write_bytes(b"\x00\x00\x00\x18ftypmp42fixture")
            result = {
                "temp_file_path": str(final_path),
                "success": True,
                "provider_status_class": "2xx",
                "contract_complete": True,
                "provider_media_mode": "split_av",
                "instagram_candidate_summary": {
                    "raw_video_count": 1,
                    "raw_audio_count": 1,
                    "normalized_video_count": 1,
                    "normalized_audio_count": 1,
                    "normalized_thumbnail_count": 1,
                    "unknown_role_count": 0,
                    "eligible_declared_video_count": 0,
                    "probe_required_video_count": 1,
                },
                "diagnostic_media": {
                    "actual_video_codec": "h264",
                    "actual_container": "mp4",
                    "media_signature_valid": True,
                    "initial_probe_video_present": True,
                    "initial_probe_audio_present": True,
                    "separate_audio_required": False,
                    "audio_download_verified": False,
                    "mux_succeeded": False,
                    "final_video_present": True,
                    "final_audio_present": True,
                },
            }
            output = io.StringIO()
            with patch.dict(
                "os.environ",
                {
                    "APP_ENV": "development",
                    "ALLOW_LIVE_INSTAGRAM_DIAGNOSTIC": "true",
                    "INSTAGRAM_RAPIDAPI_KEY": credential,
                },
                clear=True,
            ), patch.object(
                diagnostic, "_git_branch", return_value="develop"
            ), patch.object(
                diagnostic, "_validate_media_tools"
            ), patch.object(
                diagnostic,
                "_diagnostic_settings",
                return_value=SimpleNamespace(temp_media_dir=directory),
            ), patch.object(
                diagnostic.getpass, "getpass", return_value=source_url
            ), patch.object(
                diagnostic, "resolve_external_media", return_value=result
            ) as resolve, redirect_stdout(output):
                exit_code = diagnostic.main()
            self.assertEqual(exit_code, 0)
            self.assertFalse(media_directory.exists())
            self.assertEqual(resolve.call_args.kwargs["platform"], "instagram")
            rendered = output.getvalue()
            self.assertNotIn(source_url, rendered)
            self.assertNotIn(credential, rendered)
            self.assertNotIn(str(final_path), rendered)
            self.assertNotIn("provider.invalid", rendered)
            self.assertIn("success=true", rendered)
            self.assertIn("media_type=video", rendered)
            self.assertIn("video_candidate_count=1", rendered)
            self.assertIn("audio_candidate_count=1", rendered)
            self.assertIn("thumbnail_candidate_count=1", rendered)
            self.assertIn("duration_within_limit=true", rendered)
            self.assertIn("size_within_limit=true", rendered)
            keys = {line.partition("=")[0] for line in rendered.splitlines()}
            self.assertEqual(keys, set(diagnostic.OUTPUT_KEYS))


if __name__ == "__main__":
    unittest.main()
