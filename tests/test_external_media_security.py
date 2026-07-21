from __future__ import annotations

import tempfile
import os
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.media_resolvers.contracts import MediaCandidate, ResolverError
from app.media_resolvers.media_fetch import (
    SecureMediaDownloader,
    host_allowed,
    is_public_ip,
    supported_media_signature,
    sweep_stale_job_directories,
    validate_media_url,
)


class ExternalMediaSecurityTests(unittest.TestCase):
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
            removed = sweep_stale_job_directories(
                directory,
                older_than_seconds=300,
                now=time.time(),
            )
            self.assertEqual(removed, 1)
            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(unrelated.exists())


if __name__ == "__main__":
    unittest.main()
