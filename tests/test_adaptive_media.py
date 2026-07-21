from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from app.config import Settings
from app.social import stream_download

MIB = 1024 * 1024


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "WORKER_API_SECRET": "test",
        "supabase_url": "https://example.supabase.co",
        "supabase_service_role_key": "test",
        "preferred_video_size_mb": 36,
        "max_video_size_mb": 50,
        "gemini_file_max_size_bytes": 2_000_000_000,
        "media_adaptive_max_attempts": 4,
        "max_video_height": 720,
        "min_video_height": 360,
        "enable_cookie_file": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def media_format(
    format_id: str,
    short_edge: int,
    *,
    size: int | None = None,
    approx: int | None = None,
    ext: str = "mp4",
    vcodec: str = "h264",
    acodec: str = "aac",
    vbr: float | None = None,
    abr: float | None = None,
) -> dict[str, object]:
    return {
        "format_id": format_id,
        "ext": ext,
        "width": short_edge,
        "height": int(short_edge * 16 / 9),
        "vcodec": vcodec,
        "acodec": acodec,
        "filesize": size,
        "filesize_approx": approx,
        "vbr": vbr,
        "abr": abr,
        "fps": 30,
        "url": "https://cdn.invalid/media",
    }


def candidates(info: dict[str, object], active: Settings | None = None):
    return stream_download.build_media_format_candidates(info, active or settings())


def direct_incident_info(*, size: int | None = 42_996_054) -> dict[str, object]:
    return {
        "duration": 63,
        "formats": [],
        "requested_formats": [],
        "url": "https://cdn.invalid/direct-media.mp4",
        "ext": "mp4",
        "width": 1080,
        "height": 1920,
        "filesize": size,
        "protocol": "https",
    }


