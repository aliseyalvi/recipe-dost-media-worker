from __future__ import annotations

import tempfile
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

from app.media_resolvers.contracts import MediaCandidate, ResolverError
from app.media_resolvers.media_fetch import (
    DownloadedMedia,
    SecureMediaDownloader,
    host_allowed,
    is_public_ip,
    supported_media_signature,
    validate_media_url,
    connected_peer_ip,
)
from app.worker_runtime.cleanup import cleanup_stale_media


class ExternalMediaSecurityTests(unittest.TestCase):
    def test_throughput_probe_reorders_url_alternatives_without_exposing_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            downloader = SecureMediaDownloader(
                temp_root=directory,
                allowed_host_suffixes=("provider.invalid",),
                byte_limit=1024,
                connect_timeout_seconds=1,
                read_timeout_seconds=1,
                total_timeout_seconds=10,
                max_redirects=1,
            )
            job_dir = downloader.create_job_directory("throughput")
            path = job_dir / "video.mp4"
            path.write_bytes(b"\x00\x00\x00\x18ftypmp42")
            candidate = MediaCandidate(
                media_type="video",
                _urls=(
                    "https://provider.invalid/slow",
                    "https://provider.invalid/fast",
                    "https://provider.invalid/middle",
                ),
                size_bytes=12,
            )
            downloaded = DownloadedMedia(path, 12, "video/mp4")
            with patch.object(
                downloader,
                "_probe_url_throughput",
                side_effect=[1.0, 3.0, 2.0],
            ) as probe, patch.object(
                downloader,
                "_download_url",
                return_value=downloaded,
            ) as download:
                result = downloader.download_candidate(
                    candidate,
                    job_directory=job_dir,
                    filename="video.mp4",
                    probe_alternatives=True,
                )
            self.assertEqual(result, downloaded)
            self.assertEqual(probe.call_count, 3)
            self.assertEqual(
                download.call_args.args[0],
                "https://provider.invalid/fast",
            )

    def test_known_provider_size_stops_chunked_stream_without_waiting_for_eof(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.headers = {"Content-Type": "video/mp4"}

        def chunks(*_args: object, **_kwargs: object):
            yield b"\x00\x00\x00\x18ftypmp42"
            raise AssertionError("the downloader read beyond the bounded candidate size")

        response.iter_content.side_effect = chunks
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with tempfile.TemporaryDirectory() as directory:
            downloader = SecureMediaDownloader(
                temp_root=directory,
                allowed_host_suffixes=("provider.invalid",),
                byte_limit=1024,
                connect_timeout_seconds=1,
                read_timeout_seconds=1,
                total_timeout_seconds=10,
                max_redirects=1,
            )
            downloader._session = MagicMock()
            downloader._session.get.return_value = response
            job_dir = downloader.create_job_directory("known-size")
            candidate = MediaCandidate(
                media_type="video",
                _urls=("https://provider.invalid/media",),
                size_bytes=12,
            )
            with patch(
                "app.media_resolvers.media_fetch.resolve_public_ips",
                return_value={"8.8.8.8"},
            ), patch(
                "app.media_resolvers.media_fetch.connected_peer_ip",
                return_value="8.8.8.8",
            ):
                result = downloader.download_candidate(
                    candidate,
                    job_directory=job_dir,
                    filename="video.mp4",
                )
            self.assertEqual(result.size_bytes, 12)
            self.assertEqual(
                downloader._session.get.call_args.kwargs["headers"]["Range"],
                "bytes=0-11",
            )

    def test_candidate_deadline_bounds_all_url_alternatives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            downloader = SecureMediaDownloader(
                temp_root=directory,
                allowed_host_suffixes=("provider.invalid",),
                byte_limit=1024,
                connect_timeout_seconds=1,
                read_timeout_seconds=1,
                total_timeout_seconds=10,
                max_redirects=1,
            )
            job_dir = downloader.create_job_directory("deadline")
            candidate = MediaCandidate(
                media_type="video",
                _urls=(
                    "https://provider.invalid/first",
                    "https://provider.invalid/second",
                ),
            )
            with patch.object(downloader, "_download_url") as download:
                with self.assertRaises(ResolverError) as captured:
                    downloader.download_candidate(
                        candidate,
                        job_directory=job_dir,
                        filename="video.mp4",
                        deadline_monotonic=time.monotonic() - 1,
                    )
            download.assert_not_called()
            self.assertEqual(captured.exception.code, "media_download_failed")

    def test_stream_read_failure_is_normalized_and_partial_file_removed(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.headers = {"Content-Type": "video/mp4"}
        response.iter_content.side_effect = requests.ConnectionError("safe fixture")
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with tempfile.TemporaryDirectory() as directory:
            downloader = SecureMediaDownloader(
                temp_root=directory,
                allowed_host_suffixes=("provider.invalid",),
                byte_limit=1024,
                connect_timeout_seconds=1,
                read_timeout_seconds=1,
                total_timeout_seconds=10,
                max_redirects=1,
            )
            downloader._session = MagicMock()
            downloader._session.get.return_value = response
            job_dir = downloader.create_job_directory("stream-failure")
            candidate = MediaCandidate(
                media_type="video",
                _urls=("https://provider.invalid/media",),
            )
            with patch(
                "app.media_resolvers.media_fetch.resolve_public_ips",
                return_value={"8.8.8.8"},
            ), patch(
                "app.media_resolvers.media_fetch.connected_peer_ip",
                return_value="8.8.8.8",
            ):
                with self.assertRaises(ResolverError) as captured:
                    downloader.download_candidate(
                        candidate,
                        job_directory=job_dir,
                        filename="partial.mp4",
                    )
            self.assertEqual(captured.exception.code, "media_download_failed")
            self.assertFalse((job_dir / "partial.mp4").exists())

    def test_peer_ip_supports_nested_urllib3_socket_shape(self) -> None:
        socket_value = MagicMock()
        socket_value.getpeername.return_value = ("8.8.8.8", 443)
        response = MagicMock()
        response.raw._connection = None
        response.raw.connection = None
        response.raw._fp.fp.raw._sock = socket_value
        self.assertEqual(connected_peer_ip(response), "8.8.8.8")

    def test_https_host_policy_and_private_ips(self) -> None:
        parsed = validate_media_url(
            "https://cdn.provider.invalid/media?id=opaque",
            ("provider.invalid",),
        )
        self.assertEqual(parsed.hostname, "cdn.provider.invalid")
        for value in ("http://cdn.provider.invalid/media", "https://127.0.0.1/media"):
            with self.assertRaises(ResolverError):
                validate_media_url(value, ("provider.invalid",))
        self.assertTrue(host_allowed("a.provider.invalid", ("provider.invalid",)))
        self.assertFalse(host_allowed("provider.invalid.attacker.test", ("provider.invalid",)))
        self.assertFalse(is_public_ip("127.0.0.1"))
        self.assertFalse(is_public_ip("169.254.169.254"))
        self.assertTrue(is_public_ip("8.8.8.8"))

    def test_signature_rejects_html(self) -> None:
        self.assertTrue(supported_media_signature(b"\x00\x00\x00\x18ftypmp42more"))
        self.assertFalse(supported_media_signature(b"<!doctype html>"))

    def test_candidate_repr_and_error_hide_url(self) -> None:
        candidate = MediaCandidate(
            media_type="video",
            _urls=("https://provider.invalid/media?token=secret",),
            container="mp4",
        )
        self.assertNotIn("token=secret", repr(candidate))
        self.assertNotIn("token=secret", str(ResolverError("media_download_failed", "safe")))

    def test_oversized_stream_removes_partial_file(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.headers = {"Content-Type": "video/mp4"}
        response.iter_content.return_value = [b"\x00\x00\x00\x18ftypmp42", b"x" * 64]
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        session = MagicMock()
        session.get.return_value = response
        with tempfile.TemporaryDirectory() as directory:
            downloader = SecureMediaDownloader(
                temp_root=directory,
                allowed_host_suffixes=("provider.invalid",),
                byte_limit=20,
                connect_timeout_seconds=1,
                read_timeout_seconds=1,
                total_timeout_seconds=10,
                max_redirects=1,
            )
            downloader._session = session
            job_dir = downloader.create_job_directory("job")
            candidate = MediaCandidate(
                media_type="video",
                _urls=("https://provider.invalid/media",),
            )
            with patch(
                "app.media_resolvers.media_fetch.resolve_public_ips",
                return_value={"8.8.8.8"},
            ), patch(
                "app.media_resolvers.media_fetch.connected_peer_ip",
                return_value="8.8.8.8",
            ):
                with self.assertRaises(ResolverError) as captured:
                    downloader.download_candidate(
                        candidate,
                        job_directory=job_dir,
                        filename="partial.mp4",
                    )
            self.assertEqual(captured.exception.code, "media_size_exceeded")
            self.assertFalse((Path(job_dir) / "partial.mp4").exists())
            self.assertEqual(
                session.get.call_args.kwargs["headers"]["Range"],
                "bytes=0-19",
            )

    def test_candidate_url_alternatives_are_tried_in_order_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            downloader = SecureMediaDownloader(
                temp_root=directory,
                allowed_host_suffixes=("provider.invalid",),
                byte_limit=1024,
                connect_timeout_seconds=1,
                read_timeout_seconds=1,
                total_timeout_seconds=10,
                max_redirects=1,
            )
            job_dir = downloader.create_job_directory("job")
            output = job_dir / "video.mp4"
            output.write_bytes(b"fixture")
            candidate = MediaCandidate(
                media_type="video",
                _urls=(
                    "https://provider.invalid/tiktok/download?file=first",
                    "https://provider.invalid/tiktok/download?file=second",
                ),
            )
            first_failure = ResolverError(
                "media_download_failed",
                "Resolved media could not be downloaded.",
            )
            success = DownloadedMedia(output, 7, "video/mp4")
            with patch.object(
                downloader,
                "_download_url",
                side_effect=[first_failure, success],
            ) as download:
                result = downloader.download_candidate(
                    candidate,
                    job_directory=job_dir,
                    filename="video.mp4",
                )
            self.assertEqual(result, success)
            self.assertEqual(download.call_count, 2)

    def test_startup_sweep_is_limited_to_stale_external_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = root / "external-stale"
            fresh = root / "external-fresh"
            unrelated = root / "user-data"
            for path in (stale, fresh, unrelated):
                path.mkdir()
            old = time.time() - 1000
            os.utime(stale, (old, old))
            removed = cleanup_stale_media(
                SimpleNamespace(
                    temp_media_dir=directory,
                    max_job_runtime_seconds=180,
                )
            )
            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())

    def test_startup_cleanup_is_safe_for_missing_root_and_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            missing = root / "missing"
            settings = SimpleNamespace(
                temp_media_dir=str(missing),
                max_job_runtime_seconds=180,
            )
            self.assertEqual(cleanup_stale_media(settings), 0)

            target = Path(outside) / "preserved"
            target.mkdir()
            link = root / "external-link"
            link.symlink_to(target, target_is_directory=True)
            settings.temp_media_dir = directory

            self.assertEqual(cleanup_stale_media(settings), 0)
            self.assertTrue(link.is_symlink())
            self.assertTrue(target.is_dir())

    def test_legacy_cleanup_import_reexports_active_implementation(self) -> None:
        from app.pipeline import recovery

        self.assertIs(recovery.cleanup_stale_media, cleanup_stale_media)


if __name__ == "__main__":
    unittest.main()
