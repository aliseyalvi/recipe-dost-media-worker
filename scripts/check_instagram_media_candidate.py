from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import Settings
from app.media_resolvers.contracts import ResolverError
from app.media_resolvers.media_fetch import cleanup_job_directory
from app.media_resolvers.pipeline import resolve_external_media

OUTPUT_KEYS = (
    "provider_status_class",
    "success",
    "media_type",
    "contract_complete",
    "video_candidate_count",
    "audio_candidate_count",
    "thumbnail_candidate_count",
    "video_probe_valid",
    "embedded_audio_present",
    "separate_audio_required",
    "audio_probe_valid",
    "mux_required",
    "mux_succeeded",
    "final_video_present",
    "final_audio_present",
    "duration_within_limit",
    "size_within_limit",
    "cleanup_succeeded",
)


class DiagnosticRepository:
    def insert_event(
        self,
        media_job_id: UUID,
        *,
        event_type: str,
        message: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        del media_job_id, event_type, message, metadata

    def has_external_resolver_attempt(self, media_job_id: UUID, adapter_id: str) -> bool:
        del media_job_id, adapter_id
        return False


def main() -> int:
    summary = _empty_summary()
    temp_directory: Path | None = None
    exit_code = 1
    failure_stage: str | None = None
    try:
        _validate_runtime_guard()
        _validate_media_tools()
        credential = os.environ.get("INSTAGRAM_RAPIDAPI_KEY", "").strip()
        if not credential:
            raise ResolverError(
                "resolver_configuration_error",
                "Development Instagram credential is not configured.",
                failure_stage="input_validation",
            )
        source_url = getpass.getpass("Instagram URL (hidden): ").strip()
        if not source_url:
            raise ResolverError(
                "media_unavailable",
                "An Instagram URL is required.",
                failure_stage="input_validation",
            )
        settings = _diagnostic_settings(credential)
        result = resolve_external_media(
            repository=DiagnosticRepository(),
            settings=settings,
            media_job_id=uuid4(),
            source_url=source_url,
            platform="instagram",
            audience_route="development_diagnostic",
        )
        raw_path = str(result.get("temp_file_path") or "")
        final_path = Path(raw_path)
        temp_root = Path(settings.temp_media_dir).resolve()
        resolved_path = final_path.resolve()
        if (
            not raw_path
            or not resolved_path.is_relative_to(temp_root)
            or not resolved_path.parent.name.startswith("external-")
        ):
            raise ResolverError(
                "media_validation_failed",
                "The diagnostic temporary media location was rejected.",
                technical=True,
                failure_stage="final_media_validation",
            )
        temp_directory = final_path.parent
        _populate_summary(summary, result)
        exit_code = 0 if _ready_for_gemini(summary, final_path) else 1
        if exit_code:
            failure_stage = "final_media_validation"
    except ResolverError as exc:
        failure_stage = _safe_failure_stage(exc.failure_stage)
        _populate_error_summary(summary, exc)
    except KeyboardInterrupt:
        failure_stage = "cleanup"
        exit_code = 130
    except Exception:
        failure_stage = "final_media_validation"
    finally:
        if temp_directory is not None:
            cleanup_job_directory(temp_directory)
            summary["cleanup_succeeded"] = not temp_directory.exists()
        else:
            summary["cleanup_succeeded"] = True
        if not summary["cleanup_succeeded"]:
            failure_stage = "cleanup"
            exit_code = 1
        if failure_stage:
            print(f"failure_stage={failure_stage}")
        for key in OUTPUT_KEYS:
            print(f"{key}={_safe_scalar(summary[key])}")
    return exit_code


def _validate_runtime_guard() -> None:
    if os.environ.get("APP_ENV", "").strip().lower() != "development":
        raise ResolverError(
            "resolver_configuration_error",
            "Development environment is required.",
            failure_stage="input_validation",
        )
    if os.environ.get("ALLOW_LIVE_INSTAGRAM_DIAGNOSTIC", "").strip().lower() != "true":
        raise ResolverError(
            "resolver_configuration_error",
            "Explicit live diagnostic opt-in is required.",
            failure_stage="input_validation",
        )
    if _git_branch() != "develop":
        raise ResolverError(
            "resolver_configuration_error",
            "The diagnostic is allowed only on develop.",
            failure_stage="input_validation",
        )


def _validate_media_tools() -> None:
    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        raise ResolverError(
            "resolver_configuration_error",
            "Local ffprobe and ffmpeg are required.",
            technical=True,
            failure_stage="input_validation",
        )


def _diagnostic_settings(credential: str) -> Settings:
    return Settings(
        _env_file=None,
        instagram_resolver_chain=["rapidapi_instagram_unified_v1"],
        instagram_reels13_enabled=False,
        instagram_rapidapi_key=credential,
        tiktok_akng_access_token="diagnostic-unused",
        worker_api_secret="diagnostic-only",
        supabase_url="https://diagnostic.invalid",
        supabase_service_role_key="diagnostic-only",
        sentry_dsn=None,
    )


def _empty_summary() -> dict[str, object]:
    return {
        "provider_status_class": "unknown",
        "success": False,
        "media_type": "unknown",
        "contract_complete": False,
        "video_candidate_count": 0,
        "audio_candidate_count": 0,
        "thumbnail_candidate_count": 0,
        "video_probe_valid": False,
        "embedded_audio_present": False,
        "separate_audio_required": False,
        "audio_probe_valid": False,
        "mux_required": False,
        "mux_succeeded": False,
        "final_video_present": False,
        "final_audio_present": False,
        "duration_within_limit": False,
        "size_within_limit": False,
        "cleanup_succeeded": True,
    }


def _populate_summary(summary: dict[str, object], result: dict[str, object]) -> None:
    candidate_value = result.get("instagram_candidate_summary")
    candidate = candidate_value if isinstance(candidate_value, dict) else {}
    media_value = result.get("diagnostic_media")
    media = media_value if isinstance(media_value, dict) else {}
    summary.update(
        {
            "provider_status_class": result.get("provider_status_class") or "unknown",
            "success": bool(result.get("success")),
            "media_type": "video" if result.get("success") else "unknown",
            "contract_complete": bool(result.get("contract_complete")),
            "video_candidate_count": int(
                candidate.get("normalized_video_count")
                or candidate.get("raw_video_count")
                or 0
            ),
            "audio_candidate_count": int(
                candidate.get("normalized_audio_count")
                or candidate.get("raw_audio_count")
                or 0
            ),
            "thumbnail_candidate_count": int(
                candidate.get("normalized_thumbnail_count") or 0
            ),
            "video_probe_valid": bool(media.get("initial_probe_video_present")),
            "embedded_audio_present": bool(
                media.get("initial_probe_audio_present")
            ),
            "separate_audio_required": bool(media.get("separate_audio_required")),
            "audio_probe_valid": bool(media.get("audio_download_verified")),
            "mux_required": bool(media.get("separate_audio_required")),
            "mux_succeeded": bool(media.get("mux_succeeded")),
            "final_video_present": bool(media.get("final_video_present")),
            "final_audio_present": bool(media.get("final_audio_present")),
            "duration_within_limit": bool(result.get("success")),
            "size_within_limit": bool(result.get("success")),
        }
    )


def _populate_error_summary(
    summary: dict[str, object],
    exc: ResolverError,
) -> None:
    diagnostic = exc.contract_diagnostic
    summary.update(
        {
            "provider_status_class": (
                exc.http_status_class
                or diagnostic.get("provider_status_class")
                or "unknown"
            ),
            "success": False,
            "media_type": diagnostic.get("media_type") or "unknown",
            "contract_complete": bool(diagnostic.get("contract_complete")),
            "video_candidate_count": int(
                diagnostic.get("normalized_video_count") or 0
            ),
            "audio_candidate_count": int(
                diagnostic.get("normalized_audio_count") or 0
            ),
            "thumbnail_candidate_count": int(
                diagnostic.get("normalized_thumbnail_count") or 0
            ),
        }
    )


def _ready_for_gemini(summary: dict[str, object], final_path: Path) -> bool:
    return bool(
        summary["success"]
        and summary["media_type"] == "video"
        and summary["contract_complete"]
        and summary["video_probe_valid"]
        and summary["final_video_present"]
        and summary["final_audio_present"]
        and summary["duration_within_limit"]
        and summary["size_within_limit"]
        and final_path.is_file()
    )


def _safe_failure_stage(value: str | None) -> str:
    allowed = {
        "input_validation",
        "candidate_selection",
        "provider_http",
        "provider_contract",
        "media_download",
        "media_redirect_validation",
        "media_content_validation",
        "media_signature_validation",
        "ffprobe_video",
        "ffprobe_audio",
        "candidate_fallback",
        "final_media_validation",
        "cleanup",
    }
    return value if value in allowed else "final_media_validation"


def _safe_scalar(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    text = str(value or "none")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
    return text if len(text) <= 64 and set(text) <= allowed else "redacted"


def _git_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
