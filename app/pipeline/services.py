from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from app.config import Settings
from app.job_runner import (
    platform_for_job,
    save_mock_recipe_card_for_job,
)
from app.pipeline.contracts import (
    DispatchEnvelope,
    FinalizationInput,
    GeminiAnalysisInput,
    GeminiAnalysisResult,
    IngestedMedia,
    SourceContext,
    StageTimestamps,
    utc_now,
)
from app.pipeline.resources import (
    AdmissionSnapshot,
    ResourceAdmissionController,
    byte_bucket,
    latency_bucket,
    queue_depth_bucket,
)
from app.pipeline.telemetry import PipelineTransitionRecorder, emit_stage_event
from app.pipeline.transport import InProcessStageTransport
from app.queue_worker import (
    DeferredGeminiRecipe,
    QueueJobFailure,
    _GEMINI_CALL_CONTEXT,
    add_event,
    create_gemini_client,
    delete_temp_media,
    ensure_quota_reservation_event_safely,
    finalize_quota_transition_safely,
    make_gemini_runtime_context,
    process_youtube_gemini_url_job,
    record_queue_failure_with_reporting,
    resolve_queue_media,
    run_gemini_video_poc_analysis,
    safe_int_or_none,
    save_gemini_recipe_card,
    save_metadata,
    sample_thumbnail_best_effort,
    str_or_none,
    update_job_source_fields,
    upload_thumbnail_best_effort,
)
from app.schemas import MediaJobRecord
from app.supabase_client import MediaJobRepository, create_repository
from app.social.thumbnail_selection import early_frame_thumbnail_selected

logger = logging.getLogger("recipe_dost.worker")
_finalization_locks = tuple(threading.Lock() for _ in range(64))


@dataclass
class BackpressureDebouncer:
    minimum_interval_seconds: float
    reason: str | None = None
    threshold_key: tuple[str, str, str] | None = None
    started_at: float | None = None
    last_emitted_at: float = 0.0

    def blocked(
        self,
        *,
        reason: str,
        queue_depth: int,
        snapshot: AdmissionSnapshot | None = None,
    ) -> None:
        now = perf_counter()
        threshold_key = (
            queue_depth_bucket(queue_depth),
            byte_bucket(snapshot.rss_bytes) if snapshot is not None else "unknown",
            byte_bucket(snapshot.temp_bytes) if snapshot is not None else "unknown",
        )
        changed = reason != self.reason or threshold_key != self.threshold_key
        elapsed = now - self.last_emitted_at
        if self.started_at is None or reason != self.reason:
            self.started_at = now
        self.reason = reason
        self.threshold_key = threshold_key
        if not changed and elapsed < self.minimum_interval_seconds:
            return
        self.last_emitted_at = now
        logger.info(
            "worker_stage_backpressure",
            extra={
                "stage": "dispatch",
                "reason": reason,
                "duration_bucket": latency_bucket(
                    int(max(0.0, now - (self.started_at or now)) * 1000)
                ),
                "queue_depth_bucket": threshold_key[0],
                "rss_bucket": threshold_key[1],
                "temp_byte_bucket": threshold_key[2],
            },
        )

    def cleared(self, *, queue_depth: int) -> None:
        if self.reason is None:
            return
        now = perf_counter()
        logger.info(
            "worker_stage_backpressure_cleared",
            extra={
                "stage": "dispatch",
                "reason": self.reason,
                "duration_bucket": latency_bucket(
                    int(max(0.0, now - (self.started_at or now)) * 1000)
                ),
                "queue_depth_bucket": queue_depth_bucket(queue_depth),
                "rss_bucket": "unknown",
                "temp_byte_bucket": "unknown",
            },
        )
        self.reason = None
        self.threshold_key = None
        self.started_at = None
        self.last_emitted_at = 0.0