class FormatSelectionTests(unittest.TestCase):
    def test_incident_top_level_direct_candidate_is_normalized_with_unknown_audio(self) -> None:
        info = direct_incident_info()
        normalized = stream_download.normalize_media_candidate_inputs(info)
        selected = candidates(info)
        attempts = stream_download.build_adaptive_attempts(selected, settings())
        self.assertEqual(len(normalized), 1)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].source_type, "top_level_direct")
        self.assertEqual(selected[0].audio_state, "audio_unknown")
        self.assertIsNone(selected[0].format_id)
        self.assertEqual(len(attempts), 1)

    def test_requested_formats_are_normalized_and_top_level_duplicate_is_removed(self) -> None:
        requested = media_format("requested", 720, size=MIB)
        info = {
            "duration": 30,
            "formats": [],
            "requested_formats": [requested],
            **requested,
        }
        normalized = stream_download.normalize_media_candidate_inputs(info)
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0].source_type, "requested_formats")

    def test_audio_tri_state(self) -> None:
        self.assertEqual(
            stream_download.classify_audio_state({"acodec": "aac"}),
            "audio_present",
        )
        self.assertEqual(
            stream_download.classify_audio_state({"acodec": "none"}),
            "audio_absent",
        )
        self.assertEqual(stream_download.classify_audio_state({}), "audio_unknown")

    def test_1080_above_target_and_720_below_selects_720(self) -> None:
        info = {
            "duration": 63,
            "formats": [
                media_format("1080", 1080, size=42_996_054),
                media_format("720", 720, size=32 * MIB),
            ],
        }
        attempts = stream_download.build_adaptive_attempts(candidates(info), settings())
        self.assertEqual(attempts[0].format_id, "720")
        self.assertEqual(attempts[0].resolution_bucket, "720p")
        self.assertNotIn("1080", [attempt.format_id for attempt in attempts])

    def test_720_above_target_and_540_below_selects_540(self) -> None:
        info = {
            "duration": 60,
            "formats": [
                media_format("720", 720, size=37 * MIB),
                media_format("540", 540, size=30 * MIB),
            ],
        }
        attempts = stream_download.build_adaptive_attempts(candidates(info), settings())
        self.assertEqual(attempts[0].format_id, "540")

    def test_540_unavailable_and_480_acceptable_selects_480(self) -> None:
        info = {
            "duration": 60,
            "formats": [media_format("480", 480, size=20 * MIB)],
        }
        attempts = stream_download.build_adaptive_attempts(candidates(info), settings())
        self.assertEqual(attempts[0].format_id, "480")

    def test_size_estimation_order_and_combined_bitrate(self) -> None:
        info = {
            "duration": 100,
            "formats": [
                media_format("exact", 720, size=10, approx=20, vbr=1000, abr=128),
                media_format("approx", 540, approx=20, vbr=1000, abr=128),
                media_format("bitrate", 480, vbr=1000, abr=128),
            ],
        }
        by_id = {item.format_id: item for item in candidates(info)}
        self.assertEqual(by_id["exact"].estimated_size_bytes, 10)
        self.assertEqual(by_id["exact"].estimate_source, "filesize")
        self.assertEqual(by_id["approx"].estimated_size_bytes, 20)
        self.assertEqual(by_id["approx"].estimate_source, "filesize_approx")
        self.assertEqual(by_id["bitrate"].total_bitrate_kbps, 1128)
        self.assertEqual(
            by_id["bitrate"].estimated_size_bytes,
            int(100 * 1128 * 1000 / 8 * 1.05),
        )

    def test_audio_only_video_only_and_unsupported_containers_are_rejected(self) -> None:
        info = {
            "duration": 30,
            "formats": [
                media_format("audio", 480, size=MIB, vcodec="none"),
                media_format("video", 480, size=MIB, acodec="none"),
                media_format("image", 480, size=MIB, ext="jpg"),
                media_format("good", 480, size=MIB),
            ],
        }
        selected = candidates(info)
        self.assertEqual([item.format_id for item in selected], ["good"])
        self.assertTrue(selected[0].has_audio)
        self.assertTrue(selected[0].has_video)

    def test_unknown_size_is_bounded_as_final_attempt(self) -> None:
        info = {"duration": None, "formats": [media_format("unknown", 480)]}
        attempts = stream_download.build_adaptive_attempts(candidates(info), settings())
        self.assertEqual([item.format_id for item in attempts], ["unknown"])
        self.assertEqual(attempts[0].estimate_source, "unknown")

    def test_one_smallest_candidate_below_hard_limit_when_none_fit_preferred(self) -> None:
        info = {
            "duration": 60,
            "formats": [
                media_format("720", 720, size=45 * MIB),
                media_format("480", 480, size=40 * MIB),
            ],
        }
        attempts = stream_download.build_adaptive_attempts(candidates(info), settings())
        self.assertEqual([attempt.format_id for attempt in attempts], ["480"])

    def test_attempt_count_is_bounded(self) -> None:
        info = {
            "duration": 60,
            "formats": [
                media_format(str(height), height, size=(height // 20) * MIB)
                for height in (720, 600, 540, 480, 420, 360)
            ],
        }
        attempts = stream_download.build_adaptive_attempts(candidates(info), settings())
        self.assertLessEqual(len(attempts), 4)
        self.assertEqual(len({item.format_id for item in attempts}), len(attempts))

    def test_incident_all_candidates_above_hard_limit_is_terminal_size_failure(self) -> None:
        active = settings()
        info = {
            "duration": 63,
            "formats": [
                media_format("1080", 1080, size=60 * MIB),
                media_format("720", 720, size=55 * MIB),
                media_format("480", 480, size=51 * MIB),
            ],
        }
        result = stream_download.no_candidate_result(
            "instagram",
            info,
            resolver="ytdlp_stream_cookies",
            cookie_mode="platform_cookie_file",
            output_mode="temp_file",
            settings=active,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "media_size_exceeded")
        self.assertEqual(result["safe_error_message"], stream_download.TERMINAL_SIZE_MESSAGE)


class FakeYoutubeDL:
    info: dict[str, object] = {}
    actual_sizes: dict[str, int] = {}
    selected_formats: list[str] = []
    require_cookie = False

    def __init__(self, options: dict[str, object]) -> None:
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, _url: str, download: bool = False):
        if self.require_cookie and not self.options.get("cookiefile"):
            raise DownloadError("login required https://source.invalid")
        return self.info

    def download(self, _urls: list[str]) -> None:
        format_id = str(self.options["format"])
        self.selected_formats.append(format_id)
        actual_size = self.actual_sizes[format_id]
        output = Path(str(self.options["outtmpl"]).replace("%(ext)s", "mp4"))
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_suffix(".part")
        partial.write_bytes(b"partial")
        for hook in self.options.get("progress_hooks", []):
            hook({"downloaded_bytes": actual_size})
        partial.unlink()
        output.write_bytes(b"x" * actual_size)


class DownloadError(Exception):
    pass


class FakeDirectResponse:
    def __init__(self, chunks: list[bytes], declared_size: int | None = None) -> None:
        self.chunks = chunks
        self.headers = (
            {"content-length": str(declared_size)} if declared_size is not None else {}
        )

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield from self.chunks


class AdaptiveDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeYoutubeDL.selected_formats = []
        FakeYoutubeDL.require_cookie = False

    def _info(self) -> dict[str, object]:
        return {
            "duration": 63,
            "title": "fixture",
            "formats": [
                media_format("720", 720, size=MIB),
                media_format("540", 540, size=MIB // 2),
            ],
        }

    def _run(self, active: Settings, info: dict[str, object]) -> dict[str, object]:
        attempts = stream_download.build_adaptive_attempts(candidates(info, active), active)
        with patch("yt_dlp.YoutubeDL", FakeYoutubeDL):
            return stream_download.extract_to_temp_file(
                "https://source.invalid/video",
                "instagram",
                "fixture-job",
                active,
                stream_download.ytdlp_options("instagram", active, None),
                info,
                attempts,
                resolver="ytdlp_stream",
                cookie_mode="none",
            )

    def test_actual_below_preferred_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active = settings(
                preferred_video_size_mb=1,
                max_video_size_mb=2,
                temp_media_dir=temp_dir,
            )
            info = self._info()
            FakeYoutubeDL.info = info
            FakeYoutubeDL.actual_sizes = {"720": MIB, "540": MIB // 2}
            result = self._run(active, info)
            self.assertTrue(result["success"])
            self.assertEqual(result["selected_resolution_bucket"], "720p")
            stream_download.cleanup_job_dir(Path(temp_dir) / "fixture-job")

    def test_incident_fixture_succeeds_with_smaller_720p_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active = settings(temp_media_dir=temp_dir)
            info = {
                "duration": 63,
                "formats": [
                    media_format("1080", 1080, size=42_996_054),
                    media_format("720", 720, size=32 * MIB),
                ],
            }
            FakeYoutubeDL.info = info
            FakeYoutubeDL.actual_sizes = {"720": 32 * MIB}
            result = self._run(active, info)
            self.assertTrue(result["success"])
            self.assertEqual(FakeYoutubeDL.selected_formats, ["720"])
            self.assertEqual(result["selected_resolution_bucket"], "720p")
            stream_download.cleanup_job_dir(Path(temp_dir) / "fixture-job")

    def test_actual_above_preferred_moves_to_smaller_and_cleans_first_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active = settings(
                preferred_video_size_mb=1,
                max_video_size_mb=2,
                temp_media_dir=temp_dir,
            )
            info = self._info()
            FakeYoutubeDL.info = info
            FakeYoutubeDL.actual_sizes = {"720": MIB + 1, "540": MIB // 2}
            result = self._run(active, info)
            self.assertTrue(result["success"])
            self.assertEqual(FakeYoutubeDL.selected_formats, ["720", "540"])
            self.assertEqual(result["adaptive_attempt_count"], 2)
            files = list((Path(temp_dir) / "fixture-job").iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].stat().st_size, MIB // 2)
            stream_download.cleanup_job_dir(Path(temp_dir) / "fixture-job")

    def test_hard_guard_aborts_and_removes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active = settings(
                preferred_video_size_mb=1,
                max_video_size_mb=2,
                media_adaptive_max_attempts=1,
                temp_media_dir=temp_dir,
            )
            info = self._info()
            FakeYoutubeDL.info = info
            FakeYoutubeDL.actual_sizes = {"720": 2 * MIB + 1, "540": MIB // 2}
            result = self._run(active, info)
            self.assertFalse(result["success"])
            self.assertEqual(result["error_code"], "media_size_exceeded")
            self.assertFalse((Path(temp_dir) / "fixture-job").exists())

    def test_streaming_guard_exact_byte_boundaries(self) -> None:
        guard = stream_download.HardByteGuard(50 * MIB)
        guard({"downloaded_bytes": 50 * MIB - 1})
        guard({"downloaded_bytes": 50 * MIB})
        self.assertEqual(guard.observed_bytes, 50 * MIB)
        with self.assertRaises(stream_download.HardByteLimitExceeded):
            guard({"downloaded_bytes": 50 * MIB + 1})

    def test_cookie_retry_repeats_metadata_access_but_keeps_adaptive_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_path = Path(temp_dir) / "cookies.txt"
            cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            active = settings(
                preferred_video_size_mb=1,
                max_video_size_mb=2,
                temp_media_dir=temp_dir,
                enable_ytdlp_cookies=True,
                enable_tiktok_cookies=True,
                tiktok_cookies_file=str(cookie_path),
            )
            info = self._info()
            FakeYoutubeDL.info = info
            FakeYoutubeDL.actual_sizes = {"720": MIB, "540": MIB // 2}
            FakeYoutubeDL.require_cookie = True
            with patch("yt_dlp.YoutubeDL", FakeYoutubeDL):
                result = stream_download.extract_social_video(
                    "https://source.invalid/video",
                    "tiktok",
                    "fixture-job",
                    active,
                )
            self.assertTrue(result["success"])
            self.assertTrue(result["cookie_retry_attempted"])
            self.assertEqual(result["selected_resolution_bucket"], "720p")
            stream_download.cleanup_job_dir(Path(temp_dir) / "fixture-job")

    def test_direct_incident_probe_and_optimization_success(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active = settings(
                preferred_video_size_mb=1,
                max_video_size_mb=50,
                temp_media_dir=temp_dir,
            )
            info = direct_incident_info()
            attempts = stream_download.build_adaptive_attempts(candidates(info, active), active)
            probe = stream_download.MediaProbeResult(
                True,
                True,
                True,
                duration_seconds=63,
                width=1080,
                height=1920,
            )
            with patch(
                "app.social.stream_download.requests.get",
                return_value=FakeDirectResponse([b"x" * MIB, b"y"]),
            ), patch(
                "app.social.stream_download.probe_downloaded_media",
                return_value=probe,
            ), patch(
                "app.social.stream_download.optimize_downloaded_media",
                side_effect=lambda path, _job_dir, _settings: (path, True, True),
            ):
                result = stream_download.extract_to_temp_file(
                    "https://source.invalid/video",
                    "instagram",
                    "fixture-job",
                    active,
                    stream_download.ytdlp_options("instagram", active, None),
                    info,
                    attempts,
                    resolver="ytdlp_stream_cookies",
                    cookie_mode="platform_cookie_file",
                )
            self.assertTrue(result["success"])
            self.assertEqual(result["normalized_candidate_count"], 1)
            self.assertEqual(result["format_candidate_count"], 1)
            self.assertEqual(result["candidate_source_type"], "top_level_direct")
            self.assertEqual(result["audio_state"], "audio_unknown")
            self.assertEqual(result["adaptive_attempt_count"], 1)
            self.assertTrue(result["probe_performed"])
            self.assertTrue(result["probe_audio_present"])
            self.assertTrue(result["probe_video_present"])
            self.assertTrue(result["optimization_attempted"])
            self.assertTrue(result["optimization_succeeded"])
            stream_download.cleanup_job_dir(Path(temp_dir) / "fixture-job")

    def test_direct_candidate_without_filesize_uses_streaming_hard_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active = settings(
                preferred_video_size_mb=1,
                max_video_size_mb=2,
                temp_media_dir=temp_dir,
            )
            info = direct_incident_info(size=None)
            attempts = stream_download.build_adaptive_attempts(candidates(info, active), active)
            with patch(
                "app.social.stream_download.requests.get",
                return_value=FakeDirectResponse([b"x" * MIB, b"y" * MIB, b"z"]),
            ):
                result = stream_download.extract_to_temp_file(
                    "https://source.invalid/video",
                    "instagram",
                    "fixture-job",
                    active,
                    stream_download.ytdlp_options("instagram", active, None),
                    info,
                    attempts,
                    resolver="ytdlp_stream_cookies",
                    cookie_mode="platform_cookie_file",
                )
            self.assertFalse(result["success"])
            self.assertEqual(result["error_code"], "media_size_exceeded")
            self.assertFalse((Path(temp_dir) / "fixture-job").exists())

    def test_direct_probe_without_audio_uses_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active = settings(
                preferred_video_size_mb=1,
                max_video_size_mb=3,
                temp_media_dir=temp_dir,
            )
            info = direct_incident_info(size=MIB)
            fallback_raw = media_format("fallback", 480, size=MIB // 2)
            direct = candidates(info, active)[0]
            fallback = candidates({"duration": 63, "formats": [fallback_raw]}, active)[0]
            FakeYoutubeDL.actual_sizes = {"fallback": MIB // 2}
            with patch(
                "app.social.stream_download.requests.get",
                return_value=FakeDirectResponse([b"x" * MIB]),
            ), patch(
                "app.social.stream_download.probe_downloaded_media",
                return_value=stream_download.MediaProbeResult(True, True, False),
            ), patch("yt_dlp.YoutubeDL", FakeYoutubeDL):
                result = stream_download.extract_to_temp_file(
                    "https://source.invalid/video",
                    "instagram",
                    "fixture-job",
                    active,
                    stream_download.ytdlp_options("instagram", active, None),
                    info,
                    [direct, fallback],
                    resolver="ytdlp_stream_cookies",
                    cookie_mode="platform_cookie_file",
                )
            self.assertTrue(result["success"])
            self.assertEqual(result["adaptive_attempt_count"], 2)
            self.assertEqual(FakeYoutubeDL.selected_formats, ["fallback"])
            stream_download.cleanup_job_dir(Path(temp_dir) / "fixture-job")

    def test_direct_probe_without_audio_has_safe_terminal_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            active = settings(temp_media_dir=temp_dir)
            info = direct_incident_info(size=MIB)
            attempts = stream_download.build_adaptive_attempts(candidates(info, active), active)
            with patch(
                "app.social.stream_download.requests.get",
                return_value=FakeDirectResponse([b"x" * MIB]),
            ), patch(
                "app.social.stream_download.probe_downloaded_media",
                return_value=stream_download.MediaProbeResult(True, True, False),
            ):
                result = stream_download.extract_to_temp_file(
                    "https://source.invalid/video",
                    "instagram",
                    "fixture-job",
                    active,
                    stream_download.ytdlp_options("instagram", active, None),
                    info,
                    attempts,
                    resolver="ytdlp_stream_cookies",
                    cookie_mode="platform_cookie_file",
                )
            self.assertFalse(result["success"])
            self.assertEqual(result["error_code"], "unified_extractor_failed")
            self.assertTrue(result["probe_performed"])
            self.assertFalse(result["probe_audio_present"])
            self.assertFalse((Path(temp_dir) / "fixture-job").exists())

    def test_direct_candidate_above_hard_limit_is_not_downloaded(self) -> None:
        active = settings()
        FakeYoutubeDL.info = direct_incident_info(size=51 * MIB)
        with patch("yt_dlp.YoutubeDL", FakeYoutubeDL), patch(
            "app.social.stream_download.requests.get"
        ) as direct_download:
            result = stream_download.try_ytdlp_extract(
                "https://source.invalid/video",
                "instagram",
                "fixture-job",
                active,
                stream_download.ytdlp_options("instagram", active, None),
                cookie_mode="platform_cookie_file",
                resolver="ytdlp_stream_cookies",
                output_mode="temp_file",
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "media_size_exceeded")
        self.assertEqual(result["format_candidate_count"], 1)
        direct_download.assert_not_called()

    def test_ffprobe_unavailable_is_reported_without_executing_a_process(self) -> None:
        with patch("app.social.stream_download.shutil.which", return_value=None), patch(
            "app.social.stream_download.subprocess.run"
        ) as run:
            result = stream_download.probe_downloaded_media(Path("unused.mp4"), settings())
        self.assertFalse(result.performed)
        run.assert_not_called()

    def test_ffprobe_result_uses_only_stream_presence_and_safe_dimensions(self) -> None:
        output = json.dumps(
            {
                "streams": [
                    {"codec_type": "video", "width": 1080, "height": 1920},
                    {"codec_type": "audio"},
                ],
                "format": {"duration": "63.2"},
            }
        )
        with patch("app.social.stream_download.shutil.which", return_value="ffprobe"), patch(
            "app.social.stream_download.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout=output),
        ):
            result = stream_download.probe_downloaded_media(Path("fixture.mp4"), settings())
        self.assertTrue(result.performed)
        self.assertTrue(result.video_present)
        self.assertTrue(result.audio_present)
        self.assertEqual((result.width, result.height, result.duration_seconds), (1080, 1920, 63))

    def test_cookie_authenticated_direct_candidate_uses_same_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cookie_path = Path(temp_dir) / "cookies.txt"
            cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            active = settings(
                temp_media_dir=temp_dir,
                enable_ytdlp_cookies=True,
                enable_instagram_cookies=True,
                instagram_cookies_file=str(cookie_path),
            )
            FakeYoutubeDL.info = direct_incident_info(size=512)
            probe = stream_download.MediaProbeResult(True, True, True)
            with patch("yt_dlp.YoutubeDL", FakeYoutubeDL), patch(
                "app.social.stream_download.requests.get",
                return_value=FakeDirectResponse([b"x" * 512]),
            ), patch(
                "app.social.stream_download.probe_downloaded_media",
                return_value=probe,
            ):
                result = stream_download.extract_social_video(
                    "https://source.invalid/video",
                    "instagram",
                    "fixture-job",
                    active,
                )
            self.assertTrue(result["success"])
            self.assertTrue(result["cookie_retry_attempted"])
            self.assertEqual(result["normalized_candidate_count"], 1)
            self.assertEqual(result["format_candidate_count"], 1)
            self.assertGreaterEqual(result["adaptive_attempt_count"], 1)
            self.assertEqual(result["candidate_source_type"], "top_level_direct")
            stream_download.cleanup_job_dir(Path(temp_dir) / "fixture-job")


class MediaLimitConfigurationTests(unittest.TestCase):
    def test_mib_conversion_and_boundaries(self) -> None:
        active = settings(preferred_video_size_mb=36, max_video_size_mb=50)
        self.assertEqual(active.preferred_video_size_bytes, 36 * MIB)
        self.assertEqual(active.max_video_size_bytes, 50 * MIB)
        self.assertLess(50 * MIB - 1, active.max_video_size_bytes)
        self.assertEqual(50 * MIB, active.max_video_size_bytes)
        self.assertGreater(50 * MIB + 1, active.max_video_size_bytes)

    def test_malformed_configuration_fails_closed(self) -> None:
        with self.assertRaises(ValidationError):
            settings(max_video_size_mb="not-a-number")

    def test_provider_limit_below_worker_limit_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            settings(gemini_file_max_size_bytes=49 * MIB)


if __name__ == "__main__":
    unittest.main()
