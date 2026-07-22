from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.media_resolvers.contracts import ResolverError
from scripts import check_tiktok_akng_end_to_end as diagnostic


class TikTokLiveDiagnosticTests(unittest.TestCase):
    def test_guard_failure_is_safe_and_never_prompts_or_calls_provider(self) -> None:
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), patch.object(
            diagnostic.getpass, "getpass"
        ) as prompt, patch.object(
            diagnostic, "resolve_external_media"
        ) as resolve, redirect_stdout(output):
            exit_code = diagnostic.main(["--input-kind", "numeric_id"])
        self.assertEqual(exit_code, 1)
        prompt.assert_not_called()
        resolve.assert_not_called()
        self.assertIn("failure_stage=input_validation", output.getvalue())

    def test_explicit_mode_is_forwarded_and_success_output_is_allowlisted(self) -> None:
        hidden_input = "7651218940919598000"
        token = "dummy-development-token"
        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory)
            media_directory = temp_root / "external-fixture"
            media_directory.mkdir()
            final_path = media_directory / "validated-media.mp4"
            final_path.write_bytes(b"\x00\x00\x00\x18ftypmp42fixture")
            settings = SimpleNamespace(temp_media_dir=directory)
            result = {
                "temp_file_path": str(final_path),
                "video_bytes_verified": True,
                "provider_input_kind": "numeric_id",
                "provider_status_class": "2xx",
                "contract_complete": True,
                "short_link_expansion_attempted": False,
                "short_link_expansion_completed": False,
                "canonical_url_resolved": False,
                "numeric_id_resolved": True,
                "short_link_redirect_count": 0,
                "raw_video_candidate_count": 6,
                "eligible_video_candidate_count": 3,
                "selected_candidate": "primary",
                "diagnostic_media": {
                    "selected_candidate": {"codec": "h264", "container": "mp4"},
                    "downloaded_size_bucket": "under_20_mib",
                    "media_signature_valid": True,
                    "final_video_present": True,
                    "final_audio_present": True,
                    "resolution_bucket": "540p",
                },
            }
            output = io.StringIO()
            with patch.dict(
                "os.environ",
                {
                    "APP_ENV": "development",
                    "ALLOW_LIVE_TIKTOK_DIAGNOSTIC": "true",
                    "TIKTOK_AKNG_ACCESS_TOKEN": token,
                },
                clear=True,
            ), patch.object(
                diagnostic, "_git_branch", return_value="develop"
            ), patch.object(
                diagnostic, "_validate_media_tools"
            ), patch.object(
                diagnostic, "_diagnostic_settings", return_value=settings
            ), patch.object(
                diagnostic.getpass, "getpass", return_value=hidden_input
            ), patch.object(
                diagnostic, "resolve_external_media", return_value=result
            ) as resolve, redirect_stdout(output):
                exit_code = diagnostic.main(["--input-kind", "numeric_id"])
            self.assertEqual(exit_code, 0)
            self.assertFalse(media_directory.exists())
            self.assertEqual(
                resolve.call_args.kwargs["source_input_kind"], "numeric_id"
            )
            rendered = output.getvalue()
            self.assertNotIn(hidden_input, rendered)
            self.assertNotIn(token, rendered)
            self.assertNotIn(str(final_path), rendered)
            keys = {line.partition("=")[0] for line in rendered.splitlines()}
            self.assertEqual(keys, set(diagnostic.OUTPUT_KEYS))

    def test_resolver_failure_preserves_closed_failure_stage(self) -> None:
        failure = ResolverError(
            "media_unavailable",
            "safe",
            failure_stage="short_link_redirect_validation",
            diagnostic_state={
                "short_link_expansion_attempted": True,
                "short_link_expansion_completed": False,
            },
        )
        output = io.StringIO()
        with patch.dict(
            "os.environ",
            {
                "APP_ENV": "development",
                "ALLOW_LIVE_TIKTOK_DIAGNOSTIC": "true",
                "TIKTOK_AKNG_ACCESS_TOKEN": "dummy-development-token",
            },
            clear=True,
        ), patch.object(
            diagnostic, "_git_branch", return_value="develop"
        ), patch.object(
            diagnostic, "_validate_media_tools"
        ), patch.object(
            diagnostic, "_diagnostic_settings", return_value=SimpleNamespace()
        ), patch.object(
            diagnostic.getpass, "getpass", return_value="https://vt.tiktok.com/fixture/"
        ), patch.object(
            diagnostic, "resolve_external_media", side_effect=failure
        ), redirect_stdout(output):
            exit_code = diagnostic.main(["--input-kind", "short_url"])
        self.assertEqual(exit_code, 1)
        self.assertIn(
            "failure_stage=short_link_redirect_validation", output.getvalue()
        )

    def test_missing_ffprobe_fails_before_prompt_or_provider(self) -> None:
        output = io.StringIO()
        with patch.dict(
            "os.environ",
            {
                "APP_ENV": "development",
                "ALLOW_LIVE_TIKTOK_DIAGNOSTIC": "true",
                "TIKTOK_AKNG_ACCESS_TOKEN": "dummy-development-token",
            },
            clear=True,
        ), patch.object(
            diagnostic, "_git_branch", return_value="develop"
        ), patch.object(
            diagnostic.shutil, "which", return_value=None
        ), patch.object(
            diagnostic.getpass, "getpass"
        ) as prompt, patch.object(
            diagnostic, "resolve_external_media"
        ) as resolve, redirect_stdout(output):
            exit_code = diagnostic.main(["--input-kind", "numeric_id"])
        self.assertEqual(exit_code, 1)
        prompt.assert_not_called()
        resolve.assert_not_called()
        self.assertIn("failure_stage=ffprobe_video", output.getvalue())


if __name__ == "__main__":
    unittest.main()