def duration_category(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value <= 30:
        return "under_31s"
    if value <= 60:
        return "31_60s"
    if value <= 90:
        return "61_90s"
    return "91s_plus"


def dimension_category(width: int | None, height: int | None) -> str:
    if not width or not height:
        return "unknown"
    short_edge = min(width, height)
    if short_edge <= 360:
        return "up_to_360"
    if short_edge <= 540:
        return "361_540"
    if short_edge <= 720:
        return "541_720"
    return "over_720"


def source_context_contract(
    result: dict[str, object],
    platform: str,
) -> SourceContext:
    raw = (
        result.get("source_context")
        if isinstance(result.get("source_context"), dict)
        else {}
    )
    assert isinstance(raw, dict)

    def string_list(key: str, limit: int) -> list[str]:
        values = raw.get(key)
        if not isinstance(values, list):
            return []
        return [
            str(value).strip()
            for value in values[:limit]
            if isinstance(value, str) and str(value).strip()
        ]

    return SourceContext(
        platform=platform,
        title=str_or_none(result.get("title")),
        caption=str_or_none(raw.get("caption")),
        author=str_or_none(result.get("author_username")),
        creator_display_name=str_or_none(raw.get("creator_display_name")),
        creator_handle=str_or_none(raw.get("creator_handle")),
        text_language=str_or_none(raw.get("text_language")),
        music_title=str_or_none(raw.get("music_title")),
        music_author=str_or_none(raw.get("music_author")),
        duration_seconds=safe_int_or_none(raw.get("duration_seconds")),
        hashtags=string_list("hashtags", 20),
        on_screen_text=string_list("on_screen_text", 8),
    )


def analysis_extract_result(media: IngestedMedia) -> dict[str, object]:
    context = media.source_context.model_dump(
        mode="python",
        exclude_none=True,
        exclude={"platform", "title", "author"},
    )
    return {
        "platform": media.normalized_platform,
        "resolver": media.resolver_category,
        "output_mode": media.output_mode,
        "video_bytes_verified": media.video_bytes_verified,
        "temp_file_path": media.local_media_path,
        "size_bytes": media.size_bytes,
        "duration_seconds": media.duration_seconds,
        "width": media.width,
        "height": media.height,
        "title": media.source_context.title,
        "author_username": media.source_context.author,
        "source_context": context,
    }


def analysis_source_metadata(
    analysis: GeminiAnalysisResult,
) -> dict[str, object]:
    return {
        "platform": analysis.normalized_platform,
        "title": analysis.source_context.title,
        "source_context": analysis.source_context.model_dump(
            mode="python",
            exclude_none=True,
            exclude={"platform", "title", "author"},
        ),
    }


async def dispatcher_loop(
    *,
    settings: Settings,
    transport: InProcessStageTransport,
    stop_event: asyncio.Event,
    admission: ResourceAdmissionController,
    active_counts: dict[str, int],
    transitions: PipelineTransitionRecorder,
) -> None:
    backpressure = BackpressureDebouncer(
        settings.worker_backpressure_log_interval_seconds
    )
    while not stop_event.is_set():
        ingest_busy = (
            active_counts["ingest"] >= settings.worker_ingest_concurrency
        )
        if ingest_busy or not transport.ingest_has_capacity():
            backpressure.blocked(
                reason=(
                    "ingest_capacity_active"
                    if ingest_busy
                    else "ingest_queue_full"
                ),
                queue_depth=transport.depths()["ingest"],
            )
            await _bounded_sleep(settings, stop_event)
            continue
        snapshot = await asyncio.to_thread(admission.inspect)
        if not snapshot.admitted:
            backpressure.blocked(
                reason=snapshot.reason,
                queue_depth=transport.depths()["ingest"],
                snapshot=snapshot,
            )
            await _bounded_sleep(settings, stop_event)
            continue
        backpressure.cleared(queue_depth=transport.depths()["ingest"])
        repository = create_repository(settings)
        job = await asyncio.to_thread(repository.claim_oldest_queued_job)
        if job is None:
            await _bounded_sleep(settings, stop_event)
            continue
        await asyncio.to_thread(
            ensure_quota_reservation_event_safely,
            repository,
            job,
        )
        envelope = DispatchEnvelope(
            job_id=job.id,
            platform=job.platform or "unknown",
        )
        await transitions.emit(
            repository,
            job.id,
            "worker_pipeline_started",
            stage="dispatch",
            settings=settings,
            queue_depth=transport.depths()["ingest"],
            configured_concurrency=settings.worker_dispatch_concurrency,
            snapshot=snapshot,
        )
        await transitions.enqueue(
            repository,
            job.id,
            stage="ingest",
            settings=settings,
            queue_depth=transport.depths()["ingest"],
            configured_concurrency=settings.worker_ingest_concurrency,
            snapshot=snapshot,
            enqueue=transport.enqueue_ingest(envelope),
        )


async def ingest_loop(
    *,
    settings: Settings,
    transport: InProcessStageTransport,
    active_counts: dict[str, int],
    transitions: PipelineTransitionRecorder,
) -> None:
    while True:
        envelope = await transport.dequeue_ingest()
        started = perf_counter()
        active_counts["ingest"] += 1
        repository = create_repository(settings)
        await transitions.emit(
            repository,
            envelope.job_id,
            "worker_stage_started",
            stage="ingest",
            settings=settings,
            queue_depth=transport.depths()["ingest"],
            active_count=active_counts["ingest"],
            configured_concurrency=settings.worker_ingest_concurrency,
        )
        try:
            media = await asyncio.to_thread(
                ingest_materialize,
                repository,
                settings,
                envelope,
            )
            await transitions.emit(
                repository,
                envelope.job_id,
                "worker_stage_completed",
                stage="ingest",
                settings=settings,
                queue_depth=transport.depths()["ingest"],
                active_count=active_counts["ingest"],
                configured_concurrency=settings.worker_ingest_concurrency,
                started_at=started,
            )
            await transitions.enqueue(
                repository,
                envelope.job_id,
                stage="analysis",
                settings=settings,
                queue_depth=transport.depths()["analysis"],
                configured_concurrency=settings.worker_gemini_analysis_concurrency,
                enqueue=transport.enqueue_analysis(GeminiAnalysisInput(media=media)),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await transitions.emit(
                repository,
                envelope.job_id,
                "worker_stage_failed",
                stage="ingest",
                settings=settings,
                queue_depth=transport.depths()["ingest"],
                active_count=active_counts["ingest"],
                configured_concurrency=settings.worker_ingest_concurrency,
                recovery_category=(
                    exc.code
                    if isinstance(exc, QueueJobFailure)
                    else "unexpected_failure"
                ),
            )
            await asyncio.to_thread(
                fail_stage,
                repository,
                settings,
                envelope,
                "ingest",
                exc,
                None,
                False,
            )
        finally:
            active_counts["ingest"] = max(0, active_counts["ingest"] - 1)
            transport.task_done("ingest")


async def analyzer_loop(
    *,
    settings: Settings,
    transport: InProcessStageTransport,
    active_counts: dict[str, int],
    transitions: PipelineTransitionRecorder,
    worker_slot: int,
) -> None:
    while True:
        item = await transport.dequeue_analysis()
        envelope = item.media.dispatch
        started = perf_counter()
        active_counts["analysis"] += 1
        repository = create_repository(settings)
        await transitions.emit(
            repository,
            envelope.job_id,
            "worker_stage_started",
            stage="analysis",
            settings=settings,
            queue_depth=transport.depths()["analysis"],
            active_count=active_counts["analysis"],
            configured_concurrency=settings.worker_gemini_analysis_concurrency,
            worker_slot=worker_slot,
        )
        try:
            result = await asyncio.to_thread(
                analyze_media,
                repository,
                settings,
                item.media,
            )
            await transitions.emit(
                repository,
                envelope.job_id,
                "worker_stage_completed",
                stage="analysis",
                settings=settings,
                queue_depth=transport.depths()["analysis"],
                active_count=active_counts["analysis"],
                configured_concurrency=settings.worker_gemini_analysis_concurrency,
                started_at=started,
                worker_slot=worker_slot,
            )
            await transitions.enqueue(
                repository,
                envelope.job_id,
                stage="finalize",
                settings=settings,
                queue_depth=transport.depths()["finalize"],
                configured_concurrency=settings.worker_finalize_concurrency,
                enqueue=transport.enqueue_finalize(FinalizationInput(analysis=result)),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await transitions.emit(
                repository,
                envelope.job_id,
                "worker_stage_failed",
                stage="analysis",
                settings=settings,
                queue_depth=transport.depths()["analysis"],
                active_count=active_counts["analysis"],
                configured_concurrency=settings.worker_gemini_analysis_concurrency,
                worker_slot=worker_slot,
                recovery_category=(
                    exc.code
                    if isinstance(exc, QueueJobFailure)
                    else "unexpected_failure"
                ),
            )
            await asyncio.to_thread(
                fail_stage,
                repository,
                settings,
                envelope,
                "analysis",
                exc,
                item.media.local_media_path,
                False,
            )
        finally:
            active_counts["analysis"] = max(0, active_counts["analysis"] - 1)
            transport.task_done("analysis")


async def finalizer_loop(
    *,
    settings: Settings,
    transport: InProcessStageTransport,
    active_counts: dict[str, int],
    transitions: PipelineTransitionRecorder,
) -> None:
    while True:
        item = await transport.dequeue_finalize()
        envelope = item.analysis.dispatch
        started = perf_counter()
        active_counts["finalize"] += 1
        repository = create_repository(settings)
        await transitions.emit(
            repository,
            envelope.job_id,
            "worker_stage_started",
            stage="finalize",
            settings=settings,
            queue_depth=transport.depths()["finalize"],
            active_count=active_counts["finalize"],
            configured_concurrency=settings.worker_finalize_concurrency,
        )
        try:
            await asyncio.to_thread(
                finalize_result,
                repository,
                settings,
                item.analysis,
                started,
            )
            await transitions.emit(
                repository,
                envelope.job_id,
                "worker_stage_completed",
                stage="finalize",
                settings=settings,
                queue_depth=transport.depths()["finalize"],
                active_count=active_counts["finalize"],
                configured_concurrency=settings.worker_finalize_concurrency,
                started_at=started,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await transitions.emit(
                repository,
                envelope.job_id,
                "worker_stage_failed",
                stage="finalize",
                settings=settings,
                queue_depth=transport.depths()["finalize"],
                active_count=active_counts["finalize"],
                configured_concurrency=settings.worker_finalize_concurrency,
                recovery_category=(
                    exc.code
                    if isinstance(exc, QueueJobFailure)
                    else "unexpected_failure"
                ),
            )
            await asyncio.to_thread(
                fail_stage,
                repository,
                settings,
                envelope,
                "finalize",
                exc,
                item.analysis.local_media_path,
                False,
            )
        finally:
            cleanup_local_media(
                repository,
                repository.get_job(envelope.job_id),
                item.analysis.local_media_path,
            )
            active_counts["finalize"] = max(0, active_counts["finalize"] - 1)
            transport.task_done("finalize")


def ingest_materialize(
    repository: MediaJobRepository,
    settings: Settings,
    envelope: DispatchEnvelope,
) -> IngestedMedia:
    job = repository.get_job(envelope.job_id)
    if job is None:
        raise QueueJobFailure(
            "queue_job_failed",
            "Queue job could not be loaded.",
            stage="queue_claim",
        )
    add_event(
        repository,
        job.id,
        "queue_loop_started",
        "Queue loop is active.",
        {"worker_execution_mode": settings.worker_execution_mode},
    )
    add_event(
        repository,
        job.id,
        "queue_job_claimed",
        "Queued job claimed for processing.",
        {"previous_status": "queued", "status": "resolving", "progress": 20},
    )
    platform = platform_for_job(job)
    if platform == "youtube":
        return IngestedMedia(
            dispatch=envelope,
            normalized_platform=platform,
            resolver_category="youtube_oembed",
            media_mode="youtube_url",
            output_mode="remote_reference",
            size_category="not_materialized",
            duration_category="unknown",
            dimension_category="unknown",
            source_context=SourceContext(platform=platform),
            timestamps=StageTimestamps(
                started_at=envelope.timestamps.started_at,
                completed_at=utc_now(),
            ),
        )
    job = repository.update_job(
        job.id,
        status="downloading",
        progress=35,
        error_code=None,
        error_message=None,
    )
    result = resolve_queue_media(
        repository=repository,
        settings=settings,
        job=job,
        platform=platform,
    )
    temp_path = str_or_none(result.get("temp_file_path"))
    try:
        thumbnail = upload_thumbnail_best_effort(
            repository,
            settings,
            job,
            str_or_none(result.get("thumbnail_url")),
            str_or_none(result.get("thumbnail_source")),
            safe_int_or_none(result.get("duration_seconds")),
        )
        if str(result.get("resolver_strategy") or "") == "external":
            result["thumbnail_url"] = None
        if thumbnail is None and not result.get("thumbnail_url"):
            thumbnail = sample_thumbnail_best_effort(
                repository,
                settings,
                job,
                temp_path,
                safe_int_or_none(result.get("duration_seconds")),
            )
            if thumbnail:
                selected = early_frame_thumbnail_selected(platform)
                result.update(
                    {
                        "thumbnail_source": selected.source,
                        "thumbnail_provider": selected.provider,
                        "thumbnail_strategy": selected.strategy,
                        "thumbnail_candidate_count": selected.candidate_count,
                        "thumbnail_selected": True,
                    }
                )
        save_metadata(repository, job.id, result, thumbnail)
        update_job_source_fields(repository, job.id, result, thumbnail)
    except Exception:
        cleanup_local_media(repository, job, temp_path)
        raise
    size = safe_int_or_none(result.get("size_bytes")) or 0
    duration = safe_int_or_none(result.get("duration_seconds"))
    width = safe_int_or_none(result.get("width"))
    height = safe_int_or_none(result.get("height"))
    provider_mode = str(result.get("provider_media_mode") or "").lower()
    media_mode = "split_av" if provider_mode == "split_av" else "progressive"
    return IngestedMedia(
        dispatch=envelope,
        normalized_platform=platform,
        resolver_category=str(result.get("resolver") or "ytdlp_stream"),
        media_mode=media_mode,
        output_mode=str(result.get("output_mode") or "temp_file"),
        size_category=byte_bucket(size),
        duration_category=duration_category(duration),
        dimension_category=dimension_category(width, height),
        video_bytes_verified=bool(result.get("video_bytes_verified")),
        size_bytes=size or None,
        duration_seconds=duration,
        width=width,
        height=height,
        source_context=source_context_contract(result, platform),
        cleanup_obligations=["local_media"] if temp_path else [],
        timestamps=StageTimestamps(
            started_at=envelope.timestamps.started_at,
            completed_at=utc_now(),
        ),
        local_media_path=temp_path,
        thumbnail_url=thumbnail,
    )


def analyze_media(
    repository: MediaJobRepository,
    settings: Settings,
    media: IngestedMedia,
) -> GeminiAnalysisResult:
    job = repository.get_job(media.dispatch.job_id)
    if job is None:
        raise QueueJobFailure(
            "queue_job_failed",
            "Queue job could not be loaded.",
            stage="queue_claim",
        )
    token = _GEMINI_CALL_CONTEXT.set(
        make_gemini_runtime_context(repository, job, settings)
    )
    try:
        if settings.ai_analysis_mode != "gemini_video_poc":
            return GeminiAnalysisResult(
                dispatch=media.dispatch,
                normalized_platform=media.normalized_platform,
                resolver_category=media.resolver_category,
                source_context=media.source_context,
                cleanup_obligations=media.cleanup_obligations,
                timestamps=StageTimestamps(started_at=utc_now(), completed_at=utc_now()),
                local_media_path=media.local_media_path,
                thumbnail_url=media.thumbnail_url,
            )
        deferred = (
            process_youtube_gemini_url_job(
                repository=repository,
                settings=settings,
                job=job,
                platform=media.normalized_platform,
                started_at=perf_counter(),
                defer_finalization=True,
            )
            if media.media_mode == "youtube_url"
            else run_gemini_video_poc_analysis(
                repository=repository,
                job=job,
                settings=settings,
                extract_result=analysis_extract_result(media),
                platform=media.normalized_platform,
                uploaded_thumbnail_url=media.thumbnail_url,
                defer_finalization=True,
            )
        )
        if not isinstance(deferred, DeferredGeminiRecipe):
            raise QueueJobFailure(
                "gemini_structured_extraction_failed",
                "Recipe extraction could not complete.",
                stage="gemini_structured_extraction",
            )
        return GeminiAnalysisResult(
            dispatch=media.dispatch,
            normalized_platform=deferred.platform,
            resolver_category=deferred.resolver,
            recipe=deferred.result,
            model=deferred.model,
            used_fallback=deferred.used_fallback,
            brief_text=deferred.brief_text,
            source_context=media.source_context,
            cleanup_obligations=media.cleanup_obligations,
            timestamps=StageTimestamps(started_at=utc_now(), completed_at=utc_now()),
            local_media_path=media.local_media_path,
            thumbnail_url=deferred.uploaded_thumbnail_url,
        )
    finally:
        _GEMINI_CALL_CONTEXT.reset(token)


def finalize_result(
    repository: MediaJobRepository,
    settings: Settings,
    analysis: GeminiAnalysisResult,
    started_at: float,
) -> MediaJobRecord:
    with _finalization_lock_for(analysis.dispatch.job_id):
        durable_job = repository.get_job(analysis.dispatch.job_id)
        job = durable_job
        if job is None:
            raise QueueJobFailure(
                "queue_job_failed",
                "Queue job could not be loaded.",
                stage="queue_claim",
            )
        if job.status in {"completed", "failed"}:
            finalize_quota_transition_safely(repository, job)
            return job
        if analysis.recipe is None:
            completed = save_mock_recipe_card_for_job(
                repository=repository,
                job=job,
                settings=settings,
                platform=analysis.normalized_platform,
                resolver=analysis.resolver_category,
                thumbnail_url=analysis.thumbnail_url,
                job_started_at=started_at,
                fixture_error_code="mock_fixture_failed",
            )
        else:
            client = (
                create_gemini_client(
                    settings.gemini_api_key.get_secret_value(),
                    settings.gemini_file_upload_timeout_seconds,
                )
                if settings.gemini_api_key is not None
                else None
            )
            token = _GEMINI_CALL_CONTEXT.set(
                make_gemini_runtime_context(repository, job, settings)
            )
            try:
                completed = save_gemini_recipe_card(
                    repository=repository,
                    job=job,
                    result=analysis.recipe,
                    model=analysis.model or settings.effective_gemini_text_model,
                    platform=analysis.normalized_platform,
                    resolver=analysis.resolver_category,
                    uploaded_thumbnail_url=analysis.thumbnail_url,
                    source_metadata=analysis_source_metadata(analysis),
                    used_fallback=analysis.used_fallback,
                    brief_text=analysis.brief_text,
                    client=client,
                    settings=settings,
                )
            finally:
                _GEMINI_CALL_CONTEXT.reset(token)
        finalize_quota_transition_safely(repository, completed)
        add_event(
            repository,
            completed.id,
            "queue_job_completed",
            "Queue job completed.",
            {
                "platform": analysis.normalized_platform,
                "resolver": analysis.resolver_category,
                "elapsed_ms": max(0, int((perf_counter() - started_at) * 1000)),
            },
        )
        return completed


def fail_stage(
    repository: MediaJobRepository,
    settings: Settings,
    envelope: DispatchEnvelope,
    stage: str,
    exc: Exception,
    local_media_path: str | None,
    emit_telemetry: bool = True,
) -> None:
    if emit_telemetry:
        emit_stage_event(
            repository,
            envelope.job_id,
            "worker_stage_failed",
            stage=stage,
            settings=settings,
            configured_concurrency=stage_concurrency(settings, stage),
            recovery_category=(
                exc.code if isinstance(exc, QueueJobFailure) else "unexpected_failure"
            ),
        )
    current = repository.get_job(envelope.job_id)
    if current is None or current.status not in {"completed", "failed"}:
        failure = (
            exc
            if isinstance(exc, QueueJobFailure)
            else QueueJobFailure(
                "queue_job_failed",
                "Queue worker failed while processing this recipe.",
                stage=stage,
                exception_type=type(exc).__name__,
            )
        )
        record_queue_failure_with_reporting(
            repository,
            envelope.job_id,
            failure.code,
            failure.message,
            stage=failure.stage,
            exception_type=failure.exception_type,
            metadata=failure.metadata,
            source_platform=envelope.platform,
        )
    cleanup_local_media(repository, current, local_media_path)


def cleanup_local_media(
    repository: MediaJobRepository,
    job: MediaJobRecord | None,
    local_media_path: str | None,
) -> None:
    if delete_temp_media(local_media_path) and job is not None:
        add_event(
            repository,
            job.id,
            "temp_video_deleted",
            "Temporary video file deleted.",
            {"stage": "queue_cleanup"},
        )


async def _bounded_sleep(settings: Settings, stop_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(
            stop_event.wait(),
            timeout=max(0.25, settings.worker_admission_retry_seconds),
        )
    except TimeoutError:
        pass


async def emit_stage_event_async(
    repository: object,
    job_id: object,
    event_type: str,
    **kwargs: object,
) -> None:
    try:
        await asyncio.to_thread(
            emit_stage_event,
            repository,
            job_id,
            event_type,
            **kwargs,
        )
    except Exception:
        logger.warning(
            "worker_stage_event_persistence_failed",
            extra={"stage": str(kwargs.get("stage") or "unknown")},
        )


def _log_backpressure(snapshot: AdmissionSnapshot) -> None:
    logger.info(
        "worker_stage_backpressure",
        extra={"stage": "dispatch", "reason": snapshot.reason},
    )


def _finalization_lock_for(job_id: object) -> threading.Lock:
    return _finalization_locks[hash(job_id) % len(_finalization_locks)]


def stage_concurrency(settings: Settings, stage: str) -> int:
    return {
        "dispatch": settings.worker_dispatch_concurrency,
        "ingest": settings.worker_ingest_concurrency,
        "analysis": settings.worker_gemini_analysis_concurrency,
        "finalize": settings.worker_finalize_concurrency,
    }.get(stage, 1)
