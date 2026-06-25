from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from app.schemas import MediaJobData, MediaJobRecord
from app.supabase_client import MediaJobRepository

logger = logging.getLogger("recipe_dost.worker")

SKELETON_ERROR_CODE = "worker_skeleton_no_media_provider"
SKELETON_ERROR_MESSAGE = (
    "Media worker is connected. Media resolver is not implemented yet."
)

IDEMPOTENT_STATUSES = {
    "resolving",
    "downloading",
    "validating",
    "thumbnailing",
    "uploading_to_ai",
    "analyzing",
    "saving_result",
    "completed",
    "needs_caption",
}


class JobNotFoundError(Exception):
    pass


class WorkerProcessingError(Exception):
    pass


@dataclass(frozen=True)
class Stage:
    status: str
    progress: int
    message: str


STAGES = (
    Stage("resolving", 10, "Media worker started resolving the job."),
    Stage("validating", 30, "Media worker started validating the job."),
    Stage("needs_caption", 100, SKELETON_ERROR_MESSAGE),
)


class JobRunner:
    def __init__(self, repository: MediaJobRepository) -> None:
        self._repository = repository

    def run(self, media_job_id: UUID) -> MediaJobData:
        try:
            job = self._repository.get_job(media_job_id)
            if job is None:
                raise JobNotFoundError

            if job.status in IDEMPOTENT_STATUSES:
                return self._to_response(job)

            for stage in STAGES:
                job = self._apply_stage(media_job_id, stage)

            return self._to_response(job)
        except JobNotFoundError:
            raise
        except Exception as exc:
            logger.error(
                "worker_job_failed",
                extra={
                    "media_job_id": str(media_job_id),
                    "exception_type": type(exc).__name__,
                },
            )
            self._record_failure(media_job_id, type(exc).__name__)
            raise WorkerProcessingError from exc

    def _apply_stage(self, media_job_id: UUID, stage: Stage) -> MediaJobRecord:
        is_terminal_stage = stage.status == "needs_caption"
        job = self._repository.update_job(
            media_job_id,
            status=stage.status,
            progress=stage.progress,
            error_code=SKELETON_ERROR_CODE if is_terminal_stage else None,
            error_message=SKELETON_ERROR_MESSAGE if is_terminal_stage else None,
        )
        self._repository.insert_event(
            media_job_id,
            event_type=stage.status,
            message=stage.message,
            metadata={"status": stage.status, "progress": stage.progress},
        )
        return job

    def _record_failure(self, media_job_id: UUID, exception_type: str) -> None:
        try:
            self._repository.update_job(
                media_job_id,
                status="failed",
                error_code="worker_error",
                error_message="Media worker failed.",
            )
        except Exception as recovery_exc:
            self._log_recovery_failure(
                media_job_id,
                "worker_failure_status_update_failed",
                recovery_exc,
            )

        try:
            self._repository.insert_event(
                media_job_id,
                event_type="worker_error",
                message="Media worker failed.",
                metadata={"exception_type": exception_type},
            )
        except Exception as recovery_exc:
            self._log_recovery_failure(
                media_job_id,
                "worker_failure_event_insert_failed",
                recovery_exc,
            )

    @staticmethod
    def _log_recovery_failure(
        media_job_id: UUID,
        event: str,
        exception: Exception,
    ) -> None:
        logger.error(
            event,
            extra={
                "media_job_id": str(media_job_id),
                "exception_type": type(exception).__name__,
            },
        )

    @staticmethod
    def _to_response(job: MediaJobRecord) -> MediaJobData:
        return MediaJobData(
            media_job_id=job.id,
            status=job.status,
            progress=job.progress,
        )
