from __future__ import annotations

import json
import tempfile
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from postgrest.exceptions import APIError

from app.config import Settings
from app.media_resolvers.contracts import MediaResolutionRequest
from app.media_resolvers.instagram_unified_v1 import InstagramUnifiedV1Resolver
from app.media_resolvers.media_processing import (
    MediaProbe,
    validate_video_probe_compatibility,
)
from app.media_resolvers.selection import select_video_candidate
from app.media_resolvers.tiktok_akng import TikTokAkngResolver
from app.queue_worker import (
    QueueJobFailure,
    _GEMINI_CALL_CONTEXT,
    delete_temp_media,
    process_queue_job,
    record_queue_failure,
    run_single_pass_structured_extraction,
    save_gemini_recipe_card,
    save_metadata,
)
from app.quota_finalization import (
    ensure_quota_reservation_event,
    finalize_quota_transition,
)
from app.schemas import MediaJobRecord

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
QA_FIXTURE_ROOT = FIXTURE_ROOT / "worker_qa"
RESOLVER_FIXTURE_ROOT = FIXTURE_ROOT / "media_resolvers"
JOB_ID_BASE = 10_000
USER_ID = UUID("00000000-0000-0000-0000-000000000222")
RECIPE_ID = UUID("00000000-0000-0000-0000-000000000333")
CARD_ID = UUID("00000000-0000-0000-0000-000000000444")


class OfflineRepository:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.jobs: dict[UUID, MediaJobRecord] = {}
        self.active_jobs = 0
        self.max_active_jobs = 0
        self.private_metadata_failure = False
        self.terminal_updates: dict[UUID, int] = {}

    def register(self, job: MediaJobRecord) -> None:
        self.jobs[job.id] = job

    def get_job(self, media_job_id: UUID) -> MediaJobRecord | None:
        return self.jobs.get(media_job_id)

    def update_job(self, media_job_id: UUID, **values: object) -> MediaJobRecord:
        current = self.jobs[media_job_id]
        updated = current.model_copy(update=values)
        self.jobs[media_job_id] = updated
        if updated.status in {"completed", "failed"}:
            self.terminal_updates[media_job_id] = (
                self.terminal_updates.get(media_job_id, 0) + 1
            )
        self.active_jobs = sum(
            item.status
            in {
                "queued",
                "resolving",
                "downloading",
                "analyzing",
            }
            for item in self.jobs.values()
        )
        self.max_active_jobs = max(self.max_active_jobs, self.active_jobs)
        return updated

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
                "metadata": dict(metadata or {}),
            }
        )

    def add_event(self, media_job_id: UUID, **kwargs: object) -> None:
        self.insert_event(media_job_id, **kwargs)  # type: ignore[arg-type]

    def count_job_gemini_calls(
        self,
        media_job_id: UUID,
        *,
        since_latest_worker_start: bool = True,
    ) -> int:
        del since_latest_worker_start
        return sum(
            event["media_job_id"] == media_job_id
            and event["event_type"] == "gemini_api_call_started"
            for event in self.events
        )

    def count_daily_gemini_calls(
        self,
        _utc_day_start: object,
        *,
        model: str | None = None,
    ) -> int:
        del model
        return 0

    def has_event(self, media_job_id: UUID, event_type: str) -> bool:
        return any(
            event["media_job_id"] == media_job_id
            and event["event_type"] == event_type
            for event in self.events
        )

    def get_ai_access_context(self, _media_job_id: UUID) -> dict[str, object]:
        return {
            "allowed": True,
            "plan": "premium",
            "reason": "premium_active",
        }

    def get_quota_counts(
        self,
        *,
        user_id: UUID,
        plan: str,
    ) -> dict[str, int]:
        del plan
        jobs = [job for job in self.jobs.values() if job.user_id == user_id]
        return {
            "active_reservation_count": sum(
                job.status
                in {"queued", "resolving", "downloading", "analyzing"}
                for job in jobs
            ),
            "successful_count": sum(
                job.status == "completed"
                and job.result_recipe_card_id is not None
                for job in jobs
            ),
        }

    def insert_recipe_card(self, **_kwargs: object) -> UUID:
        return CARD_ID

    def upsert_private_metadata(self, *_args: object, **_kwargs: object) -> None:
        if self.private_metadata_failure:
            raise APIError(
                {
                    "code": "23502",
                    "message": "fixture",
                    "details": "fixture",
                    "hint": None,
                }
            )

    def update_source_metadata(
        self,
        _media_job_id: UUID,
        _source_metadata: dict[str, object],
    ) -> None:
        return None

    def update_job_metadata_best_effort(
        self,
        media_job_id: UUID,
        values: dict[str, object],
    ) -> None:
        current = self.jobs[media_job_id]
        supported = {
            key: value
            for key, value in values.items()
            if key in current.__class__.model_fields
        }
        if supported:
            self.jobs[media_job_id] = current.model_copy(update=supported)

    def has_external_resolver_attempt(
        self,
        media_job_id: UUID,
        adapter_id: str,
    ) -> bool:
        return any(
            event["media_job_id"] == media_job_id
            and event["event_type"] == "external_resolver_started"
            and isinstance(event["metadata"], dict)
            and event["metadata"].get("resolver_adapter") == adapter_id
            for event in self.events
        )

    def get_recipe_thumbnail(self, **_kwargs: object) -> None:
        return None

    def update_recipe_thumbnail(self, **_kwargs: object) -> None:
        return None

    def upload_public_object(self, *_args: object, **_kwargs: object) -> str:
        return "offline-thumbnail"


class FixtureProviderClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def get_json(self, **_kwargs: object) -> tuple[dict[str, object], str, None]:
        return self.payload, "2xx", None


class FixtureGeminiClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.models = self
        self.generate_count = 0

    def generate_content(self, **_kwargs: object) -> object:
        self.generate_count += 1
        if not self.responses:
            raise RuntimeError("Fixture Gemini response sequence exhausted.")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class FixtureClientError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("fixture client error")
        self.status_code = status_code


def fixture_response(
    text: str | None,
    *,
    finish_reason: str = "STOP",
    safety_blocked: bool = False,
) -> object:
    parts = [] if text is None else [SimpleNamespace(text=text)]
    candidate = SimpleNamespace(
        finish_reason=SimpleNamespace(name=finish_reason),
        content=SimpleNamespace(parts=parts),
    )
    return SimpleNamespace(
        text=text,
        candidates=[candidate],
        prompt_feedback=SimpleNamespace(blocked=True) if safety_blocked else None,
    )


def canonical_recipe() -> dict[str, object]:
    return {
        "is_recipe": True,
        "title_en": "Fixture Curry",
        "title_ur": "فکسچر سالن",
        "summary_en": "A deterministic fixture curry.",
        "summary_ur": "ایک فکسچر سالن۔",
        "ingredients": [
            {
                "name_en": "Ingredient A",
                "name_ur": "جزو الف",
                "quantity": "1 unit",
                "notes_en": "",
                "notes_ur": "",
            },
            {
                "name_en": "Ingredient B",
                "name_ur": "جزو ب",
                "quantity": "2 units",
                "notes_en": "",
                "notes_ur": "",
            },
        ],
        "steps": [
            {
                "step_number": number,
                "instruction_en": f"Fixture instruction {number}.",
                "instruction_ur": f"فکسچر ہدایت {number}۔",
                "tip_en": "",
                "tip_ur": "",
            }
            for number in range(1, 4)
        ],
        "grocery_list": [
            {"name_en": "Ingredient A", "name_ur": "جزو الف", "quantity": "1 unit"}
        ],
        "time": {
            "prep_minutes": None,
            "cook_minutes": None,
            "total_minutes": None,
        },
        "servings": None,
        "difficulty": "easy",
        "cuisine_en": "Fixture",
        "cuisine_ur": "فکسچر",
        "tags": ["curry", "stovetop", "savory", "dinner", "fixture cuisine"],
        "missing_info": [],
        "confidence": "high",
    }


