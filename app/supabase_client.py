from __future__ import annotations

from typing import Any
from uuid import UUID

from supabase import Client, create_client

from app.config import Settings
from app.schemas import MediaJobRecord


class MediaJobRepository:
    def __init__(self, client: Client) -> None:
        self._client = client

    def get_job(self, media_job_id: UUID) -> MediaJobRecord | None:
        response = (
            self._client.table("media_jobs")
            .select("id,status,progress")
            .eq("id", str(media_job_id))
            .limit(1)
            .execute()
        )
        if not response.data:
            return None
        return MediaJobRecord.model_validate(response.data[0])

    def update_job(
        self,
        media_job_id: UUID,
        *,
        status: str,
        progress: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> MediaJobRecord:
        values: dict[str, Any] = {
            "status": status,
            "error_code": error_code,
            "error_message": error_message,
        }
        if progress is not None:
            values["progress"] = progress

        response = (
            self._client.table("media_jobs")
            .update(values)
            .eq("id", str(media_job_id))
            .select("id,status,progress")
            .execute()
        )
        if not response.data:
            raise RuntimeError("Media job update returned no row.")
        return MediaJobRecord.model_validate(response.data[0])

    def insert_event(
        self,
        media_job_id: UUID,
        *,
        event_type: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._client.table("media_job_events").insert(
            {
                "media_job_id": str(media_job_id),
                "event_type": event_type,
                "message": message,
                "metadata": metadata or {},
            }
        ).execute()


def create_repository(settings: Settings) -> MediaJobRepository:
    client = create_client(
        settings.supabase_url,
        settings.supabase_service_role_key.get_secret_value(),
    )
    return MediaJobRepository(client)
