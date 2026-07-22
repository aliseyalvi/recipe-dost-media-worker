from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

from app.media_resolvers.contracts import MediaCandidate, MediaResolution, ResolverError
from app.media_resolvers.media_fetch import DownloadedMedia, cleanup_job_directory
from app.media_resolvers.media_processing import MediaProbe
from app.media_resolvers.pipeline import resolve_external_media


def settings_for(directory: str) -> SimpleNamespace:
    return SimpleNamespace(
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


def video_candidates() -> tuple[MediaCandidate, ...]:
    return tuple(
        MediaCandidate(
            media_type="video",
            _urls=(f"https://provider.invalid/video-{index}",),
            width=576,
            height=1024,
            size_bytes=size,
            bitrate=bitrate,
            codec=codec,
            container="mp4",
            candidate_id=candidate_id,
        )
        for index, (size, bitrate, codec, candidate_id) in enumerate(
            (
                (6_729_049, 1_037_892, "h264", "preferred"),
                (4_721_022, 728_173, "h264", "secondary"),
                (6_729_049, 0, "", "top_level_progressive"),
            ),
            start=1,
        )
    )


class FakeDownloader:
    root: Path
    created: list[Path]

    def __init__(self, **kwargs: object) -> None:
        type(self).root = Path(str(kwargs["temp_root"]))
        type(self).created = []

    def create_job_directory(self, correlation_id: str) -> Path:
        path = self.root / f"external-{correlation_id[-1]}"
        path.mkdir()
        self.created.append(path)
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
        del candidate, byte_limit, deadline_monotonic, probe_alternatives
        path = job_directory / filename
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42fixture")
        return DownloadedMedia(path, path.stat().st_size, "video/mp4")


class TikTokCandidateFallbackTests(unittest.TestCase):
    def _resolution(self) -> MediaResolution:
        return MediaResolution(
            adapter_id="akng_tiktok",
            media_mode="progressive",
            candidates=video_candidates(),
            raw_video_candidate_count=6,
            raw_audio_candidate_count=0,
            contract_diagnostic={
                "provider_call_attempted": True,
                "provider_status_class": "2xx",
                "provider_input_kind": "numeric_id",
                "contract_complete": True,
                "numeric_id_resolved": True,
            },
        )

    def _common_patches(self, resolver: MagicMock):
        return (
            patch("app.media_resolvers.pipeline.build_resolver", return_value=resolver),
            patch("app.media_resolvers.pipeline.SecureMediaDownloader", FakeDownloader),
            patch(
                "app.media_resolvers.pipeline.provider_circuit_breaker.allows",
                return_value=True,
            ),
            patch("app.media_resolvers.pipeline.provider_circuit_breaker.record_success"),
            patch("app.media_resolvers.pipeline.provider_circuit_breaker.record_failure"),
        )

    def test_second_h264_candidate_is_used_when_preferred_has_no_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resolver = MagicMock()
            resolver.resolve.return_value = self._resolution()
            repository = MagicMock()
            repository.has_external_resolver_attempt.return_value = False
            no_audio = MediaProbe(True, False, 30, 576, 1024, "h264", None, "mp4")
            embedded_audio = MediaProbe(True, True, 30, 576, 1024, "h264", "aac", "mp4")
            patches = self._common_patches(resolver)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patch(
                "app.media_resolvers.pipeline.probe_media",
                side_effect=[no_audio, embedded_audio],
            ), patch(
                "app.media_resolvers.pipeline.validate_final_media",
                return_value=embedded_audio,
            ):
                result = resolve_external_media(
                    repository=repository,
                    settings=settings_for(directory),  # type: ignore[arg-type]
                    media_job_id=UUID("00000000-0000-0000-0000-000000000321"),
                    source_url="https://www.tiktok.com/@fixture/video/7651218940919598000",
                    platform="tiktok",
                    audience_route="development_test",
                )
            self.assertEqual(result["selected_candidate"], "fallback_2")
            self.assertTrue(result["candidate_fallback_used"])
            event_types = [
                call.kwargs.get("event_type")
                for call in repository.insert_event.call_args_list
            ]
            self.assertIn("media_candidate_fallback", event_types)
            self.assertFalse(FakeDownloader.created[0].exists())
            cleanup_job_directory(Path(str(result["temp_file_path"])).parent)

    def test_all_candidates_without_audio_fail_as_media_has_no_audio_and_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            resolver = MagicMock()
            resolver.resolve.return_value = self._resolution()
            repository = MagicMock()
            repository.has_external_resolver_attempt.return_value = False
            no_audio = MediaProbe(True, False, 30, 576, 1024, "h264", None, "mp4")
            patches = self._common_patches(resolver)
            with patches[0], patches[1], patches[2], patches[3], patches[4], patch(
                "app.media_resolvers.pipeline.probe_media",
                side_effect=[no_audio, no_audio, no_audio],
            ), patch("app.media_resolvers.pipeline.validate_final_media") as validate:
                with self.assertRaises(ResolverError) as captured:
                    resolve_external_media(
                        repository=repository,
                        settings=settings_for(directory),  # type: ignore[arg-type]
                        media_job_id=UUID("00000000-0000-0000-0000-000000000654"),
                        source_url="https://www.tiktok.com/@fixture/video/7651218940919598000",
                        platform="tiktok",
                        audience_route="development_test",
                    )
            self.assertEqual(captured.exception.code, "media_has_no_audio")
            self.assertEqual(captured.exception.failure_stage, "candidate_fallback")
            validate.assert_not_called()
            self.assertTrue(all(not path.exists() for path in FakeDownloader.created))


if __name__ == "__main__":
    unittest.main()
