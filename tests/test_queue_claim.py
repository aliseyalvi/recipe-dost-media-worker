from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
from uuid import UUID

from app.queue_worker import process_next_queued_job
from app.schemas import MediaJobRecord


class QueueClaimTests(unittest.TestCase):
    def test_processor_uses_conditional_claim_before_processing(self) -> None:
        claimed = MediaJobRecord(
            id=UUID("00000000-0000-0000-0000-000000000111"),
            user_id=UUID("00000000-0000-0000-0000-000000000222"),
            recipe_item_id=None,
            source_url="https://source.invalid/video",
            platform="youtube",
            status="resolving",
            progress=20,
        )
        repository = MagicMock()
        repository.claim_oldest_queued_job.return_value = claimed
        with patch("app.queue_worker.get_settings", return_value=MagicMock()), patch(
            "app.queue_worker.create_repository", return_value=repository
        ), patch("app.queue_worker.process_queue_job") as process:
            self.assertTrue(process_next_queued_job())
        repository.claim_oldest_queued_job.assert_called_once_with()
        process.assert_called_once_with(repository, unittest.mock.ANY, claimed, already_claimed=True)

    def test_lost_claim_does_not_process(self) -> None:
        repository = MagicMock()
        repository.claim_oldest_queued_job.return_value = None
        with patch("app.queue_worker.get_settings", return_value=MagicMock()), patch(
            "app.queue_worker.create_repository", return_value=repository
        ), patch("app.queue_worker.process_queue_job") as process:
            self.assertFalse(process_next_queued_job())
        process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
