from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

from app.qa.replay import OfflineRepository, make_job
from app.quota_finalization import (
    ensure_quota_reservation_event,
    finalize_quota_transition,
)
from app.supabase_client import MediaJobRepository
from app.queue_worker import drain_queued_jobs
from tests.test_queue_liveness import LIVENESS, MemoryQueueRepository, job


class RetriedQueueIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_retried_queued_job_is_claimed_without_wake(self) -> None:
        retried = job(31, status="queued")
        repository = MemoryQueueRepository([retried])
        processed: list[UUID] = []

        with patch(
            "app.queue_worker.create_repository",
            return_value=repository,
        ), patch(
            "app.queue_worker.process_queue_job",
            side_effect=lambda _repo, _settings, claimed, **_kwargs: (
                processed.append(claimed.id)
            ),
        ):
            await drain_queued_jobs(MagicMock(), LIVENESS)

        self.assertEqual(processed, [retried.id])
        self.assertEqual(repository.claim_calls, 2)

    async def test_retried_job_stays_in_existing_fifo_drain(self) -> None:
        queued = [job(32, status="queued"), job(33, status="queued")]
        repository = MemoryQueueRepository(queued)
        processed: list[UUID] = []

        with patch(
            "app.queue_worker.create_repository",
            return_value=repository,
        ), patch(
            "app.queue_worker.process_queue_job",
            side_effect=lambda _repo, _settings, claimed, **_kwargs: (
                processed.append(claimed.id)
            ),
        ):
            await drain_queued_jobs(MagicMock(), LIVENESS)

        self.assertEqual(processed, [queued[0].id, queued[1].id])


class RetriedResultSafetyTests(unittest.TestCase):
    def test_existing_recipe_card_is_reused_after_retry(self) -> None:
        existing_id = UUID("20000000-0000-0000-0000-000000000034")
        query = MagicMock()
        for method in ("select", "eq", "order", "limit"):
            getattr(query, method).return_value = query
        query.execute.return_value = SimpleNamespace(data=[{"id": str(existing_id)}])
        client = MagicMock()
        client.table.return_value = query

        repository = MediaJobRepository(client)

        self.assertEqual(
            repository.insert_recipe_card(
                job=job(34),
                result=MagicMock(),
                model="unchanged-model",
            ),
            existing_id,
        )
        query.insert.assert_not_called()

    def test_retry_success_commits_quota_once(self) -> None:
        repository = OfflineRepository()
        original = make_job(35, "youtube")
        repository.register(original)
        ensure_quota_reservation_event(repository, original)

        failed = repository.update_job(original.id, status="failed", progress=100)
        finalize_quota_transition(repository, failed)
        retried = repository.update_job(
            original.id,
            status="queued",
            progress=0,
            error_code=None,
        )
        claimed = repository.update_job(
            retried.id,
            status="resolving",
            progress=20,
        )
        ensure_quota_reservation_event(repository, claimed)
        completed = repository.update_job(
            claimed.id,
            status="completed",
            progress=100,
            result_recipe_card_id=claimed.id,
            ai_provider="gemini",
        )

        finalize_quota_transition(repository, completed)
        finalize_quota_transition(repository, completed)

        self.assertEqual(
            sum(
                event["event_type"] == "ai_quota_success_committed"
                for event in repository.events
            ),
            1,
        )
        self.assertEqual(
            repository.get_quota_counts(
                user_id=completed.user_id,
                plan="premium",
            )["successful_count"],
            1,
        )


if __name__ == "__main__":
    unittest.main()
