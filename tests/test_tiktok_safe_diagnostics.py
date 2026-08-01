from __future__ import annotations

import json
import logging
import unittest
from unittest.mock import MagicMock
from uuid import UUID

from app.media_resolvers.pipeline import (
    _emit_tiktok_candidate_summary,
    _emit_tiktok_contract_diagnostic,
)
from app.main import JsonFormatter


class TikTokSafeDiagnosticTests(unittest.TestCase):
    def test_contract_and_candidate_events_contain_shape_and_counts_only(self) -> None:
        repository = MagicMock()
        job_id = UUID("00000000-0000-0000-0000-000000000123")
        base = {
            "platform": "tiktok",
            "resolver_adapter": "akng_tiktok",
            "provider_attempt": 1,
            "fallback_used": False,
        }
        _emit_tiktok_contract_diagnostic(
            repository,
            job_id,
            base,
            {
                "provider_status_class": "2xx",
                "json_parsed": True,
                "bitrate_info_count": 5,
                "bitrate_audio_info_count": 3,
            },
        )
        _emit_tiktok_candidate_summary(
            repository,
            job_id,
            base,
            raw_video_candidates=5,
            eligible_video_candidates=2,
            raw_audio_candidates=3,
            eligible_audio_candidates=3,
            rejection_counts={
                "unsupported_codec": 3,
                "unsafe_url": 99,
            },
        )
        self.assertEqual(repository.insert_event.call_count, 2)
        serialized = json.dumps(
            [call.kwargs["metadata"] for call in repository.insert_event.call_args_list]
        )
        self.assertNotIn("https://", serialized)
        self.assertNotIn("source_url", serialized)
        self.assertNotIn("media_url", serialized)
        self.assertNotIn("unsafe_url", serialized)
        self.assertIn("unsupported_codec", serialized)

    def test_render_formatter_retains_only_safe_shape_fields(self) -> None:
        record = logging.LogRecord(
            name="recipe_dost.worker",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="tiktok_provider_contract_inspected",
            args=(),
            exc_info=None,
        )
        record.provider_attempt = 1
        record.provider_status_class = "2xx"
        record.bitrate_info_count = 5
        record.bitrate_audio_info_count = 3
        record.source_url = "https://must-not-appear.invalid/source"
        record.provider_body = {"url": "https://must-not-appear.invalid/media"}
        rendered = JsonFormatter().format(record)
        self.assertIn('"provider_attempt":1', rendered)
        self.assertIn('"bitrate_info_count":5', rendered)
        self.assertNotIn("must-not-appear", rendered)
        self.assertNotIn("provider_body", rendered)


if __name__ == "__main__":
    unittest.main()
