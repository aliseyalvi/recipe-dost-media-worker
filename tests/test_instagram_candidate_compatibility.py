from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

from app.media_resolvers.contracts import (
    MediaCandidate,
    MediaResolutionRequest,
    ResolverError,
)
from app.media_resolvers.instagram_unified_v1 import InstagramUnifiedV1Resolver
from app.media_resolvers.media_fetch import DownloadedMedia, cleanup_job_directory
from app.media_resolvers.media_processing import (
    mux_video_audio,
    probe_media,
    validate_final_media,
    validate_video_probe_compatibility,
)
from app.media_resolvers.pipeline import resolve_external_media
from app.main import JsonFormatter

UNIFIED_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "media_resolvers"
    / "rapidapi_instagram_unified_v1"
    / "contracts.json"
)


class FixtureClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def get_json(self, **_kwargs: object):
        return self.payload, "2xx", None


def request() -> MediaResolutionRequest:
    return MediaResolutionRequest(
        platform="instagram",
        canonical_source_url="https://instagram.example/reel/fixture",
        correlation_id="fixture-job",
        capability="video_with_audio",
        byte_limit=50 * 1024 * 1024,
        preferred_byte_target=36 * 1024 * 1024,
        timeout_seconds=15,
    )


def resolver(name: str) -> InstagramUnifiedV1Resolver:
    import json

    fixtures = json.loads(UNIFIED_FIXTURE.read_text(encoding="utf-8"))
    return InstagramUnifiedV1Resolver(
        client=FixtureClient(fixtures[name]),  # type: ignore[arg-type]
        base_url="https://provider.invalid",
        rapidapi_host="provider.invalid",
        rapidapi_key="fixture-secret",
        allowed_media_host_suffixes=("fbcdn.net", "cdninstagram.com"),
    )


class InstagramNormalizationTests(unittest.TestCase):
    def test_log_formatter_keeps_counts_and_drops_sensitive_values(self) -> None:
        record = logging.LogRecord(
            name="recipe_dost.worker",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="instagram_candidate_normalization_summary",
            args=(),
            exc_info=None,
        )
        record.raw_candidate_count = 2
        record.raw_video_count = 1
        record.raw_audio_count = 1
        record.probe_required_video_count = 1
        record.source_url = "https://must-not-appear.invalid/source"
        record.provider_body = {"url": "https://must-not-appear.invalid/media"}
        rendered = JsonFormatter().format(record)
        self.assertIn('"raw_candidate_count":2', rendered)
        self.assertIn('"probe_required_video_count":1', rendered)
        self.assertNotIn("must-not-appear", rendered)
        self.assertNotIn("provider_body", rendered)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class InstagramLocalMediaProofTests(unittest.TestCase):
    def _run_ffmpeg(self, *args: str) -> None:
        result = subprocess.run(
            [str(shutil.which("ffmpeg")), "-v", "error", "-y", *args],
            capture_output=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))

    def test_actual_h264_mp4_probe_and_incompatible_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            compatible = root / "compatible.mp4"
            incompatible = root / "incompatible.mp4"
            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:d=0.25",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(compatible),
            )
            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:d=0.25",
                "-c:v",
                "mpeg4",
                "-an",
                str(incompatible),
            )
            good_probe = probe_media(compatible, timeout_seconds=10)
            validate_video_probe_compatibility(good_probe)
            bad_probe = probe_media(incompatible, timeout_seconds=10)
            with self.assertRaises(ResolverError) as captured:
                validate_video_probe_compatibility(bad_probe)
            self.assertEqual(captured.exception.code, "unsupported_media")
            self.assertEqual(captured.exception.failure_stage, "ffprobe_video")

    def test_split_av_mux_and_progressive_embedded_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "video.mp4"
            audio = root / "audio.m4a"
            merged = root / "merged.mp4"
            progressive = root / "progressive.mp4"
            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:d=0.5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(video),
            )
            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=0.5",
                "-c:a",
                "aac",
                str(audio),
            )
            mux_video_audio(
                video,
                audio,
                output_path=merged,
                timeout_seconds=20,
                byte_limit=5 * 1024 * 1024,
            )
            merged_probe = validate_final_media(
                merged,
                byte_limit=5 * 1024 * 1024,
                duration_limit_seconds=10,
                timeout_seconds=10,
            )
            self.assertTrue(merged_probe.video_present)
            self.assertTrue(merged_probe.audio_present)

            self._run_ffmpeg(
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:d=0.5",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=0.5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(progressive),
            )
            progressive_probe = probe_media(progressive, timeout_seconds=10)
            self.assertTrue(progressive_probe.video_present)
            self.assertTrue(progressive_probe.audio_present)


