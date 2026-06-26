from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from time import perf_counter
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from app.ai.gemini_recipe import (
    TEMPORARY_GEMINI_ERRORS,
    GeminiAnalysisError,
    GeminiCallGuardError,
    GeminiRecipeAnalyzer,
)
from app.config import Settings, WorkerMode
from app.schemas import MediaJobData, MediaJobRecord
from app.social.platforms import detect_social_platform
from app.social.stream_download import VideoBuffer, stream_video_to_memory
from app.social.thumbnail_upload import upload_source_thumbnail
from app.social.models import SocialResolverError, SocialVideoInfo
from app.social.resolver_orchestrator import resolve_social_video
from app.supabase_client import MediaJobRepository

logger = logging.getLogger("recipe_dost.worker")

SKELETON_ERROR_CODE = "worker_skeleton_no_media_provider"
SKELETON_ERROR_MESSAGE = (
    "Automatic video analysis is currently available for YouTube. "
    "Paste caption or visible ingredients for this source."
)

GEMINI_INVALID_JSON_MESSAGE = (
    "Gemini returned an invalid recipe response. Please try again."
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

YOUTUBE_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


class JobNotFoundError(Exception):
    pass


class WorkerProcessingError(Exception):
    pass


class PipelineFailure(Exception):
    def __init__(
        self,
        code: str,
        user_message: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.user_message = user_message
        self.metadata = metadata or {}


class JobRunner:
    def __init__(
        self,
        repository: MediaJobRepository,
        analyzer: GeminiRecipeAnalyzer | None = None,
        worker_mode: WorkerMode = "skeleton",
        app_env: str = "development",
        gemini_max_calls_per_job: int = 2,
        gemini_daily_dev_call_limit: int = 5,
        gemini_disabled_models: list[str] | None = None,
        gemini_daily_dev_model_limits: dict[str, int] | None = None,
        gemini_dev_budget_reset_at: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._repository = repository
        self._analyzer = analyzer
        self._worker_mode = worker_mode
        self._app_env = app_env
        self._gemini_max_calls_per_job = max(0, gemini_max_calls_per_job)
        self._gemini_daily_dev_call_limit = max(0, gemini_daily_dev_call_limit)
        self._gemini_disabled_models = set(gemini_disabled_models or [])
        self._gemini_daily_dev_model_limits = gemini_daily_dev_model_limits or {}
        self._gemini_dev_budget_reset_at = gemini_dev_budget_reset_at
        self._settings = settings

    def accept(
        self,
        media_job_id: UUID,
        *,
        force: bool = False,
        force_reset_attempt_budget: bool = False,
    ) -> tuple[MediaJobData, bool]:
        job = self._repository.get_job(media_job_id)
        if job is None:
            raise JobNotFoundError
        active = {
            "queued",
            "resolving",
            "downloading",
            "validating",
            "thumbnailing",
            "uploading_to_ai",
            "analyzing",
            "saving_result",
        }
        if job.status in active and job.status != "queued":
            return self._to_response(job), False
        if job.status == "completed" and not (
            force and self._app_env == "development"
        ):
            return self._to_response(job), False
        if job.status in {"failed", "needs_caption"} and not force:
            return self._to_response(job), False
        if force_reset_attempt_budget and self._app_env == "development":
            self._repository.insert_event(
                media_job_id,
                event_type="dev_attempt_budget_reset_requested",
                message="Development attempt budget reset requested.",
                metadata={},
            )
        job = self._repository.update_job(
            media_job_id,
            status="resolving",
            progress=10,
            error_code=None,
            error_message=None,
            started_at=job.started_at or datetime.now(timezone.utc).isoformat(),
        )
        self._repository.insert_event(
            media_job_id,
            event_type="worker_started",
            message="Media worker accepted the job.",
            metadata={"status": "resolving", "progress": 10},
        )
        data = self._to_response(job)
        data.accepted = True
        return data, True

    def run(self, media_job_id: UUID, background_resume: bool = False) -> MediaJobData:
        try:
            job = self._repository.get_job(media_job_id)
            if job is None:
                raise JobNotFoundError

            if not background_resume and job.status in IDEMPOTENT_STATUSES:
                return self._to_response(job)
            if not background_resume and job.status == "failed":
                return self._to_response(job)

            platform = detect_social_platform(job.source_url)
            if self._worker_mode == "youtube_direct" and platform == "youtube":
                job = self._run_youtube(job)
            elif self._worker_mode == "social_video" and platform in {"instagram", "tiktok", "facebook"}:
                job = self._run_social_video(job, platform)
            else:
                job = self._run_skeleton(job)

            return self._to_response(job)
        except JobNotFoundError:
            raise
        except PipelineFailure as exc:
            self._log_job_failure(media_job_id, type(exc).__name__)
            self._record_failure(
                media_job_id,
                error_code=exc.code,
                error_message=exc.user_message,
                exception_type=type(exc).__name__,
                metadata=exc.metadata,
            )
            failed_job = self._repository.get_job(media_job_id)
            if failed_job is not None:
                return self._to_response(failed_job)
            raise WorkerProcessingError from exc
        except Exception as exc:
            self._log_job_failure(media_job_id, type(exc).__name__)
            self._record_failure(
                media_job_id,
                error_code="worker_error",
                error_message="Media worker failed.",
                exception_type=type(exc).__name__,
            )
            raise WorkerProcessingError from exc

    def _run_social_video(self, job: MediaJobRecord, platform: str) -> MediaJobRecord:
        settings = self._settings
        if settings is None or not settings.enable_social_video:
            raise PipelineFailure(
                "social_video_not_enabled",
                "Social video analysis is not enabled yet.",
                {"platform": platform},
            )
        if self._analyzer is None:
            raise PipelineFailure("gemini_not_configured", "Gemini video analysis is not configured yet.")

        resolver_name = expected_resolver_for_platform(platform)
        self._repository.insert_event(
            job.id,
            event_type=f"{resolver_name}_resolver_started",
            message=f"{resolver_name} resolver started.",
            metadata={
                "platform": platform,
                "resolver": resolver_name,
            },
        )
        job = self._repository.update_job(job.id, status="resolving", progress=20, error_code=None, error_message=None)
        resolver_started_at = perf_counter()
        try:
            info = resolve_social_video(
                job.source_url,
                platform,
                settings,
            )
        except SocialResolverError as exc:
            failed_resolver = str(exc.metadata.get("resolver") or resolver_name)
            self._repository.insert_event(
                job.id,
                event_type=f"{failed_resolver}_resolver_failed",
                message=f"{failed_resolver} resolver failed.",
                metadata={
                    "platform": platform,
                    "resolver": failed_resolver,
                    "error_code": exc.code,
                    "exception_type": type(exc).__name__,
                    "elapsed_ms": int((perf_counter() - resolver_started_at) * 1000),
                    **safe_event_metadata(exc.metadata),
                },
            )
            raise PipelineFailure(exc.code, exc.message, {"platform": platform, **exc.metadata}) from exc
        self._repository.insert_event(
            job.id,
            event_type=f"{info.resolver}_resolver_completed",
            message=f"{info.resolver} resolver completed.",
            metadata={
                **_social_metadata(info),
                "elapsed_ms": int((perf_counter() - resolver_started_at) * 1000),
            },
        )
        if (
            info.duration_seconds is not None
            and info.duration_seconds > settings.max_video_duration_seconds
        ):
            raise PipelineFailure(
                "media_duration_too_long",
                "This video is too long for automatic recipe analysis.",
                {
                    "platform": platform,
                    "resolver": info.resolver,
                    "duration_seconds": info.duration_seconds,
                    "max_video_duration_seconds": settings.max_video_duration_seconds,
                },
            )
        self._update_social_metadata_best_effort(job, info)

        job = self._repository.update_job(job.id, status="downloading", progress=35, error_code=None, error_message=None)
        self._repository.insert_event(job.id, event_type="social_stream_started", message="Social video stream started.", metadata={"platform": platform, "resolver": info.resolver, "ext": info.ext})
        video: VideoBuffer | None = None
        try:
            try:
                video = stream_video_to_memory(
                    info,
                    settings,
                    max_size_mb=(
                        settings.tikwm_max_video_size_mb
                        if info.resolver == "tikwm"
                        else None
                    ),
                )
            except SocialResolverError as exc:
                self._repository.insert_event(
                    job.id,
                    event_type="social_stream_failed",
                    message="Social video stream failed.",
                    metadata={
                        "platform": platform,
                        "resolver": info.resolver,
                        "error_code": exc.code,
                        "ext": info.ext,
                        **safe_event_metadata(exc.metadata),
                    },
                )
                raise PipelineFailure(
                    exc.code,
                    exc.message,
                    {"platform": platform, **exc.metadata},
                ) from exc
            self._repository.insert_event(job.id, event_type="social_stream_completed", message="Social video stream completed.", metadata={"platform": platform, "resolver": info.resolver, "size_bytes": video.size_bytes, "mime_type": video.mime_type, "ext": info.ext})

            job = self._repository.update_job(job.id, status="thumbnailing", progress=45, error_code=None, error_message=None)
            thumbnail_url = self._upload_social_thumbnail_best_effort(job, info.thumbnail_url, settings)

            self._filter_model_candidates(job.id)
            job = self._repository.update_job(job.id, status="analyzing", progress=60, thumbnail_url=thumbnail_url, error_code=None, error_message=None, ai_provider="gemini", ai_model=self._analyzer.model)
            self._repository.insert_event(job.id, event_type="gemini_inline_video_started", message="Gemini inline video analysis started.", metadata={"model": self._analyzer.model, "stage": "video_observation", "mime_type": video.mime_type, "size_bytes": video.size_bytes})
            self._repository.insert_event(job.id, event_type="video_observation_started", message="Gemini video observation started.", metadata={"model": self._analyzer.model, "mime_type": video.mime_type, "size_bytes": video.size_bytes})
            try:
                outcome = self._analyzer.analyze_video_bytes(
                    video.buffer.getvalue(),
                    video.mime_type,
                    on_event=lambda event_type, metadata: self._repository.insert_event(job.id, event_type=event_type, message=event_type.replace("_", " ").capitalize() + ".", metadata=metadata),
                    before_call=lambda model, stage: self._start_gemini_api_call(job.id, model=model, stage=stage),
                )
            except GeminiCallGuardError as exc:
                raise PipelineFailure(exc.code, exc.message, exc.metadata) from exc
            except GeminiAnalysisError as exc:
                raise PipelineFailure(exc.code, gemini_user_message(exc.code), {"stage": "gemini_inline_video", "attempted_models": exc.attempted_models, "final_model": exc.final_model, "attempts_count": exc.attempts_count, "provider_error_type": exc.provider_error_type, "provider_error_message": exc.provider_error_message}) from exc
            self._repository.insert_event(job.id, event_type="gemini_inline_video_completed", message="Gemini inline video analysis completed.", metadata={"model": outcome.model, "stage": "recipe_normalization"})

            job = self._repository.update_job(job.id, status="saving_result", progress=85, error_code=None, error_message=None)
            if job.recipe_item_id is None:
                raise PipelineFailure("recipe_item_required", "A saved recipe is required before analysis can be completed.")
            recipe_card_id = self._repository.insert_recipe_card(job=job, result=outcome.result, model=outcome.model)
            self._repository.insert_event(job.id, event_type="recipe_card_saved", message="Recipe card saved.", metadata={"ingredients_count": len(outcome.result.ingredients), "steps_count": len(outcome.result.steps)})
            job = self._repository.update_job(job.id, status="completed", progress=100, result_recipe_card_id=recipe_card_id, ai_provider="gemini", ai_model=outcome.model, thumbnail_url=thumbnail_url, completed_at=datetime.now(timezone.utc).isoformat(), error_code=None, error_message=None)
            self._repository.insert_event(job.id, event_type="completed", message="Social video recipe analysis completed.", metadata={"platform": platform, "model": outcome.model, "status": "completed"})
            return job
        finally:
            if video is not None:
                video.buffer.close()

    def _upload_social_thumbnail_best_effort(self, job: MediaJobRecord, thumbnail_url: str | None, settings: Settings) -> str | None:
        self._repository.insert_event(job.id, event_type="thumbnail_upload_started", message="Thumbnail upload started.", metadata={"thumbnail_uploaded": False})
        try:
            public_url = upload_source_thumbnail(thumbnail_url, user_id=job.user_id, media_job_id=job.id, repository=self._repository, settings=settings)
            if public_url:
                self._repository.insert_event(job.id, event_type="thumbnail_uploaded", message="Thumbnail uploaded.", metadata={"thumbnail_uploaded": True})
                self._update_recipe_thumbnail_best_effort(job, public_url)
            return public_url
        except Exception as exc:
            self._repository.insert_event(job.id, event_type="thumbnail_upload_failed", message="Thumbnail upload failed.", metadata={"thumbnail_uploaded": False, "error_code": "thumbnail_upload_failed", "exception_type": type(exc).__name__})
            return None

    def _update_social_metadata_best_effort(self, job: MediaJobRecord, info: SocialVideoInfo) -> None:
        values = {
            "media_duration_seconds": int(info.duration_seconds) if info.duration_seconds is not None else None,
            "media_size_bytes": info.filesize_approx,
            "source_title": info.title,
            "source_thumbnail_url": info.thumbnail_url,
            "thumbnail_url": info.thumbnail_url,
            "resolver": info.resolver,
        }
        for key, value in values.items():
            if value is None:
                continue
            try:
                self._repository.update_job_metadata_best_effort(job.id, {key: value})
            except Exception as exc:
                logger.warning("social_metadata_update_failed", extra={"media_job_id": str(job.id), "exception_type": type(exc).__name__})

    def _run_youtube(self, job: MediaJobRecord) -> MediaJobRecord:
        media_job_id = job.id
        video_id = extract_youtube_video_id(job.source_url)
        if video_id is None:
            raise PipelineFailure(
                "invalid_youtube_url",
                "The YouTube link is invalid or unsupported.",
            )

        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        thumbnail_url = f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
        self._repository.insert_event(
            media_job_id,
            event_type="youtube_detected",
            message="Public YouTube source detected.",
            metadata={"platform": "youtube"},
        )

        job = self._repository.update_job(
            media_job_id,
            status="resolving",
            progress=10,
            thumbnail_url=thumbnail_url,
            error_code=None,
            error_message=None,
        )
        self._update_recipe_thumbnail_best_effort(job, thumbnail_url)
        self._repository.insert_event(
            media_job_id,
            event_type="thumbnail_resolved",
            message="YouTube thumbnail resolved.",
            metadata={"platform": "youtube"},
        )

        if self._analyzer is None:
            raise PipelineFailure(
                "gemini_not_configured",
                "Gemini video analysis is not configured yet.",
            )

        self._filter_model_candidates(media_job_id)
        job = self._repository.update_job(
            media_job_id,
            status="analyzing",
            progress=60,
            error_code=None,
            error_message=None,
            ai_provider="gemini",
            ai_model=self._analyzer.model,
        )
        self._repository.insert_event(
            media_job_id,
            event_type="gemini_analysis_started",
            message="Gemini video analysis started.",
            metadata={"model": self._analyzer.model, "status": "analyzing"},
        )
        self._repository.insert_event(
            media_job_id,
            event_type="video_observation_started",
            message="Gemini video observation started.",
            metadata={"model": self._analyzer.model},
        )

        try:
            outcome = self._analyzer.analyze(
                canonical_url,
                on_event=lambda event_type, metadata: self._repository.insert_event(
                    media_job_id,
                    event_type=event_type,
                    message=event_type.replace("_", " ").capitalize() + ".",
                    metadata=metadata,
                ),
                before_call=lambda model, stage: self._start_gemini_api_call(
                    media_job_id,
                    model=model,
                    stage=stage,
                ),
            )
            result = outcome.result
        except GeminiCallGuardError as exc:
            raise PipelineFailure(exc.code, exc.message, exc.metadata) from exc
        except GeminiAnalysisError as exc:
            raise PipelineFailure(
                exc.code,
                gemini_user_message(exc.code),
                {
                    "stage": "gemini_analysis",
                    "attempted_models": exc.attempted_models,
                    "final_model": exc.final_model,
                    "attempts_count": exc.attempts_count,
                    "provider_error_type": exc.provider_error_type,
                    "provider_error_message": exc.provider_error_message,
                },
            ) from exc

        self._repository.insert_event(
            media_job_id,
            event_type="gemini_analysis_completed",
            message="Gemini video analysis completed.",
            metadata={
                "model": outcome.model,
                "ingredients_count": len(result.ingredients),
                "steps_count": len(result.steps),
            },
        )

        job = self._repository.update_job(
            media_job_id,
            status="saving_result",
            progress=85,
            error_code=None,
            error_message=None,
        )
        if job.recipe_item_id is None:
            raise PipelineFailure(
                "recipe_item_required",
                "A saved recipe is required before analysis can be completed.",
            )

        try:
            recipe_card_id = self._repository.insert_recipe_card(
                job=job,
                result=result,
                model=outcome.model,
            )
        except Exception as exc:
            raise PipelineFailure(
                "recipe_card_insert_failed",
                "Recipe analysis completed, but the recipe card could not be saved.",
            ) from exc
        self._repository.insert_event(
            media_job_id,
            event_type="recipe_card_saved",
            message="Recipe card saved.",
            metadata={
                "ingredients_count": len(result.ingredients),
                "steps_count": len(result.steps),
            },
        )

        job = self._repository.update_job(
            media_job_id,
            status="completed",
            progress=100,
            result_recipe_card_id=recipe_card_id,
            ai_provider="gemini",
            ai_model=outcome.model,
            thumbnail_url=thumbnail_url,
            completed_at=datetime.now(timezone.utc).isoformat(),
            error_code=None,
            error_message=None,
        )
        self._repository.insert_event(
            media_job_id,
            event_type="completed",
            message="YouTube recipe analysis completed.",
            metadata={
                "platform": "youtube",
                "model": outcome.model,
                "status": "completed",
                "ingredients_count": len(result.ingredients),
                "steps_count": len(result.steps),
            },
        )
        return job

    def _start_gemini_api_call(
        self,
        media_job_id: UUID,
        *,
        model: str,
        stage: str,
    ) -> dict[str, object]:
        job_count = self._repository.count_job_gemini_calls(media_job_id)
        if job_count >= self._gemini_max_calls_per_job:
            raise GeminiCallGuardError(
                "gemini_job_call_limit_reached",
                "Gemini call limit reached for this job. Try again later.",
                {
                    "error_code": "gemini_job_call_limit_reached",
                    "call_count": job_count,
                    "max_calls_per_job": self._gemini_max_calls_per_job,
                },
            )

        budget_start = self._budget_window_start()
        daily_count = self._repository.count_daily_gemini_calls(budget_start)
        if (
            self._app_env == "development"
            and daily_count >= self._gemini_daily_dev_call_limit
        ):
            raise GeminiCallGuardError(
                "gemini_daily_dev_limit_reached",
                (
                    "Daily development Gemini limit reached. "
                    "Try again after quota resets."
                ),
                {
                    "error_code": "gemini_daily_dev_limit_reached",
                    "call_count": daily_count,
                    "daily_dev_limit": self._gemini_daily_dev_call_limit,
                },
            )

        metadata: dict[str, object] = {
            "model": model,
            "stage": stage,
            "job_call_number": job_count + 1,
            "model_daily_count": self._repository.count_daily_gemini_calls(
                budget_start,
                model=model,
            ),
            "model_daily_limit": self._gemini_daily_dev_model_limits.get(model),
            "global_daily_count": daily_count,
            "max_calls_per_job": self._gemini_max_calls_per_job,
            "global_daily_limit": self._gemini_daily_dev_call_limit,
        }
        self._repository.insert_event(
            media_job_id,
            event_type="gemini_api_call_started",
            message="Gemini API call started.",
            metadata=metadata,
        )
        return metadata

    def _filter_model_candidates(self, media_job_id: UUID) -> None:
        if self._analyzer is None:
            return
        available: list[str] = []
        skipped: list[dict[str, object]] = []
        budget_start = self._budget_window_start()
        for model in self._analyzer.models:
            if model in self._gemini_disabled_models:
                skipped.append({"model": model, "reason": "disabled"})
                continue
            limit = self._gemini_daily_dev_model_limits.get(model)
            count = self._repository.count_daily_gemini_calls(
                budget_start,
                model=model,
            )
            if self._app_env == "development" and limit is not None and count >= limit:
                metadata = {
                    "model": model,
                    "reason": "model_daily_dev_limit_reached",
                    "count": count,
                    "limit": limit,
                }
                skipped.append(metadata)
                self._repository.insert_event(
                    media_job_id,
                    event_type="gemini_model_skipped",
                    message=(
                        "Gemini model skipped because development model "
                        "budget is reached."
                    ),
                    metadata=metadata,
                )
                continue
            available.append(model)
        self._analyzer.models = available
        if not available:
            raise PipelineFailure(
                "gemini_no_model_budget_available",
                (
                    "No Gemini model budget is available for this job. "
                    "Try again later or change model settings."
                ),
                {"stage": "gemini_analysis", "model_budgets": skipped},
            )

    def _budget_window_start(self) -> datetime:
        if self._gemini_dev_budget_reset_at:
            try:
                return datetime.fromisoformat(
                    self._gemini_dev_budget_reset_at.replace("Z", "+00:00")
                )
            except ValueError:
                pass
        return datetime.now(timezone.utc).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

    def _run_skeleton(self, job: MediaJobRecord) -> MediaJobRecord:
        stages = (
            ("resolving", 10, "Media worker started resolving the job."),
            ("validating", 30, "Media worker started validating the job."),
            ("needs_caption", 100, SKELETON_ERROR_MESSAGE),
        )
        for status, progress, message in stages:
            is_terminal = status == "needs_caption"
            job = self._repository.update_job(
                job.id,
                status=status,
                progress=progress,
                error_code=SKELETON_ERROR_CODE if is_terminal else None,
                error_message=SKELETON_ERROR_MESSAGE if is_terminal else None,
            )
            self._repository.insert_event(
                job.id,
                event_type=status,
                message=message,
                metadata={
                    "platform": job.platform,
                    "status": status,
                    "progress": progress,
                },
            )
        return job

    def _update_recipe_thumbnail_best_effort(
        self,
        job: MediaJobRecord,
        thumbnail_url: str,
    ) -> None:
        if job.recipe_item_id is None:
            return
        try:
            self._repository.update_recipe_thumbnail(
                recipe_item_id=job.recipe_item_id,
                user_id=job.user_id,
                thumbnail_url=thumbnail_url,
            )
        except Exception as exc:
            logger.warning(
                "recipe_thumbnail_update_failed",
                extra={
                    "media_job_id": str(job.id),
                    "exception_type": type(exc).__name__,
                },
            )

    def _record_failure(
        self,
        media_job_id: UUID,
        *,
        error_code: str,
        error_message: str,
        exception_type: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        try:
            self._repository.update_job(
                media_job_id,
                status="failed",
                progress=100,
                error_code=error_code,
                error_message=error_message,
                ai_model=(
                    str(metadata["final_model"])
                    if metadata and metadata.get("final_model")
                    else None
                ),
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
                message=error_message,
                metadata={
                    "error_code": error_code,
                    "exception_type": exception_type,
                    **(metadata or {}),
                },
            )
        except Exception as recovery_exc:
            self._log_recovery_failure(
                media_job_id,
                "worker_failure_event_insert_failed",
                recovery_exc,
            )

    @staticmethod
    def _log_job_failure(media_job_id: UUID, exception_type: str) -> None:
        logger.error(
            "worker_job_failed",
            extra={
                "media_job_id": str(media_job_id),
                "exception_type": exception_type,
            },
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
            error_code=job.error_code,
            accepted=False,
        )


def extract_youtube_video_id(source_url: str) -> str | None:
    try:
        parsed = urlparse(source_url)
    except ValueError:
        return None

    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        return None

    host = (parsed.hostname or "").lower().rstrip(".")
    video_id: str | None = None

    if host in {"youtu.be", "www.youtu.be"}:
        video_id = first_path_segment(parsed.path)
    elif host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
    }:
        if parsed.path.rstrip("/") == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        elif parsed.path.startswith("/shorts/"):
            video_id = first_path_segment(parsed.path.removeprefix("/shorts/"))

    if video_id and YOUTUBE_VIDEO_ID_PATTERN.fullmatch(video_id):
        return video_id
    return None


def first_path_segment(path: str) -> str | None:
    segment = path.strip("/").split("/", maxsplit=1)[0]
    return segment or None


def gemini_user_message(error_code: str) -> str:
    if error_code == "gemini_model_overloaded":
        return (
            "Gemini video analysis is temporarily busy. "
            "Please try again in a few minutes."
        )
    if error_code == "gemini_rate_limited":
        return "Video analysis limit was reached. Please try again later."
    if error_code == "gemini_youtube_unavailable":
        return "This YouTube video could not be accessed for analysis."
    if error_code == "gemini_invalid_json":
        return GEMINI_INVALID_JSON_MESSAGE
    if error_code == "gemini_empty_observation":
        return "Gemini could not extract useful recipe details from this video."
    if error_code == "gemini_inline_video_not_supported":
        return "Inline video analysis is not supported by the current worker configuration."
    if error_code == "gemini_video_bytes_failed":
        return "Social video analysis failed. Please try again."
    return "Video recipe analysis failed. Please try again."


def _social_metadata(info: SocialVideoInfo) -> dict[str, object]:
    return {
        "platform": info.platform,
        "resolver": info.resolver,
        "video_id_present": bool(info.video_id),
        "title_present": bool(info.title),
        "duration_seconds": info.duration_seconds,
        "has_thumbnail": bool(info.thumbnail_url),
        "ext": info.ext,
        "mime_type": info.mime_type,
        "has_stream_headers": bool(info.stream_headers),
    }


def expected_resolver_for_platform(platform: str) -> str:
    if platform == "tiktok":
        return "tikwm"
    if platform in {"instagram", "facebook"}:
        return "cobalt"
    return "social"


def safe_event_metadata(metadata: dict[str, object]) -> dict[str, object]:
    forbidden = {"stream_url", "source_url", "url", "cookies", "cookie", "authorization"}
    return {
        key: value
        for key, value in metadata.items()
        if key.lower() not in forbidden
    }
