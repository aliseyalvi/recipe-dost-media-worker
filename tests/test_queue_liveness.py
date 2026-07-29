from __future__ import annotations

import asyncio
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import UUID

from app import main
from app.queue_worker import (
    QueueLivenessConfig,
    drain_queued_jobs,
    queue_liveness_config,
    recover_stale_queue_jobs,
    release_stale_quota_reservation_safely,
    reset_queue_runtime,
    run_queue_loop,
    signal_queue_wake,
)
from app.schemas import MediaJobRecord
from app.supabase_client import MediaJobRepository


def job(number: int, status: str = "resolving") -> MediaJobRecord:
    return MediaJobRecord(
        id=UUID(f"00000000-0000-0000-0000-{number:012d}"),
        user_id=UUID("00000000-0000-0000-0000-000000000999"),
        recipe_item_id=UUID(f"10000000-0000-0000-0000-{number:012d}"),
        source_url=f"https://source.invalid/{number}",
        platform="youtube",
        status=status,
        progress=20 if status == "resolving" else 0,
    )


LIVENESS = QueueLivenessConfig(
    idle_poll_seconds=1,
    stale_after_seconds=600,
    stale_max_recovery_attempts=2,
    stale_scan_interval_seconds=60,
)


class MemoryQueueRepository:
    def __init__(self, queued: list[MediaJobRecord] | None = None) -> None:
        self.queued = list(queued or [])
        self.claim_calls = 0
        self.heartbeat_calls: list[UUID] = []
        self.release_calls: list[UUID] = []
        self.events: list[dict[str, object]] = []
        self.recoveries: list[dict[str, object]] = []

    def claim_oldest_queued_job(self) -> MediaJobRecord | None:
        self.claim_calls += 1
        return self.queued.pop(0) if self.queued else None

    def heartbeat_job(self, media_job_id: UUID) -> None:
        self.heartbeat_calls.append(media_job_id)

    def release_job_lease(self, media_job_id: UUID) -> None:
        self.release_calls.append(media_job_id)

    def recover_stale_jobs(
        self,
        *,
        stale_after_seconds: int,
        max_recovery_attempts: int,
    ) -> list[dict[str, object]]:
        self.last_recovery_args = (stale_after_seconds, max_recovery_attempts)
        rows = list(self.recoveries)
        self.recoveries.clear()
        return rows

    def has_event(self, media_job_id: UUID, event_type: str) -> bool:
        return any(
            event["media_job_id"] == media_job_id
            and event["event_type"] == event_type
            for event in self.events
        )

    def insert_event(
        self,
        media_job_id: UUID,
        *,
        event_type: str,
        message: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.events.append(
            {
                "media_job_id": media_job_id,
                "event_type": event_type,
                "message": message,
                "metadata": metadata or {},
            }
        )

    def add_event(
        self,
        media_job_id: UUID,
        *,
        event_type: str,
        message: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.insert_event(
            media_job_id,
            event_type=event_type,
            message=message,
            metadata=metadata,
        )


class QueueDrainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        reset_queue_runtime()

    async def asyncTearDown(self) -> None:
        reset_queue_runtime()
        main.queue_task = None

    async def test_startup_drain_processes_all_preexisting_jobs_fifo(self) -> None:
        repository = MemoryQueueRepository([job(1), job(2), job(3)])
        processed: list[UUID] = []
        with patch(
            "app.queue_worker.create_repository", return_value=repository
        ), patch(
            "app.queue_worker.process_queue_job",
            side_effect=lambda _repo, _settings, claimed, **_kwargs: processed.append(
                claimed.id
            ),
        ):
            await drain_queued_jobs(MagicMock(), LIVENESS)
        self.assertEqual(processed, [job(1).id, job(2).id, job(3).id])
        self.assertEqual(repository.claim_calls, 4)

    async def test_job_queued_while_busy_is_claimed_without_another_wake(self) -> None:
        repository = MemoryQueueRepository([job(1)])
        processed: list[UUID] = []

        def process(_repo, _settings, claimed, **_kwargs) -> None:
            processed.append(claimed.id)
            if claimed.id == job(1).id:
                repository.queued.append(job(2))

        with patch(
            "app.queue_worker.create_repository", return_value=repository
        ), patch("app.queue_worker.process_queue_job", side_effect=process):
            await drain_queued_jobs(MagicMock(), LIVENESS)
        self.assertEqual(processed, [job(1).id, job(2).id])

    async def test_jobs_queued_while_busy_remain_a_b_c(self) -> None:
        repository = MemoryQueueRepository([job(1)])
        processed: list[UUID] = []

        def process(_repo, _settings, claimed, **_kwargs) -> None:
            processed.append(claimed.id)
            if claimed.id == job(1).id:
                repository.queued.extend([job(2), job(3)])

        with patch(
            "app.queue_worker.create_repository", return_value=repository
        ), patch("app.queue_worker.process_queue_job", side_effect=process):
            await drain_queued_jobs(MagicMock(), LIVENESS)
        self.assertEqual(processed, [job(1).id, job(2).id, job(3).id])

    async def test_missing_wake_is_recovered_by_idle_poll(self) -> None:
        repository = MemoryQueueRepository()
        processed = asyncio.Event()
        loop = asyncio.get_running_loop()
        settings = MagicMock()
        with patch("app.queue_worker.get_settings", return_value=settings), patch(
            "app.queue_worker.queue_liveness_config", return_value=LIVENESS
        ), patch(
            "app.queue_worker.create_repository", return_value=repository
        ), patch(
            "app.queue_worker.process_queue_job",
            side_effect=lambda *_args, **_kwargs: loop.call_soon_threadsafe(
                processed.set
            ),
        ):
            task = asyncio.create_task(run_queue_loop(MagicMock()))
            try:
                while repository.claim_calls < 1:
                    await asyncio.sleep(0)
                repository.queued.append(job(2))
                await asyncio.wait_for(processed.wait(), timeout=1.5)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertEqual(repository.release_calls, [job(2).id])

    async def test_empty_queue_waits_instead_of_busy_looping(self) -> None:
        repository = MemoryQueueRepository()
        with patch(
            "app.queue_worker.get_settings", return_value=MagicMock()
        ), patch(
            "app.queue_worker.queue_liveness_config", return_value=LIVENESS
        ), patch(
            "app.queue_worker.create_repository", return_value=repository
        ):
            task = asyncio.create_task(run_queue_loop(MagicMock()))
            await asyncio.sleep(0.1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(repository.claim_calls, 1)

    async def test_processing_and_gemini_provider_section_stay_sequential(self) -> None:
        repository = MemoryQueueRepository([job(1), job(2), job(3)])
        active = 0
        maximum_active = 0
        order: list[UUID] = []
        state_lock = threading.Lock()

        def process(_repo, _settings, claimed, **_kwargs) -> None:
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
                order.append(claimed.id)
            time.sleep(0.02)
            with state_lock:
                active -= 1

        with patch(
            "app.queue_worker.create_repository", return_value=repository
        ), patch("app.queue_worker.process_queue_job", side_effect=process):
            await drain_queued_jobs(MagicMock(), LIVENESS)
        self.assertEqual(maximum_active, 1)
        self.assertEqual(order, [job(1).id, job(2).id, job(3).id])


class WakeAndSettingsTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_queue_runtime()
        main.queue_task = None

    def test_wake_while_busy_signals_without_starting_second_loop(self) -> None:
        settings = MagicMock(worker_execution_mode="queue")
        active_task = MagicMock()
        active_task.done.return_value = False
        main.queue_task = active_task
        with patch.object(main, "signal_queue_wake") as signal, patch.object(
            main.asyncio, "create_task"
        ) as create:
            main.ensure_queue_worker_started(settings)
        signal.assert_called_once_with()
        create.assert_not_called()

    def test_multiple_wakes_are_coalesced_by_one_event(self) -> None:
        signal_queue_wake()
        signal_queue_wake()
        signal_queue_wake()
        from app import queue_worker

        self.assertIsNotNone(queue_worker._queue_wake_event)
        self.assertTrue(queue_worker._queue_wake_event.is_set())

    def test_queue_settings_defaults_and_bounds(self) -> None:
        settings = MagicMock(queue_empty_sleep_seconds=5)
        with patch.dict(
            os.environ,
            {
                "QUEUE_STALE_AFTER_SECONDS": "600",
                "QUEUE_STALE_MAX_RECOVERY_ATTEMPTS": "2",
                "QUEUE_STALE_SCAN_INTERVAL_SECONDS": "60",
            },
            clear=False,
        ):
            config = queue_liveness_config(settings)
        self.assertEqual(
            config.safe_config(),
            {
                "queue_idle_poll_seconds": 5,
                "queue_stale_after_seconds": 600,
                "queue_stale_max_recovery_attempts": 2,
                "queue_stale_scan_interval_seconds": 60,
            },
        )
        with patch.dict(os.environ, {"QUEUE_EMPTY_SLEEP_SECONDS": "0"}):
            with self.assertRaisesRegex(ValueError, "between 1 and 60"):
                queue_liveness_config(settings)


class StaleRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MemoryQueueRepository()
        self.stale_job = job(7, status="queued")
        self.payload = {
            **self.stale_job.model_dump(mode="json"),
            "source_metadata": {"title": "preserved"},
        }

    def test_fresh_active_job_is_not_recovered(self) -> None:
        self.assertEqual(recover_stale_queue_jobs(self.repository, LIVENESS), 0)
        self.assertEqual(self.repository.last_recovery_args, (600, 2))
        self.assertEqual(self.repository.events, [])

    def test_stale_job_below_limit_is_requeued_once_and_preserved(self) -> None:
        original_payload = dict(self.payload)
        self.repository.events.append(
            {
                "media_job_id": self.stale_job.id,
                "event_type": "ai_quota_reservation_created",
                "message": "",
                "metadata": {},
            }
        )
        self.repository.recoveries = [
            {
                "job": self.payload,
                "recovery_action": "requeued",
                "processing_attempt_count": 1,
            }
        ]
        self.assertEqual(recover_stale_queue_jobs(self.repository, LIVENESS), 1)
        event_types = [event["event_type"] for event in self.repository.events]
        self.assertEqual(event_types.count("queue_job_stale_requeued"), 1)
        self.assertEqual(event_types.count("ai_quota_reservation_released"), 1)
        self.assertEqual(self.payload, original_payload)

    def test_stale_job_at_limit_fails_with_timeout(self) -> None:
        failed_payload = {
            **self.payload,
            "status": "failed",
            "progress": 100,
            "error_code": "stale_job_timeout",
        }
        self.repository.recoveries = [
            {
                "job": failed_payload,
                "recovery_action": "failed",
                "processing_attempt_count": 2,
            }
        ]
        with patch("app.queue_worker.finalize_quota_transition_safely") as finalize:
            self.assertEqual(recover_stale_queue_jobs(self.repository, LIVENESS), 1)
        finalize.assert_called_once()
        stale_event = next(
            event
            for event in self.repository.events
            if event["event_type"] == "queue_job_stale_failed"
        )
        self.assertEqual(stale_event["metadata"]["error_code"], "stale_job_timeout")

    def test_stale_quota_release_is_idempotent(self) -> None:
        self.repository.events.append(
            {
                "media_job_id": self.stale_job.id,
                "event_type": "ai_quota_reservation_created",
                "message": "",
                "metadata": {},
            }
        )
        release_stale_quota_reservation_safely(
            self.repository, self.stale_job, 1
        )
        release_stale_quota_reservation_safely(
            self.repository, self.stale_job, 1
        )
        self.assertEqual(
            sum(
                event["event_type"] == "ai_quota_reservation_released"
                for event in self.repository.events
            ),
            1,
        )


class QueueRepositoryTests(unittest.TestCase):
    def test_repository_claim_uses_rpc_not_select_then_update(self) -> None:
        claimed = job(8)
        response = SimpleNamespace(data=[claimed.model_dump(mode="json")])
        client = MagicMock()
        client.rpc.return_value.execute.return_value = response
        repository = MediaJobRepository(client)
        self.assertEqual(repository.claim_oldest_queued_job(), claimed)
        client.rpc.assert_called_once_with("claim_oldest_media_job")
        client.table.assert_not_called()

    def test_repository_stale_recovery_uses_authoritative_rpc(self) -> None:
        client = MagicMock()
        client.rpc.return_value.execute.return_value = SimpleNamespace(data=[])
        repository = MediaJobRepository(client)
        self.assertEqual(
            repository.recover_stale_jobs(
                stale_after_seconds=600,
                max_recovery_attempts=2,
            ),
            [],
        )
        client.rpc.assert_called_once_with(
            "recover_stale_media_jobs",
            {
                "p_stale_after_seconds": 600,
                "p_max_recovery_attempts": 2,
            },
        )

    def test_existing_recipe_card_is_reused_instead_of_duplicated(self) -> None:
        existing_id = UUID("20000000-0000-0000-0000-000000000008")
        query = MagicMock()
        for method in ("select", "eq", "order", "limit"):
            getattr(query, method).return_value = query
        query.execute.return_value = SimpleNamespace(data=[{"id": str(existing_id)}])
        client = MagicMock()
        client.table.return_value = query
        repository = MediaJobRepository(client)
        self.assertEqual(
            repository.insert_recipe_card(
                job=job(8),
                result=MagicMock(),
                model="unchanged-model",
            ),
            existing_id,
        )
        query.insert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
