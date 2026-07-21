from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.media_resolvers.contracts import ResolverError
from app.media_resolvers.media_processing import (
    mux_video_audio,
    probe_media,
    validate_final_media,
)


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


if __name__ == "__main__":
    unittest.main()
