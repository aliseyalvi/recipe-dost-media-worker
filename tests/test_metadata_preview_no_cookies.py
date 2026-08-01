from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.metadata_preview import fetch_ytdlp_preview


class MetadataPreviewNoCookieTests(unittest.TestCase):
    def test_social_preview_never_passes_cookiefile(self) -> None:
        active = Settings(
            _env_file=None,
            WORKER_API_SECRET="test",
            supabase_url="https://example.supabase.co",
            supabase_service_role_key="test",
            media_resolver_mode="legacy",
            external_media_resolver_audience="disabled",
            instagram_resolver_chain=[],
            tiktok_resolver_chain=[],
            enable_ytdlp_cookies=True,
            enable_instagram_cookies=True,
            instagram_cookies_b64="secret-cookie",
        )
        ydl = MagicMock()
        ydl.__enter__.return_value = ydl
        ydl.__exit__.return_value = False
        ydl.extract_info.return_value = {
            "title": "fixture",
            "thumbnail": "https://images.invalid/thumb.jpg",
        }
        with patch("yt_dlp.YoutubeDL", return_value=ydl) as factory:
            preview = fetch_ytdlp_preview(
                "https://instagram.example/post",
                "instagram",
                active,
                resolver="instagram_ytdlp",
            )
        self.assertEqual(preview.title, "fixture")
        self.assertNotIn("cookiefile", factory.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
