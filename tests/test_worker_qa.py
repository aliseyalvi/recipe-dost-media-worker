from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.qa.replay import (
    OfflineRepository,
    make_job,
    run_batch,
    run_case,
)
from app.quota_finalization import (
    ensure_quota_reservation_event,
    finalize_quota_transition,
)
from app.supabase_client import MediaJobRepository


class WorkerReplayTests(unittest.TestCase):
    def test_primary_repair_and_budget_paths_are_real(self) -> None:
        repaired = run_case("primary_malformed_repaired")
        self.assertEqual(repaired["terminal_status"], "completed")
        self.assertEqual(
            repaired["call_counts"],
            [
                {
                    "purpose": "analysis",
                    "model": "gemini-3.1-flash-lite",
                    "count": 1,
                },
                {
                    "purpose": "repair",
                    "model": "gemini-3.1-flash-lite",
                    "count": 1,
                },
            ],
        )
        exhausted = run_case("call_budget_exhaustion")
        self.assertEqual(
            exhausted["normalized_error_category"],
            "gemini_call_budget_exhausted",
        )
        self.assertNotIn(
            "model_fallback",
            [item["purpose"] for item in exhausted["call_counts"]],
        )

    def test_mixed_batch_is_fifo_serial_and_terminal(self) -> None:
        names = [
            "tiktok_success_1",
            "tiktok_success_2",
            "instagram_progressive_success",
            "instagram_split_av_success",
            "deliberate_gemini_parse_failure",
            "provider_failure",
            "private_metadata_failure",
        ]
        report = run_batch(names)
        self.assertEqual(report["fifo_order"], names)
        self.assertTrue(report["one_active_job"])
        self.assertEqual(report["terminal_count"], len(names))
        self.assertEqual(report["active_reservation_count"], 0)
        self.assertEqual(report["successful_count"], 5)
        self.assertEqual(report["success_ratio"], 0.7143)
        self.assertFalse(report["queue_starvation"])
        self.assertTrue(all(case["cleanup_success"] for case in report["cases"]))

    def test_private_metadata_failure_is_nonterminal_and_safe(self) -> None:
        result = run_case("private_metadata_failure")
        self.assertEqual(result["terminal_status"], "completed")
        self.assertEqual(result["success_committed_count"], 1)

    def test_private_metadata_none_is_mapped_to_non_null_json(self) -> None:
        client = MagicMock()
        repository = MediaJobRepository(client)
        job = make_job(93, "tiktok")
        repository.upsert_private_metadata(
            job.id,
            platform="tiktok",
            resolver="fixture",
            cdn_url=None,
            raw_metadata=None,
        )
        values = client.table.return_value.upsert.call_args.args[0]
        self.assertEqual(values["raw_metadata"], {})


class QuotaFinalizationTests(unittest.TestCase):
    def test_reservation_and_failure_finalization_are_idempotent(self) -> None:
        repository = OfflineRepository()
        job = make_job(91, "tiktok")
        repository.register(job)
        ensure_quota_reservation_event(repository, job)
        ensure_quota_reservation_event(repository, job)
        self.assertEqual(
            sum(
                event["event_type"] == "ai_quota_reservation_created"
                for event in repository.events
            ),
            1,
        )
        failed = repository.update_job(job.id, status="failed", progress=100)
        finalize_quota_transition(repository, failed)
        finalize_quota_transition(repository, failed)
        self.assertEqual(
            sum(
                event["event_type"] == "ai_quota_reservation_released"
                for event in repository.events
            ),
            1,
        )
        self.assertEqual(
            sum(
                event["event_type"] == "ai_quota_finalization_replayed"
                for event in repository.events
            ),
            1,
        )
        counts = repository.get_quota_counts(user_id=job.user_id, plan="premium")
        self.assertEqual(counts["active_reservation_count"], 0)
        self.assertEqual(counts["successful_count"], 0)

    def test_success_commits_once_and_replay_does_not_double_charge(self) -> None:
        repository = OfflineRepository()
        job = make_job(92, "instagram")
        repository.register(job)
        ensure_quota_reservation_event(repository, job)
        completed = repository.update_job(
            job.id,
            status="completed",
            progress=100,
            result_recipe_card_id=job.id,
            ai_provider="gemini",
        )
        finalize_quota_transition(repository, completed)
        finalize_quota_transition(repository, completed)
        counts = repository.get_quota_counts(user_id=job.user_id, plan="premium")
        self.assertEqual(counts["active_reservation_count"], 0)
        self.assertEqual(counts["successful_count"], 1)
        self.assertEqual(
            sum(
                event["event_type"] == "ai_quota_success_committed"
                for event in repository.events
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()
