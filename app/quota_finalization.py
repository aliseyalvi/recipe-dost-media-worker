from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.schemas import MediaJobRecord

ACTIVE_AI_JOB_STATUSES = (
    "queued",
    "processing",
    "analyzing",
    "pending",
    "running",
    "resolving",
    "downloading",
    "validating",
    "thumbnailing",
    "uploading_to_ai",
    "saving_result",
)


class QuotaRepository(Protocol):
    def get_ai_access_context(self, media_job_id: UUID) -> dict[str, object] | None: ...

    def has_event(self, media_job_id: UUID, event_type: str) -> bool: ...

    def get_quota_counts(
        self,
        *,
        user_id: UUID,
        plan: str,
    ) -> dict[str, int]: ...

    def insert_event(
        self,
        media_job_id: UUID,
        *,
        event_type: str,
        message: str,
        metadata: dict[str, object] | None = None,
    ) -> None: ...


def ensure_quota_reservation_event(
    repository: QuotaRepository,
    job: MediaJobRecord,
) -> None:
    if repository.has_event(job.id, "ai_quota_reservation_created") is True:
        return
    plan, reason = quota_identity(repository, job.id)
    counts = quota_counts(repository, job, plan)
    repository.insert_event(
        job.id,
        event_type="ai_quota_reservation_created",
        message="AI quota reservation observed.",
        metadata={
            "plan": plan,
            "reason": reason,
            **counts,
            "quota_count_observed_at": "reservation_after_claim",
            "idempotent_replay": False,
        },
    )


def finalize_quota_transition(
    repository: QuotaRepository,
    job: MediaJobRecord,
) -> None:
    if job.status not in {"completed", "failed", "cancelled"}:
        return
    plan, reason = quota_identity(repository, job.id)
    committed = (
        job.status == "completed"
        and job.result_recipe_card_id is not None
    )
    terminal_event = (
        "ai_quota_success_committed"
        if committed
        else "ai_quota_reservation_released"
    )
    if repository.has_event(job.id, terminal_event) is True:
        repository.insert_event(
            job.id,
            event_type="ai_quota_finalization_replayed",
            message="AI quota finalization replayed idempotently.",
            metadata={
                "plan": plan,
                "reason": reason,
                **quota_counts(repository, job, plan),
                "quota_count_observed_at": "terminal_replay",
                "idempotent_replay": True,
            },
        )
        return
    repository.insert_event(
        job.id,
        event_type=terminal_event,
        message=(
            "AI quota success committed."
            if committed
            else "AI quota reservation released."
        ),
        metadata={
            "plan": plan,
            "reason": reason,
            **quota_counts(repository, job, plan),
            "quota_count_observed_at": "terminal_after_status_update",
            "idempotent_replay": False,
        },
    )


def quota_identity(
    repository: QuotaRepository,
    media_job_id: UUID,
) -> tuple[str, str]:
    context = repository.get_ai_access_context(media_job_id)
    if not isinstance(context, dict):
        return "unknown", "unknown"
    plan = str(context.get("plan") or "").strip().lower()
    if plan not in {"free", "premium"}:
        plan = "unknown"
    reason = str(context.get("reason") or "").strip().lower()
    if reason not in {
        "free_credit_available",
        "premium_active",
        "admin_override",
        "tester_override",
        "limit_reached",
    }:
        reason = "unknown"
    return plan, reason


def quota_counts(
    repository: QuotaRepository,
    job: MediaJobRecord,
    plan: str,
) -> dict[str, int]:
    try:
        counts = repository.get_quota_counts(user_id=job.user_id, plan=plan)
    except Exception:
        return {}
    if not isinstance(counts, dict):
        return {}
    result: dict[str, int] = {}
    active = counts.get("active_reservation_count")
    successful = counts.get("successful_count")
    if isinstance(active, int) and active >= 0:
        result["active_reservation_count"] = active
    if isinstance(successful, int) and successful >= 0:
        result["successful_count"] = successful
    return result
