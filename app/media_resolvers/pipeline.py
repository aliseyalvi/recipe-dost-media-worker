from __future__ import annotations

import time
import logging
from collections import Counter
from pathlib import Path
from typing import Protocol
from uuid import UUID

from app.config import Settings
from app.media_resolvers.circuit_breaker import provider_circuit_breaker
from app.media_resolvers.contracts import MediaResolutionRequest, ResolverError
from app.media_resolvers.media_fetch import SecureMediaDownloader, cleanup_job_directory
from app.media_resolvers.media_processing import (
    mux_video_audio,
    probe_media,
    validate_final_media,
)
from app.media_resolvers.registry import build_resolver
from app.media_resolvers.selection import (
    evaluate_video_candidates,
    select_audio_candidate,
    select_video_candidate,
)

logger = logging.getLogger("recipe_dost.worker")


class EventRepository(Protocol):
    def insert_event(
        self,
        media_job_id: UUID,
        *,
        event_type: str,
        message: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        ...

    def has_external_resolver_attempt(self, media_job_id: UUID, adapter_id: str) -> bool:
        ...


def resolve_external_media(
    *,
    repository: EventRepository,
    settings: Settings,
    media_job_id: UUID,
    source_url: str,
    platform: str,
    audience_route: str,
) -> dict[str, object]:
    chain = (
        settings.instagram_resolver_chain
        if platform == "instagram"
        else settings.tiktok_resolver_chain
        if platform == "tiktok"
        else []
    )
    if not chain:
        raise ResolverError(
            "resolver_configuration_error",
            "External media resolution is not configured for this platform.",
            technical=True,
        )
    request = MediaResolutionRequest(
        platform=platform,
        canonical_source_url=source_url,
        correlation_id=str(media_job_id),
        capability="video_with_audio",
        byte_limit=settings.max_video_size_bytes,
        preferred_byte_target=settings.preferred_video_size_bytes,
        timeout_seconds=settings.external_provider_timeout_seconds,
    )
    last_error: ResolverError | None = None
    for attempt, adapter_id in enumerate(chain[:2], start=1):
        fallback_used = attempt > 1
        if not provider_circuit_breaker.allows(adapter_id):
            last_error = ResolverError(
                "resolver_temporarily_unavailable",
                "The media resolver is temporarily unavailable.",
                retryable=True,
                fallback_allowed=True,
            )
            last_error.adapter_id = adapter_id
            continue
        if repository.has_external_resolver_attempt(media_job_id, adapter_id):
            last_error = ResolverError(
                "resolver_temporarily_unavailable",
                "A resolver attempt was already recorded for this job.",
                technical=True,
            )
            break
        base_event = {
            "platform": platform,
            "resolver_adapter": adapter_id,
            "route_audience": audience_route,
            "provider_attempt": attempt,
            "fallback_used": fallback_used,
        }
        repository.insert_event(
            media_job_id,
            event_type="external_resolver_started",
            message="External media resolver started.",
            metadata=base_event,
        )
        logger.info("external_resolver_started", extra=base_event)
        started = time.monotonic()
        job_directory: Path | None = None
        tiktok_diagnostics_emitted = False
        try:
            resolution = build_resolver(adapter_id, settings).resolve(request)
            if platform == "tiktok":
                _emit_tiktok_contract_diagnostic(
                    repository,
                    media_job_id,
                    base_event,
                    resolution.contract_diagnostic,
                )
            repository.insert_event(
                media_job_id,
                event_type="external_resolver_completed",
                message="External media resolver completed.",
                metadata={
                    **base_event,
                    "media_mode": resolution.media_mode,
                    "external_resolution_latency_bucket": _latency_bucket(started),
                    "candidate_count": len(resolution.candidates),
                },
            )
            logger.info(
                "external_resolver_completed",
                extra={
                    **base_event,
                    "media_mode": resolution.media_mode,
                    "latency_bucket": _latency_bucket(started),
                },
            )
            provider_circuit_breaker.record_success(adapter_id)
            suffixes = tuple(
                settings.instagram_media_host_suffixes
                if platform == "instagram"
                else settings.tiktok_media_host_suffixes
            )
            eligible_videos, selection_rejections = evaluate_video_candidates(
                resolution.candidates,
                hard_limit_bytes=settings.max_video_size_bytes,
                max_short_edge=settings.max_video_height,
                allowed_host_suffixes=suffixes,
            )
            if platform == "tiktok":
                combined_rejections = Counter(resolution.normalization_rejection_counts)
                combined_rejections.update(selection_rejections)
                _emit_tiktok_candidate_summary(
                    repository,
                    media_job_id,
                    base_event,
                    raw_video_candidates=resolution.raw_video_candidate_count,
                    eligible_video_candidates=len(eligible_videos),
                    raw_audio_candidates=resolution.raw_audio_candidate_count,
                    eligible_audio_candidates=sum(
                        1 for candidate in resolution.candidates if candidate.media_type == "audio"
                    ),
                    rejection_counts=dict(combined_rejections),
                )
                tiktok_diagnostics_emitted = True
            video = select_video_candidate(
                resolution.candidates,
                preferred_bytes=settings.preferred_video_size_bytes,
                hard_limit_bytes=settings.max_video_size_bytes,
                max_short_edge=settings.max_video_height,
                allowed_host_suffixes=suffixes,
            )
            repository.insert_event(
                media_job_id,
                event_type="media_candidate_selected",
                message="External media candidate selected.",
                metadata={**base_event, **_candidate_event(video), "media_mode": resolution.media_mode},
            )
            downloader = SecureMediaDownloader(
                temp_root=settings.temp_media_dir,
                allowed_host_suffixes=suffixes,
                byte_limit=settings.max_video_size_bytes,
                connect_timeout_seconds=settings.external_media_download_connect_timeout_seconds,
                read_timeout_seconds=settings.external_media_download_read_timeout_seconds,
                total_timeout_seconds=settings.external_media_download_total_timeout_seconds,
                max_redirects=settings.external_media_max_redirects,
            )
            job_directory = downloader.create_job_directory(str(media_job_id))
            repository.insert_event(
                media_job_id,
                event_type="external_media_download_started",
                message="External media download started.",
                metadata=base_event,
            )
            downloaded_video = downloader.download_candidate(
                video,
                job_directory=job_directory,
                filename="video-input.mp4",
            )
            repository.insert_event(
                media_job_id,
                event_type="external_media_download_completed",
                message="External media download completed.",
                metadata={**base_event, "size_bucket": _size_bucket(downloaded_video.size_bytes)},
            )
            probe = probe_media(
                downloaded_video.path,
                timeout_seconds=settings.ytdlp_extract_timeout_seconds,
            )
            repository.insert_event(
                media_job_id,
                event_type="media_probe_completed",
                message="External media probe completed.",
                metadata={
                    **base_event,
                    "media_validation_result": (
                        "video_with_audio" if probe.video_present and probe.audio_present else "video_only"
                    ),
                    "duration_bucket": _duration_bucket(probe.duration_seconds),
                },
            )
            final_path = downloaded_video.path
            if not probe.video_present:
                raise ResolverError(
                    "media_validation_failed",
                    "Downloaded media does not contain video.",
                    fallback_allowed=True,
                )
            if not probe.audio_present:
                remaining = settings.max_video_size_bytes - downloaded_video.size_bytes
                audio = select_audio_candidate(resolution.candidates, remaining_bytes=remaining)
                if audio is None:
                    raise ResolverError(
                        "media_has_no_audio",
                        "This video does not contain usable audio.",
                        fallback_allowed=True,
                    )
                downloaded_audio = downloader.download_candidate(
                    audio,
                    job_directory=job_directory,
                    filename="audio-input.m4a",
                    byte_limit=max(1, remaining),
                )
                repository.insert_event(
                    media_job_id,
                    event_type="media_merge_started",
                    message="External media merge started.",
                    metadata={**base_event, "media_mode": "split_av"},
                )
                final_path = mux_video_audio(
                    downloaded_video.path,
                    downloaded_audio.path,
                    output_path=job_directory / "validated-media.mp4",
                    timeout_seconds=min(settings.max_job_runtime_seconds, 120),
                    byte_limit=settings.max_video_size_bytes,
                )
                repository.insert_event(
                    media_job_id,
                    event_type="media_merge_completed",
                    message="External media merge completed.",
                    metadata={**base_event, "media_mode": "split_av"},
                )
                downloaded_video.path.unlink(missing_ok=True)
                downloaded_audio.path.unlink(missing_ok=True)
            final_probe = validate_final_media(
                final_path,
                byte_limit=settings.max_video_size_bytes,
                duration_limit_seconds=settings.max_video_duration_seconds,
                timeout_seconds=settings.ytdlp_extract_timeout_seconds,
            )
            final_size = final_path.stat().st_size
            repository.insert_event(
                media_job_id,
                event_type="external_media_validated",
                message="External media validated.",
                metadata={
                    **base_event,
                    "media_mode": "split_av" if final_path.name == "validated-media.mp4" else "progressive",
                    "media_validation_result": "video_with_audio",
                    "size_bucket": _size_bucket(final_size),
                    "duration_bucket": _duration_bucket(final_probe.duration_seconds),
                    "resolution_bucket": _resolution_bucket(final_probe.width, final_probe.height),
                },
            )
            logger.info(
                "external_media_validated",
                extra={
                    **base_event,
                    "media_mode": (
                        "split_av" if final_path.name == "validated-media.mp4" else "progressive"
                    ),
                },
            )
            source_context = resolution.source_context.to_prompt_dict()
            if platform == "tiktok":
                safe_context = resolution.source_context.safe_to_dict()
                repository.insert_event(
                    media_job_id,
                    event_type="gemini_source_context_prepared",
                    message="Provider source context prepared for Gemini.",
                    metadata={
                        **base_event,
                        **safe_context,
                        "caption_length_bucket": _text_length_bucket(
                            int(safe_context.get("caption_length") or 0)
                        ),
                    },
                )
            return {
                "success": True,
                "platform": platform,
                "resolver": adapter_id,
                "resolution_mode": "local_file_ready",
                "output_mode": "temp_file",
                "temp_file_path": str(final_path),
                "video_bytes_verified": True,
                "size_bytes": final_size,
                "duration_seconds": final_probe.duration_seconds or resolution.metadata.duration_seconds,
                "width": final_probe.width,
                "height": final_probe.height,
                "mime_type": "video/mp4",
                "title": resolution.metadata.title,
                "author_username": resolution.metadata.author,
                "thumbnail_url": resolution.metadata.thumbnail_url,
                "thumbnail_source": f"{platform}_external_provider",
                "thumbnail_provider": adapter_id,
                "thumbnail_strategy": "external_provider_then_upload",
                "thumbnail_candidate_count": int(bool(resolution.metadata.thumbnail_url)),
                "thumbnail_selected": bool(resolution.metadata.thumbnail_url),
                "cookie_mode": "none",
                "provider_attempt": attempt,
                "fallback_used": fallback_used,
                "source_context": source_context,
            }
        except ResolverError as exc:
            exc.adapter_id = adapter_id
            exc.provider_attempt = attempt
            last_error = exc
            if platform == "tiktok" and not tiktok_diagnostics_emitted:
                _emit_tiktok_contract_diagnostic(
                    repository,
                    media_job_id,
                    base_event,
                    getattr(exc, "contract_diagnostic", {}),
                )
                if (
                    getattr(exc, "raw_video_candidate_count", 0)
                    or getattr(exc, "raw_audio_candidate_count", 0)
                    or getattr(exc, "normalization_rejection_counts", {})
                ):
                    _emit_tiktok_candidate_summary(
                        repository,
                        media_job_id,
                        base_event,
                        raw_video_candidates=getattr(exc, "raw_video_candidate_count", 0),
                        eligible_video_candidates=0,
                        raw_audio_candidates=getattr(exc, "raw_audio_candidate_count", 0),
                        eligible_audio_candidates=0,
                        rejection_counts=getattr(
                            exc, "normalization_rejection_counts", {}
                        ),
                    )
            if exc.code in {
                "resolver_rate_limited",
                "resolver_temporarily_unavailable",
                "media_download_failed",
                "media_validation_failed",
                "media_merge_failed",
                "resolver_configuration_error",
                "resolver_contract_error",
            }:
                provider_circuit_breaker.record_failure(
                    adapter_id,
                    immediate=exc.code == "resolver_configuration_error",
                )
            cleanup_job_directory(job_directory)
            repository.insert_event(
                media_job_id,
                event_type="external_resolver_failed",
                message="External media resolver failed.",
                metadata={
                    **base_event,
                    "normalized_error_code": exc.code,
                    "provider_status_class": exc.http_status_class,
                    "terminal": not (exc.fallback_allowed and attempt < len(chain[:2])),
                },
            )
            logger.warning(
                "external_resolver_failed",
                extra={
                    **base_event,
                    "error_code": exc.code,
                    "provider_status_class": exc.http_status_class or "unknown",
                    "terminal": not (exc.fallback_allowed and attempt < len(chain[:2])),
                },
            )
            if not exc.fallback_allowed or attempt >= len(chain[:2]):
                break
    raise last_error or ResolverError(
        "resolver_temporarily_unavailable",
        "External media resolution failed.",
        retryable=True,
    )


def _emit_tiktok_contract_diagnostic(
    repository: EventRepository,
    media_job_id: UUID,
    base_event: dict[str, object],
    diagnostic: dict[str, object],
) -> None:
    if not diagnostic:
        return
    repository.insert_event(
        media_job_id,
        event_type="tiktok_provider_contract_inspected",
        message="TikTok provider contract shape inspected.",
        metadata={**base_event, **diagnostic},
    )
    logger.info(
        "tiktok_provider_contract_inspected",
        extra={**base_event, **diagnostic},
    )


def _emit_tiktok_candidate_summary(
    repository: EventRepository,
    media_job_id: UUID,
    base_event: dict[str, object],
    *,
    raw_video_candidates: int,
    eligible_video_candidates: int,
    raw_audio_candidates: int,
    eligible_audio_candidates: int,
    rejection_counts: dict[str, int],
) -> None:
    safe_rejections = {
        key: max(0, int(value))
        for key, value in rejection_counts.items()
        if key
        in {
            "missing_play_addr",
            "empty_url_list",
            "invalid_url_type",
            "non_https_url",
            "disallowed_initial_host",
            "unsupported_format",
            "unsupported_codec",
            "invalid_size",
            "exceeds_hard_limit",
            "invalid_dimensions",
            "below_minimum_resolution",
            "exceeds_resolution_limit",
            "audio_state_unacceptable",
            "malformed_candidate",
        }
    }
    metadata = {
        **base_event,
        "raw_video_candidates": max(0, raw_video_candidates),
        "eligible_video_candidates": max(0, eligible_video_candidates),
        "raw_audio_candidates": max(0, raw_audio_candidates),
        "eligible_audio_candidates": max(0, eligible_audio_candidates),
        "rejection_counts": safe_rejections,
    }
    repository.insert_event(
        media_job_id,
        event_type="tiktok_candidate_normalization_summary",
        message="TikTok media candidates normalized.",
        metadata=metadata,
    )
    logger.info("tiktok_candidate_normalization_summary", extra=metadata)


def _candidate_event(candidate) -> dict[str, object]:
    return {
        "quality_bucket": candidate.quality_label or "unknown",
        "resolution_bucket": _resolution_bucket(candidate.width, candidate.height),
        "size_bucket": _size_bucket(candidate.size_bytes),
        "audio_state": candidate.audio_state,
    }


def _latency_bucket(started: float) -> str:
    elapsed = time.monotonic() - started
    if elapsed < 1:
        return "under_1s"
    if elapsed < 5:
        return "1_5s"
    if elapsed < 15:
        return "5_15s"
    return "15s_or_more"


def _size_bucket(value: int | None) -> str:
    if value is None:
        return "unknown"
    mib = value / (1024 * 1024)
    if mib < 20:
        return "under_20_mib"
    if mib < 32:
        return "20_32_mib"
    if mib < 50:
        return "32_50_mib"
    return "50_mib_or_more"


def _duration_bucket(value: int | None) -> str:
    if value is None:
        return "unknown"
    if value <= 60:
        return "up_to_60_seconds"
    if value <= 120:
        return "60_120_seconds"
    return "over_120_seconds"


def _text_length_bucket(value: int) -> str:
    if value <= 0:
        return "none"
    if value <= 250:
        return "up_to_250_chars"
    if value <= 750:
        return "251_750_chars"
    return "751_1500_chars"


def _resolution_bucket(width: int | None, height: int | None) -> str:
    if not width and not height:
        return "unknown"
    short_edge = min(value for value in (width, height) if value)
    if short_edge >= 720:
        return "720p"
    if short_edge >= 540:
        return "540p"
    if short_edge >= 360:
        return "360_539p"
    return "under_360p"