class InstagramPipelineTests(unittest.TestCase):
    def test_progressive_embedded_audio_skips_mux_and_cleans_on_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resolution = resolver("valid_video_only").resolve(request())
            settings = SimpleNamespace(
                instagram_resolver_chain=["rapidapi_instagram_unified_v1"],
                tiktok_resolver_chain=[],
                max_video_size_bytes=50 * 1024 * 1024,
                preferred_video_size_bytes=36 * 1024 * 1024,
                external_provider_timeout_seconds=15,
                max_video_height=720,
                instagram_media_host_suffixes=["fbcdn.net", "cdninstagram.com"],
                tiktok_media_host_suffixes=[],
                temp_media_dir=directory,
                external_media_download_connect_timeout_seconds=1,
                external_media_download_read_timeout_seconds=1,
                external_media_download_total_timeout_seconds=10,
                external_media_max_redirects=1,
                ytdlp_extract_timeout_seconds=5,
                max_job_runtime_seconds=30,
                max_video_duration_seconds=120,
            )
            repository = MagicMock()
            repository.has_external_resolver_attempt.return_value = False
            built = MagicMock()
            built.resolve.return_value = resolution

            class FakeDownloader:
                def __init__(self, **_kwargs: object) -> None:
                    pass

                def create_job_directory(self, _correlation_id: str) -> Path:
                    path = Path(directory) / "external-instagram-fixture"
                    path.mkdir()
                    return path

                def download_candidate(
                    self,
                    candidate: MediaCandidate,
                    *,
                    job_directory: Path,
                    filename: str,
                    **_kwargs: object,
                ) -> DownloadedMedia:
                    self.assert_video(candidate)
                    path = job_directory / filename
                    path.write_bytes(b"\x00\x00\x00\x18ftypmp42fixture")
                    return DownloadedMedia(path, path.stat().st_size, "video/mp4")

                @staticmethod
                def assert_video(candidate: MediaCandidate) -> None:
                    if candidate.media_type != "video":
                        raise AssertionError("audio candidate selected as video")

            probe = SimpleNamespace(
                video_present=True,
                audio_present=True,
                duration_seconds=30,
                width=1080,
                height=1920,
                video_codec="h264",
                audio_codec="aac",
                container="mov,mp4,m4a,3gp,3g2,mj2",
            )
            with patch(
                "app.media_resolvers.pipeline.build_resolver", return_value=built
            ), patch(
                "app.media_resolvers.pipeline.SecureMediaDownloader", FakeDownloader
            ), patch(
                "app.media_resolvers.pipeline.provider_circuit_breaker.allows",
                return_value=True,
            ), patch(
                "app.media_resolvers.pipeline.provider_circuit_breaker.record_success"
            ), patch(
                "app.media_resolvers.pipeline.probe_media", return_value=probe
            ), patch(
                "app.media_resolvers.pipeline.validate_final_media", return_value=probe
            ), patch("app.media_resolvers.pipeline.mux_video_audio") as mux:
                result = resolve_external_media(
                    repository=repository,
                    settings=settings,  # type: ignore[arg-type]
                    media_job_id=UUID("00000000-0000-0000-0000-000000000777"),
                    source_url="https://instagram.example/reel/fixture",
                    platform="instagram",
                    audience_route="development_test",
                )
            mux.assert_not_called()
            diagnostic = result["diagnostic_media"]
            self.assertFalse(diagnostic["separate_audio_required"])
            self.assertEqual(diagnostic["actual_video_codec"], "h264")
            self.assertEqual(diagnostic["actual_container"], "mp4")
            cleanup_job_directory(Path(str(result["temp_file_path"])).parent)
            self.assertFalse(Path(str(result["temp_file_path"])).parent.exists())


if __name__ == "__main__":
    unittest.main()
