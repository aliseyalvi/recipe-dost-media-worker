from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from pathlib import Path
from uuid import UUID

from fastapi import BackgroundTasks

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.main as main
from app.schemas import MediaJobRecord, ProcessJobRequest


JOB_ID = UUID("00000000-0000-0000-0000-000000000001")
USER_ID = UUID("00000000-0000-0000-0000-000000000002")


class FakeRepository:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def get_job(self, media_job_id: UUID) -> MediaJobRecord | None:
        if media_job_id != JOB_ID:
            return None
        return MediaJobRecord(
            id=JOB_ID,
            user_id=USER_ID,
            recipe_item_id=None,
            source_url="https://example.invalid/video",
            platform="tiktok",
            status="queued",
            progress=0,
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
                "media_job_id": str(media_job_id),
                "event_type": event_type,
                "message": message,
                "metadata": metadata or {},
            }
        )


async def main_check() -> None:
    repository = FakeRepository()
    background_tasks = BackgroundTasks()
    wake_calls = 0

    def fake_create_repository(_settings: object) -> FakeRepository:
        return repository

    def fake_ensure_queue_worker_started(_settings: object) -> None:
        nonlocal wake_calls
        wake_calls += 1

    original_create_repository = main.create_repository
    original_ensure_queue_worker_started = main.ensure_queue_worker_started
    try:
        main.create_repository = fake_create_repository  # type: ignore[assignment]
        main.ensure_queue_worker_started = fake_ensure_queue_worker_started  # type: ignore[assignment]
        response = await main.process_job(
            ProcessJobRequest(media_job_id=JOB_ID),
            background_tasks,
            settings=SimpleNamespace(
                app_env="production",
                worker_execution_mode="queue",
            ),
        )
    finally:
        main.create_repository = original_create_repository  # type: ignore[assignment]
        main.ensure_queue_worker_started = original_ensure_queue_worker_started  # type: ignore[assignment]

    assert response.status_code == 202
    body = json.loads(response.body)
    assert body["accepted"] is True
    assert body["message"] == "Job accepted; queue worker wake requested."
    assert wake_calls == 1
    assert len(background_tasks.tasks) == 0
    assert [event["event_type"] for event in repository.events] == [
        "process_job_queue_wake_requested"
    ]
    print("process-job queue mode compatibility check passed")


if __name__ == "__main__":
    asyncio.run(main_check())