def settings_for_case(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "WORKER_API_SECRET": "fixture",
        "supabase_url": "https://example.invalid",
        "supabase_service_role_key": "fixture",
        "app_env": "production",
        "media_resolver_mode": "legacy",
        "external_media_resolver_audience": "disabled",
        "instagram_resolver_chain": [],
        "tiktok_resolver_chain": [],
        "gemini_max_calls_per_job": 3,
        "gemini_primary_model": "gemini-3.1-flash-lite",
        "gemini_repair_model": "gemini-3.1-flash-lite",
        "gemini_fallback_model": "gemini-2.5-flash",
        "recipe_repair_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def make_job(index: int, platform: str) -> MediaJobRecord:
    job_id = UUID(int=JOB_ID_BASE + index)
    return MediaJobRecord(
        id=job_id,
        user_id=USER_ID,
        recipe_item_id=RECIPE_ID,
        source_url="https://source.invalid/offline-fixture",
        platform=platform,
        status="queued",
        progress=0,
    )


def run_case(
    case_name: str,
    *,
    repository: OfflineRepository | None = None,
    index: int = 1,
) -> dict[str, object]:
    repository = repository or OfflineRepository()
    platform = "instagram" if case_name.startswith("instagram") else "tiktok"
    job = make_job(index, platform)
    repository.register(job)
    ensure_quota_reservation_event(repository, job)
    started = perf_counter()
    cleanup_succeeded = _exercise_cleanup()
    terminal_error: str | None = None
    try:
        if case_name.startswith(("tiktok_success", "instagram_")):
            _exercise_resolver(case_name)
            responses = [fixture_response(json.dumps(canonical_recipe(), ensure_ascii=False))]
            _run_structured_success(repository, job, settings_for_case(), responses)
        elif case_name == "primary_valid_json":
            _run_structured_success(
                repository,
                job,
                settings_for_case(),
                [fixture_response(json.dumps(canonical_recipe(), ensure_ascii=False))],
            )
        elif case_name == "primary_fenced_json":
            payload = json.dumps(canonical_recipe(), ensure_ascii=False)
            _run_structured_success(
                repository,
                job,
                settings_for_case(),
                [fixture_response(f"```json\n{payload}\n```")],
            )
        elif case_name == "primary_malformed_repaired":
            _run_structured_success(
                repository,
                job,
                settings_for_case(),
                [
                    fixture_response('{"is_recipe": true, "title_en":'),
                    fixture_response(json.dumps(canonical_recipe(), ensure_ascii=False)),
                ],
            )
        elif case_name == "private_metadata_failure":
            repository.private_metadata_failure = True
            save_metadata(
                repository,  # type: ignore[arg-type]
                job.id,
                {
                    "platform": platform,
                    "resolver": "fixture",
                    "raw_metadata": None,
                },
                None,
                allow_private_metadata_failure=True,
            )
            _run_structured_success(
                repository,
                job,
                settings_for_case(),
                [fixture_response(json.dumps(canonical_recipe(), ensure_ascii=False))],
            )
        elif case_name == "primary_empty_response":
            _run_structured_failure(
                repository,
                job,
                settings_for_case(gemini_fallback_model=""),
                [fixture_response(None)],
            )
        elif case_name == "primary_safety_block":
            _run_structured_failure(
                repository,
                job,
                settings_for_case(gemini_fallback_model=""),
                [fixture_response(None, finish_reason="SAFETY", safety_blocked=True)],
            )
        elif case_name == "primary_client_error":
            _run_structured_failure(
                repository,
                job,
                settings_for_case(gemini_fallback_model=""),
                [FixtureClientError(400)],
            )
        elif case_name == "fallback_configuration_rejection":
            _run_structured_failure(
                repository,
                job,
                settings_for_case(
                    recipe_repair_enabled=False,
                    gemini_fallback_model="unsupported-fixture-model",
                ),
                [fixture_response('{"is_recipe": true')],
            )
        elif case_name in {"call_budget_exhaustion", "deliberate_gemini_parse_failure"}:
            _run_structured_failure(
                repository,
                job,
                settings_for_case(gemini_max_calls_per_job=2),
                [
                    fixture_response('{"is_recipe": true'),
                    fixture_response('{"is_recipe": true'),
                ],
            )
        elif case_name in {
            "provider_failure",
            "media_download_failure",
            "wake_timeout",
        }:
            error = (
                "wake_timeout"
                if case_name == "wake_timeout"
                else "media_download_failed"
            )
            record_queue_failure(
                repository,  # type: ignore[arg-type]
                job.id,
                error,
                "Offline fixture failed.",
                stage="external_resolver",
            )
        else:
            raise ValueError(f"Unknown offline QA case: {case_name}")
    except QueueJobFailure as exc:
        terminal_error = str(exc.metadata.get("failure_category") or exc.code)
        current = repository.jobs[job.id]
        if current.status not in {"completed", "failed"}:
            record_queue_failure(
                repository,  # type: ignore[arg-type]
                job.id,
                exc.code,
                exc.message,
                stage=exc.stage,
                exception_type=exc.exception_type,
                metadata=exc.metadata,
            )
    final = repository.jobs[job.id]
    calls = [
        {
            "purpose": str(event["metadata"].get("call_purpose") or "unknown"),
            "model": str(event["metadata"].get("model") or "unknown"),
        }
        for event in repository.events
        if event["media_job_id"] == job.id
        and event["event_type"] == "gemini_api_call_started"
        and isinstance(event["metadata"], dict)
    ]
    error_category = terminal_error or final.error_code
    if final.status == "failed" and not error_category:
        error_category = "offline_failure"
    return {
        "case_name": case_name,
        "stage_durations_ms": {
            "total": max(0, int((perf_counter() - started) * 1000))
        },
        "terminal_status": final.status,
        "normalized_error_category": error_category,
        "call_counts": _call_counts(calls),
        "queue_wait_bucket": "under_1s",
        "cleanup_success": cleanup_succeeded,
        "reservation_created_count": _event_count(
            repository, job.id, "ai_quota_reservation_created"
        ),
        "reservation_released_count": _event_count(
            repository, job.id, "ai_quota_reservation_released"
        ),
        "success_committed_count": _event_count(
            repository, job.id, "ai_quota_success_committed"
        ),
        "finalization_replayed_count": _event_count(
            repository, job.id, "ai_quota_finalization_replayed"
        ),
    }


def run_batch(case_names: list[str]) -> dict[str, object]:
    repository = OfflineRepository()
    results = [
        run_case(name, repository=repository, index=index)
        for index, name in enumerate(case_names, start=1)
    ]
    completed = sum(item["terminal_status"] == "completed" for item in results)
    terminal_count = sum(
        item["terminal_status"] in {"completed", "failed"} for item in results
    )
    expected_successful = sum(
        job.status == "completed" and job.result_recipe_card_id is not None
        for job in repository.jobs.values()
    )
    expected_active = sum(
        job.status in {"queued", "resolving", "downloading", "analyzing"}
        for job in repository.jobs.values()
    )
    return {
        "cases": results,
        "fifo_order": [item["case_name"] for item in results],
        "one_active_job": repository.max_active_jobs <= 1,
        "terminal_count": terminal_count,
        "job_count": len(results),
        "active_reservation_count": expected_active,
        "successful_count": expected_successful,
        "success_ratio": (
            round(completed / len(results), 4) if results else 0.0
        ),
        "queue_starvation": False,
    }


def load_manifest(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not all(isinstance(item, str) for item in cases):
        raise ValueError("QA manifest must contain a string cases array.")
    return cases


def _run_structured_success(
    repository: OfflineRepository,
    job: MediaJobRecord,
    settings: Settings,
    responses: list[object],
) -> None:
    active = repository.update_job(job.id, status="analyzing", progress=60)
    client = FixtureGeminiClient(responses)
    token = _GEMINI_CALL_CONTEXT.set((repository, active, settings))  # type: ignore[arg-type]
    try:
        result, model, _repair = run_single_pass_structured_extraction(
            repository=repository,  # type: ignore[arg-type]
            job=active,
            settings=settings,
            client=client,
            input_part=object(),
            input_mode="offline_fixture",
            source_metadata={"platform": job.platform, "title": "Fixture"},
            platform=job.platform,
        )
        completed = save_gemini_recipe_card(
            repository=repository,  # type: ignore[arg-type]
            job=active,
            result=result,
            model=model,
            platform=job.platform,
            resolver="offline_fixture",
            uploaded_thumbnail_url=None,
            source_metadata={"platform": job.platform, "title": "Fixture"},
            settings=settings,
        )
        finalize_quota_transition(repository, completed)
    finally:
        _GEMINI_CALL_CONTEXT.reset(token)


def _run_structured_failure(
    repository: OfflineRepository,
    job: MediaJobRecord,
    settings: Settings,
    responses: list[object],
) -> None:
    active = repository.update_job(job.id, status="analyzing", progress=60)
    client = FixtureGeminiClient(responses)
    token = _GEMINI_CALL_CONTEXT.set((repository, active, settings))  # type: ignore[arg-type]
    try:
        run_single_pass_structured_extraction(
            repository=repository,  # type: ignore[arg-type]
            job=active,
            settings=settings,
            client=client,
            input_part=object(),
            input_mode="offline_fixture",
            source_metadata={"platform": job.platform, "title": "Fixture"},
            platform=job.platform,
        )
    finally:
        _GEMINI_CALL_CONTEXT.reset(token)


def _exercise_resolver(case_name: str) -> None:
    if case_name.startswith("tiktok"):
        payload = _json_fixture("akng_tiktok", "success_multi_quality.json")
        resolver = TikTokAkngResolver(
            client=FixtureProviderClient(payload),  # type: ignore[arg-type]
            base_url="https://provider.invalid",
            access_token="fixture",
        )
        request = MediaResolutionRequest(
            platform="tiktok",
            canonical_source_url=(
                "https://www.tiktok.com/@fixture/video/7651218940919598000"
            ),
            correlation_id="offline",
            capability="video_with_audio",
            byte_limit=50 * 1024 * 1024,
            preferred_byte_target=36 * 1024 * 1024,
            timeout_seconds=15,
        )
    else:
        fixture_name = (
            "valid_split_av"
            if case_name == "instagram_split_av_success"
            else "valid_video_only"
        )
        contracts = _json_fixture(
            "rapidapi_instagram_unified_v1",
            "contracts.json",
        )
        payload = contracts[fixture_name]
        resolver = InstagramUnifiedV1Resolver(
            client=FixtureProviderClient(payload),  # type: ignore[arg-type]
            base_url="https://provider.invalid",
            rapidapi_host="provider.invalid",
            rapidapi_key="fixture",
            allowed_media_host_suffixes=("fbcdn.net", "cdninstagram.com"),
        )
        request = MediaResolutionRequest(
            platform="instagram",
            canonical_source_url="https://instagram.invalid/offline",
            correlation_id="offline",
            capability="video_with_audio",
            byte_limit=50 * 1024 * 1024,
            preferred_byte_target=36 * 1024 * 1024,
            timeout_seconds=15,
        )
    resolution = resolver.resolve(request)
    select_video_candidate(
        resolution.candidates,
        preferred_bytes=request.preferred_byte_target,
        hard_limit_bytes=request.byte_limit,
        allowed_host_suffixes=(
            ("provider.invalid",)
            if case_name.startswith("tiktok")
            else ("fbcdn.net", "cdninstagram.com")
        ),
        enforce_max_short_edge=False,
    )
    validate_video_probe_compatibility(
        MediaProbe(
            video_present=True,
            audio_present=True,
            duration_seconds=60,
            width=720,
            height=1280,
            video_codec="h264",
            audio_codec="aac",
            container="mp4",
        )
    )


def _exercise_cleanup() -> bool:
    with tempfile.TemporaryDirectory(prefix="worker-qa-") as directory:
        temp_path = Path(directory) / "fixture.bin"
        temp_path.write_bytes(b"offline")
        deleted = delete_temp_media(str(temp_path))
        return deleted and not temp_path.exists()


def _json_fixture(adapter: str, name: str) -> dict[str, object]:
    return json.loads(
        (RESOLVER_FIXTURE_ROOT / adapter / name).read_text(encoding="utf-8")
    )


def _event_count(
    repository: OfflineRepository,
    job_id: UUID,
    event_type: str,
) -> int:
    return sum(
        event["media_job_id"] == job_id and event["event_type"] == event_type
        for event in repository.events
    )


def _call_counts(calls: list[dict[str, str]]) -> list[dict[str, object]]:
    counts: dict[tuple[str, str], int] = {}
    for call in calls:
        key = (call["purpose"], call["model"])
        counts[key] = counts.get(key, 0) + 1
    return [
        {"purpose": purpose, "model": model, "count": count}
        for (purpose, model), count in sorted(counts.items())
    ]
