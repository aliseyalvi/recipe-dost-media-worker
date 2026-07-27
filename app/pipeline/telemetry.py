from __future__ import annotations

from time import perf_counter
from uuid import UUID

from app.config import Settings
from app.pipeline.resources import (
    AdmissionSnapshot,
    byte_bucket,
    latency_bucket,
    queue_depth_bucket,
)

PIPELINE_EVENTS = {
    "worker_pipeline_started",
    "worker_stage_enqueued",
    "worker_stage_started",
    "worker_stage_completed",
    "worker_stage_failed",
    "worker_stage_backpressure",
    "worker_stage_recovered",
    "worker_pipeline_shutdown",
}


def emit_stage_event(
    repository: object,
    job_id: UUID,
    event_type: str,
    *,
    stage: str,
    settings: Settings,
    queue_depth: int = 0,
    active_count: int = 0,
    configured_concurrency: int = 1,
    started_at: float | None = None,
    recovery_category: str | None = None,
    lease_state: str = "claim_without_lease",
    snapshot: AdmissionSnapshot | None = None,
) -> None:
    if event_type not in PIPELINE_EVENTS:
        raise ValueError("unsupported pipeline event")
    metadata: dict[str, object] = {
        "stage": stage,
        "pipeline_mode": settings.worker_pipeline_mode,
        "configured_concurrency": configured_concurrency,
        "queue_depth_bucket": queue_depth_bucket(queue_depth),
        "active_stage_count": max(0, active_count),
        "lease_state_category": lease_state,
    }
    if started_at is not None:
        metadata["latency_bucket"] = latency_bucket(
            max(0, int((perf_counter() - started_at) * 1000))
        )
    if recovery_category:
        metadata["retry_recovery_category"] = recovery_category
    if snapshot is not None:
        metadata["rss_bucket"] = byte_bucket(snapshot.rss_bytes)
        metadata["temp_byte_bucket"] = byte_bucket(snapshot.temp_bytes)
    repository.insert_event(
        job_id,
        event_type=event_type,
        message=event_message(event_type),
        metadata=metadata,
    )


def event_message(event_type: str) -> str:
    return {
        "worker_pipeline_started": "Worker pipeline started.",
        "worker_stage_enqueued": "Worker stage enqueued.",
        "worker_stage_started": "Worker stage started.",
        "worker_stage_completed": "Worker stage completed.",
        "worker_stage_failed": "Worker stage failed.",
        "worker_stage_backpressure": "Worker stage admission delayed.",
        "worker_stage_recovered": "Worker stage recovered.",
        "worker_pipeline_shutdown": "Worker pipeline shutdown.",
    }[event_type]

