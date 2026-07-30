from __future__ import annotations

from typing import Any
from datetime import datetime, timezone
from uuid import UUID

from supabase import Client, create_client

from app.config import Settings
from app.schemas import GeminiRecipeResult, MediaJobRecord

MAX_RECIPE_DURATION_MINUTES = 10_080

MEDIA_JOB_SELECT = (
    "id,user_id,recipe_item_id,source_url,platform,status,progress,"
    "thumbnail_url,result_recipe_card_id,error_code,started_at"
)
class MediaJobRepository:
    def __init__(self, client: Client) -> None:
        self._client = client

    def get_job(self, media_job_id: UUID) -> MediaJobRecord | None:
        response = (
            self._client.table("media_jobs")
            .select(MEDIA_JOB_SELECT)
            .eq("id", str(media_job_id))
            .limit(1)
            .execute()
        )
        if not response.data:
            return None
        return MediaJobRecord.model_validate(response.data[0])

    def get_oldest_queued_job(self) -> MediaJobRecord | None:
        response = (
            self._client.table("media_jobs")
            .select(MEDIA_JOB_SELECT)
            .eq("status", "queued")
            .order("created_at", desc=False)
            .limit(1)
            .execute()
        )
        if not response.data:
            return None
        return MediaJobRecord.model_validate(response.data[0])

    def claim_oldest_queued_job(self) -> MediaJobRecord | None:
        response = self._client.rpc("claim_oldest_media_job").execute()
        if not response.data:
            return None
        row = response.data[0] if isinstance(response.data, list) else response.data
        return MediaJobRecord.model_validate(row)

    def heartbeat_job(self, media_job_id: UUID) -> None:
        now = datetime.now(timezone.utc).isoformat()
        (
            self._client.table("media_jobs")
            .update({"worker_heartbeat_at": now})
            .eq("id", str(media_job_id))
            .in_("status", ["resolving", "downloading", "analyzing"])
            .execute()
        )

    def release_job_lease(self, media_job_id: UUID) -> None:
        (
            self._client.table("media_jobs")
            .update(
                {
                    "worker_claimed_at": None,
                    "worker_heartbeat_at": None,
                }
            )
            .eq("id", str(media_job_id))
            .execute()
        )

    def recover_stale_jobs(
        self,
        *,
        stale_after_seconds: int,
        max_recovery_attempts: int,
    ) -> list[dict[str, Any]]:
        response = self._client.rpc(
            "recover_stale_media_jobs",
            {
                "p_stale_after_seconds": stale_after_seconds,
                "p_max_recovery_attempts": max_recovery_attempts,
            },
        ).execute()
        if not isinstance(response.data, list):
            return []
        return [row for row in response.data if isinstance(row, dict)]

    def get_ai_access_context(self, media_job_id: UUID) -> dict[str, Any] | None:
        response = (
            self._client.table("media_job_events")
            .select("metadata")
            .eq("media_job_id", str(media_job_id))
            .eq("event_type", "media_job_created")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not response.data:
            return None
        metadata = response.data[0].get("metadata")
        if not isinstance(metadata, dict):
            return None
        access = metadata.get("ai_generation_access")
        return access if isinstance(access, dict) else None

    def has_external_resolver_attempt(
        self,
        media_job_id: UUID,
        adapter_id: str,
    ) -> bool:
        response = (
            self._client.table("media_job_events")
            .select("id", count="exact", head=True)
            .eq("media_job_id", str(media_job_id))
            .eq("event_type", "external_resolver_started")
            .contains("metadata", {"resolver_adapter": adapter_id})
            .execute()
        )
        return int(response.count or 0) > 0

    def has_event(self, media_job_id: UUID, event_type: str) -> bool:
        response = (
            self._client.table("media_job_events")
            .select("id", count="exact", head=True)
            .eq("media_job_id", str(media_job_id))
            .eq("event_type", event_type)
            .execute()
        )
        return int(response.count or 0) > 0

    def get_quota_counts(
        self,
        *,
        user_id: UUID,
        plan: str,
    ) -> dict[str, int]:
        active_statuses = [
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
        ]
        active_query = (
            self._client.table("media_jobs")
            .select("id", count="exact", head=True)
            .eq("user_id", str(user_id))
            .in_("status", active_statuses)
        )
        successful_query = (
            self._client.table("media_jobs")
            .select("id", count="exact", head=True)
            .eq("user_id", str(user_id))
            .eq("status", "completed")
            .not_.is_("result_recipe_card_id", "null")
            .not_.is_("ai_provider", "null")
        )
        if plan == "premium":
            now = datetime.now(timezone.utc)
            month_start = now.replace(
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            if month_start.month == 12:
                next_month = month_start.replace(
                    year=month_start.year + 1,
                    month=1,
                )
            else:
                next_month = month_start.replace(month=month_start.month + 1)
            active_query = active_query.gte(
                "created_at", month_start.isoformat()
            ).lt("created_at", next_month.isoformat())
            successful_query = successful_query.gte(
                "completed_at", month_start.isoformat()
            ).lt("completed_at", next_month.isoformat())
        return {
            "active_reservation_count": int(active_query.execute().count or 0),
            "successful_count": int(successful_query.execute().count or 0),
        }

    def update_job(
        self,
        media_job_id: UUID,
        *,
        status: str,
        progress: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        thumbnail_url: str | None = None,
        result_recipe_card_id: UUID | None = None,
        ai_provider: str | None = None,
        ai_model: str | None = None,
        completed_at: str | None = None,
        started_at: str | None = None,
        media_duration_seconds: int | None = None,
        media_size_bytes: int | None = None,
        source_title: str | None = None,
        source_thumbnail_url: str | None = None,
    ) -> MediaJobRecord:
        values: dict[str, Any] = {
            "status": status,
            "error_code": error_code,
            "error_message": error_message,
        }
        if progress is not None:
            values["progress"] = progress
        if thumbnail_url is not None:
            values["thumbnail_url"] = thumbnail_url
        if result_recipe_card_id is not None:
            values["result_recipe_card_id"] = str(result_recipe_card_id)
        if ai_provider is not None:
            values["ai_provider"] = ai_provider
        if ai_model is not None:
            values["ai_model"] = ai_model
        if completed_at is not None:
            values["completed_at"] = completed_at
        if started_at is not None:
            values["started_at"] = started_at
        if media_duration_seconds is not None:
            values["media_duration_seconds"] = media_duration_seconds
        if media_size_bytes is not None:
            values["media_size_bytes"] = media_size_bytes
        if source_title is not None:
            values["source_title"] = source_title
        if source_thumbnail_url is not None:
            values["source_thumbnail_url"] = source_thumbnail_url

        response = (
            self._client.table("media_jobs")
            .update(values)
            .eq("id", str(media_job_id))
            .select(MEDIA_JOB_SELECT)
            .execute()
        )
        if not response.data:
            raise RuntimeError("Media job update returned no row.")
        return MediaJobRecord.model_validate(response.data[0])

    def update_job_metadata_best_effort(
        self,
        media_job_id: UUID,
        values: dict[str, Any],
    ) -> None:
        if not values:
            return
        self._client.table("media_jobs").update(values).eq("id", str(media_job_id)).execute()

    def update_source_metadata(
        self,
        media_job_id: UUID,
        source_metadata: dict[str, Any],
    ) -> None:
        self._client.table("media_jobs").update(
            {"source_metadata": source_metadata}
        ).eq("id", str(media_job_id)).execute()

    def upsert_private_metadata(
        self,
        media_job_id: UUID,
        *,
        platform: str,
        resolver: str,
        cdn_url: str | None,
        raw_metadata: dict[str, Any] | None = None,
    ) -> None:
        values = {
            "media_job_id": str(media_job_id),
            "platform": platform,
            "resolver": resolver,
            "cdn_url": cdn_url,
            "cdn_url_expires_at": None,
            "raw_metadata": raw_metadata if isinstance(raw_metadata, dict) else {},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._client.table("media_job_private_metadata").upsert(
            values,
            on_conflict="media_job_id",
        ).execute()

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

    def add_event(
        self,
        media_job_id: UUID,
        *,
        event_type: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.insert_event(
            media_job_id,
            event_type=event_type,
            message=message,
            metadata=metadata,
        )

    def count_job_gemini_calls(
        self,
        media_job_id: UUID,
        *,
        since_latest_worker_start: bool = True,
    ) -> int:
        since: str | None = None
        if since_latest_worker_start:
            latest = (
                self._client.table("media_job_events")
                .select("created_at")
                .eq("media_job_id", str(media_job_id))
                .eq("event_type", "worker_started")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if latest.data:
                since = latest.data[0]["created_at"]
        return self._count_gemini_events(
            event_types=("gemini_api_call_started",),
            media_job_id=media_job_id,
            since=since,
        )

    def count_daily_gemini_calls(
        self,
        utc_day_start: datetime,
        *,
        model: str | None = None,
    ) -> int:
        since = utc_day_start.isoformat()
        query = (
            self._client.table("media_job_events")
            .select("id", count="exact", head=True)
            .eq("event_type", "gemini_api_call_started")
            .gte("created_at", since)
        )
        if model is not None:
            query = query.contains("metadata", {"model": model})
        return int(query.execute().count or 0)

    def _count_gemini_events(
        self,
        *,
        event_types: tuple[str, ...],
        media_job_id: UUID | None = None,
        since: str | None = None,
    ) -> int:
        query = (
            self._client.table("media_job_events")
            .select("id", count="exact", head=True)
            .in_("event_type", list(event_types))
        )
        if media_job_id is not None:
            query = query.eq("media_job_id", str(media_job_id))
        if since is not None:
            query = query.gte("created_at", since)
        return int(query.execute().count or 0)

    def update_recipe_thumbnail(
        self,
        *,
        recipe_item_id: UUID,
        user_id: UUID,
        thumbnail_url: str,
    ) -> None:
        (
            self._client.table("recipe_items")
            .update({"thumbnail_url": thumbnail_url})
            .eq("id", str(recipe_item_id))
            .eq("user_id", str(user_id))
            .execute()
        )

    def get_recipe_thumbnail(
        self,
        *,
        recipe_item_id: UUID,
        user_id: UUID,
    ) -> str | None:
        response = (
            self._client.table("recipe_items")
            .select("thumbnail_url")
            .eq("id", str(recipe_item_id))
            .eq("user_id", str(user_id))
            .limit(1)
            .execute()
        )
        if not response.data:
            return None
        value = response.data[0].get("thumbnail_url")
        return value if isinstance(value, str) and value.strip() else None

    def upload_public_object(
        self,
        bucket: str,
        path: str,
        data: bytes,
        content_type: str,
    ) -> str:
        self._client.storage.from_(bucket).upload(
            path,
            data,
            {"content-type": content_type, "upsert": "true"},
        )
        return self._client.storage.from_(bucket).get_public_url(path)

    def insert_recipe_card(
        self,
        *,
        job: MediaJobRecord,
        result: GeminiRecipeResult,
        model: str,
        prompt_version: str = "m18_recipe_video_v1",
    ) -> UUID:
        if job.recipe_item_id is None:
            raise ValueError("Media job has no recipe item.")

        existing = (
            self._client.table("recipe_cards")
            .select("id")
            .eq("recipe_id", str(job.recipe_item_id))
            .eq("user_id", str(job.user_id))
            .order("created_at", desc=False)
            .limit(1)
            .execute()
        )
        if existing.data:
            row = existing.data[0] if isinstance(existing.data, list) else existing.data
            return UUID(str(row["id"]))

        ingredients = [
            {
                **item.model_dump(mode="json"),
                "name": item.name_en,
                "notes": item.notes_en,
            }
            for item in result.ingredients
        ]
        steps = [
            {
                **item.model_dump(mode="json"),
                "instruction": item.instruction_en,
                "tip": item.tip_en,
            }
            for item in result.steps
        ]
        grocery_list = [
            {**item.model_dump(mode="json"), "name": item.name_en}
            for item in result.grocery_list
        ]

        response = (
            self._client.table("recipe_cards")
            .insert(
                {
                    "recipe_id": str(job.recipe_item_id),
                    "user_id": str(job.user_id),
                    "recipe_title": result.recipe_title_en,
                    "short_summary": result.short_summary_en,
                    "ingredients_json": ingredients,
                    "steps_json": steps,
                    "time_json": {
                        "prep_time_minutes": result.prep_time_minutes,
                        "cook_time_minutes": result.cook_time_minutes,
                        "prep_minutes": result.prep_time_minutes,
                        "cook_minutes": result.cook_time_minutes,
                        "total_minutes": derive_recipe_total_minutes(
                            result.prep_time_minutes,
                            result.cook_time_minutes,
                            result.total_time_minutes,
                        ),
                    },
                    "servings": result.servings,
                    "difficulty": result.difficulty,
                    "grocery_list_json": grocery_list,
                    "tags_json": result.tags,
                    "missing_info_json": bilingual_list(
                        result.missing_info_en,
                        result.missing_info_ur,
                    )
                    + bilingual_list(
                        result.source_assumptions_en,
                        result.source_assumptions_ur,
                        prefix="Assumption: ",
                    ),
                    "storage_tip": bilingual_text(
                        result.storage_tip_en,
                        result.storage_tip_ur,
                    ),
                    "safety_note": bilingual_text(
                        result.safety_note_en,
                        result.safety_note_ur,
                    ),
                    "confidence": result.confidence,
                    "source_link": job.source_url,
                    "model_used": model,
                    "prompt_version": prompt_version,
                }
            )
            .select("id")
            .execute()
        )
        if not response.data:
            raise RuntimeError("Recipe card insert returned no row.")
        row = response.data[0] if isinstance(response.data, list) else response.data
        return UUID(str(row["id"]))


def create_repository(settings: Settings) -> MediaJobRepository:
    client = create_client(
        settings.supabase_url,
        settings.supabase_service_role_key.get_secret_value(),
    )
    return MediaJobRepository(client)


def bilingual_text(english: str, urdu: str) -> str:
    if english and urdu:
        return f"{english}\n\nاردو: {urdu}"
    return english or urdu


def bilingual_list(
    english_items: list[str],
    urdu_items: list[str],
    *,
    prefix: str = "",
) -> list[str]:
    length = max(len(english_items), len(urdu_items))
    return [
        prefix
        + bilingual_text(
            english_items[index] if index < len(english_items) else "",
            urdu_items[index] if index < len(urdu_items) else "",
        )
        for index in range(length)
    ]


def valid_recipe_minutes(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or parsed > MAX_RECIPE_DURATION_MINUTES:
        return None
    return parsed


def derive_recipe_total_minutes(
    prep_minutes: object,
    cook_minutes: object,
    explicit_total_minutes: object = None,
) -> int | None:
    explicit = valid_recipe_minutes(explicit_total_minutes)
    if explicit is not None:
        return explicit
    prep = valid_recipe_minutes(prep_minutes)
    cook = valid_recipe_minutes(cook_minutes)
    if prep is None or cook is None:
        return None
    return valid_recipe_minutes(prep + cook)
