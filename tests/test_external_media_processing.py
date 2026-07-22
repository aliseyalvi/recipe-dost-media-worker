from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

from app.media_resolvers.contracts import MediaCandidate, MediaResolution, ResolverError
from app.media_resolvers.media_fetch import DownloadedMedia, cleanup_job_directory
from app.media_resolvers.media_processing import (
    MediaProbe,
    mux_video_audio,
    probe_media,
    validate_final_media,
)
from app.media_resolvers.pipeline import resolve_external_media


def probe_result(*, video: bool = True, audio: bool = True, duration: int = 60):
    streams = []
    if video:
        streams.append({"codec_type": "video", "codec_name": "h264", "width": 576, "height": 1024})
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"streams": streams, "format": {"duration": str(duration), "format_name": "mov,mp4"}}),
    )


class ExternalMediaProcessingTests(unittest.TestCase):
    def test_probe_and_final_validation_accept_embedded_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"fixture-media")
            with patch("app.media_resolvers.media_processing.shutil.which", return_value="/usr/bin/ffprobe"), patch(
                "app.media_resolvers.media_processing.subprocess.run",
                return_value=probe_result(),
            ):
                probe = validate_final_media(
                    path,
                    byte_limit=1024,
                    duration_limit_seconds=120,
                    timeout_seconds=5,
                )
            self.assertTrue(probe.video_present)
            self.assertTrue(probe.audio_present)

    def test_missing_audio_and_duration_are_safe_terminal_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"fixture-media")
            with patch("app.media_resolvers.media_processing.shutil.which", return_value="/usr/bin/ffprobe"), patch(
                "app.media_resolvers.media_processing.subprocess.run",
                return_value=probe_result(audio=False),
            ):
                with self.assertRaises(ResolverError) as missing:
                    validate_final_media(path, byte_limit=1024, duration_limit_seconds=120, timeout_seconds=5)
            self.assertEqual(missing.exception.code, "media_has_no_audio")
            with patch("app.media_resolvers.media_processing.shutil.which", return_value="/usr/bin/ffprobe"), patch(
                "app.media_resolvers.media_processing.subprocess.run",
                return_value=probe_result(duration=121),
            ):
                with self.assertRaises(ResolverError) as duration:
                    validate_final_media(path, byte_limit=1024, duration_limit_seconds=120, timeout_seconds=5)
            self.assertEqual(duration.exception.code, "video_duration_exceeded")

    def test_mux_uses_argument_array_and_bounded_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "video.mp4"
            audio = root / "audio.m4a"
            output = root / "merged.mp4"
            video.write_bytes(b"video")
            audio.write_bytes(b"audio")

            def run(command, **_kwargs):
                self.assertIsInstance(command, list)
                self.assertNotIn("shell", _kwargs)
                output.write_bytes(b"merged")
                return SimpleNamespace(returncode=0)

            with patch("app.media_resolvers.media_processing.shutil.which", return_value="/usr/bin/ffmpeg"), patch(
                "app.media_resolvers.media_processing.subprocess.run",
                side_effect=run,
            ):
                result = mux_video_audio(
                    video,
                    audio,
                    output_path=output,
                    timeout_seconds=10,
                    byte_limit=1024,
                )
            self.assertEqual(result, output)

    def test_probe_requires_runtime_support(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"fixture")
            with patch("app.media_resolvers.media_processing.shutil.which", return_value=None):
                with self.assertRaises(ResolverError) as captured:
                    probe_media(path, timeout_seconds=5)
            self.assertEqual(captured.exception.code, "resolver_configuration_error")

    def test_shared_pipeline_probes_separate_audio_before_mux(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(
                instagram_resolver_chain=[],
                tiktok_resolver_chain=["akng_tiktok"],
                max_video_size_bytes=50 * 1024 * 1024,
                preferred_video_size_bytes=36 * 1024 * 1024,
                external_provider_timeout_seconds=15,
                max_video_height=720,
                tiktok_media_host_suffixes=["provider.invalid"],
                instagram_media_host_suffixes=[],
                temp_media_dir=directory,
                external_media_download_connect_timeout_seconds=1,
                external_media_download_read_timeout_seconds=1,
                external_media_download_total_timeout_seconds=10,
                external_media_max_redirects=1,
                ytdlp_extract_timeout_seconds=5,
                max_job_runtime_seconds=30,
                max_video_duration_seconds=120,
            )
            resolution = MediaResolution(
                adapter_id="akng_tiktok",
                media_mode="split_av",
                candidates=(
                    MediaCandidate(
                        media_type="video",
                        _urls=("https://provider.invalid/video",),
                        width=576,
                        height=1024,
                        size_bytes=100,
                        codec="h264",
                        container="mp4",
                    ),
                    MediaCandidate(
                        media_type="audio",
                        _urls=("https://provider.invalid/audio",),
                        size_bytes=20,
                        codec="aac",
                        container="m4a",
                    ),
                ),
                raw_video_candidate_count=1,
                raw_audio_candidate_count=1,
                contract_diagnostic={
                    "provider_status_class": "2xx",
                    "provider_input_kind": "canonical_url",
                    "provider_attempt": 1,
                    "contract_complete": True,
                },
            )
            resolver = MagicMock()
            resolver.resolve.return_value = resolution
            repository = MagicMock()
            repository.has_external_resolver_attempt.return_value = False

            class FakeDownloader:
                def __init__(self, **_kwargs: object) -> None:
                    pass

                def create_job_directory(self, _correlation_id: str) -> Path:
                    path = Path(directory) / "external-fixture"
                    path.mkdir()
                    return path

                def download_candidate(
                    self,
                    candidate: MediaCandidate,
                    *,
                    job_directory: Path,
                    filename: str,
                    byte_limit: int | None = None,
                    deadline_monotonic: float | None = None,
                    probe_alternatives: bool = False,
                ) -> DownloadedMedia:
                    del byte_limit, deadline_monotonic, probe_alternatives
                    path = job_directory / filename
                    path.write_bytes(b"\x00\x00\x00\x18ftypmp42fixture")
                    return DownloadedMedia(
                        path,
                        path.stat().st_size,
                        "video/mp4" if candidate.media_type == "video" else "audio/mp4",
                    )

            video_probe = MediaProbe(True, False, 30, 576, 1024, "h264", None, "mp4")
            audio_probe = MediaProbe(False, True, 30, None, None, None, "aac", "mp4")
            final_probe = MediaProbe(True, True, 30, 576, 1024, "h264", "aac", "mp4")

            def fake_mux(
                _video: Path,
                _audio: Path,
                *,
                output_path: Path,
                **_kwargs: object,
            ) -> Path:
                output_path.write_bytes(b"\x00\x00\x00\x18ftypmp42merged")
                return output_path

            with patch("app.media_resolvers.pipeline.build_resolver", return_value=resolver), patch(
                "app.media_resolvers.pipeline.SecureMediaDownloader", FakeDownloader
            ), patch(
                "app.media_resolvers.pipeline.provider_circuit_breaker.allows",
                return_value=True,
            ), patch(
                "app.media_resolvers.pipeline.provider_circuit_breaker.record_success"
            ), patch(
                "app.media_resolvers.pipeline.probe_media",
                side_effect=[video_probe, audio_probe],
            ) as probe, patch(
                "app.media_resolvers.pipeline.mux_video_audio",
                side_effect=fake_mux,
            ) as mux, patch(
                "app.media_resolvers.pipeline.validate_final_media",
                return_value=final_probe,
            ):
                result = resolve_external_media(
                    repository=repository,
                    settings=settings,  # type: ignore[arg-type]
                    media_job_id=UUID("00000000-0000-0000-0000-000000000123"),
                    source_url="https://www.tiktok.com/@fixture/video/7651218940919598000",
                    platform="tiktok",
                    audience_route="development_test",
                )
            self.assertEqual(probe.call_count, 2)
            mux.assert_called_once()
            diagnostic = result["diagnostic_media"]
            self.assertIsInstance(diagnostic, dict)
            self.assertTrue(diagnostic["audio_download_verified"])
            self.assertTrue(diagnostic["mux_succeeded"])
            cleanup_job_directory(Path(str(result["temp_file_path"])).parent)


if __name__ == "__main__":
    unittest.main()
