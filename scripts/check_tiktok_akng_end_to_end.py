from __future__ import annotations

import argparse
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

FAILURE_STAGES = {
    "input_validation",
    "short_link_dns_resolution",
    "short_link_http_request",
    "short_link_redirect_validation",
    "short_link_canonical_extraction",
    "numeric_id_extraction",
    "akng_request",
    "akng_json_parse",
    "akng_contract_validation",
    "candidate_normalization",
    "candidate_selection",
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

OUTPUT_KEYS = (
    "provider_call_attempted",
    "short_link_expansion_attempted",
    "short_link_expansion_completed",
    "canonical_url_resolved",
    "numeric_id_resolved",
    "provider_input_kind",
    "provider_status_class",
    "redirect_count",
    "contract_complete",
    "raw_video_candidate_count",
    "eligible_video_candidate_count",
    "selected_candidate",
    "selected_resolution_bucket",
    "selected_size_bucket",
    "selected_codec",
    "selected_container",
    "media_download_verified",
    "media_signature_valid",
    "ffprobe_video_present",
    "ffprobe_audio_present",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the opt-in development TikTok media verification gate."
    )
    parser.add_argument(
        "--input-kind",
        required=True,
        choices=("short_url", "canonical_url", "numeric_id"),
    )
    args = parser.parse_args(argv)
    summary = _empty_summary(args.input_kind)
    failure_stage: str | None = None
    temp_directory: Path | None = None
    exit_code = 1

    try:
        _validate_runtime_guard()
        _validate_media_tools()
        token = os.environ.get("TIKTOK_AKNG_ACCESS_TOKEN", "")
        if not token.strip():
            raise ResolverError(
                "resolver_configuration_error",
                "Development AKNG token is not configured.",
                failure_stage="input_validation",
            )
        source = getpass.getpass("TikTok input (hidden): ").strip()
        settings = _diagnostic_settings(token)
        result = resolve_external_media(
            repository=DiagnosticRepository(),
            settings=settings,
            media_job_id=uuid4(),
            source_url=source,
            source_input_kind=args.input_kind,
            platform="tiktok",
            audience_route="development_diagnostic",
        )
        raw_temp_path = str(result.get("temp_file_path") or "")
        if not raw_temp_path:
            raise ResolverError(
                "media_validation_failed",
                "The diagnostic did not produce temporary media.",
                failure_stage="final_media_validation",
            )
        final_path = Path(raw_temp_path)
        temp_root = Path(settings.temp_media_dir).resolve()
        resolved_final_path = final_path.resolve()
        if (
            not resolved_final_path.is_relative_to(temp_root)
            or not resolved_final_path.parent.name.startswith("external-")
        ):
            raise ResolverError(
                "media_validation_failed",
                "The diagnostic temporary media location was rejected.",
                technical=True,
                failure_stage="final_media_validation",
            )
        temp_directory = final_path.parent
        _populate_success_summary(summary, result)
        exit_code = 0 if _success_complete(summary, final_path) else 1
        if exit_code:
            failure_stage = "final_media_validation"
    except ResolverError as exc:
        failure_stage = _closed_failure_stage(exc.failure_stage)
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
        _print_summary(summary)
    return exit_code


def _validate_runtime_guard() -> None:
    if os.environ.get("APP_ENV", "").strip().lower() != "development":
        raise ResolverError(
            "resolver_configuration_error",
            "Development environment is required.",
            failure_stage="input_validation",
        )
    if os.environ.get("ALLOW_LIVE_TIKTOK_DIAGNOSTIC", "").strip().lower() != "true":
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
    if not shutil.which("ffprobe"):
        raise ResolverError(
            "resolver_configuration_error",
            "Local ffprobe is required for live media verification.",
            technical=True,
            failure_stage="ffprobe_video",
        )
    if not shutil.which("ffmpeg"):
        raise ResolverError(
            "resolver_configuration_error",
            "Local ffmpeg is required for live media verification.",
            technical=True,
            failure_stage="ffprobe_audio",
        )


def _diagnostic_settings(token: str) -> Settings:
    return Settings(
        _env_file=None,
        tiktok_akng_access_token=token,
        instagram_rapidapi_key="diagnostic-unused",
        worker_api_secret="diagnostic-only",
        supabase_url="https://diagnostic.invalid",
        supabase_service_role_key="diagnostic-only",
        sentry_dsn=None,
    )


def _empty_summary(input_kind: str) -> dict[str, object]:
    return {
        "provider_call_attempted": False,
        "short_link_expansion_attempted": input_kind == "short_url",
        "short_link_expansion_completed": False,
        "canonical_url_resolved": False,
        "numeric_id_resolved": False,
        "provider_input_kind": "none",
        "provider_status_class": "unknown",
        "redirect_count": 0,
        "contract_complete": False,
        "raw_video_candidate_count": 0,
        "eligible_video_candidate_count": 0,
        "selected_candidate": "none",
        "selected_resolution_bucket": "unknown",
        "selected_size_bucket": "unknown",
        "selected_codec": "unknown",
        "selected_container": "unknown",
        "media_download_verified": False,
        "media_signature_valid": False,
        "ffprobe_video_present": False,
        "ffprobe_audio_present": False,
        "cleanup_succeeded": True,
    }


def _populate_success_summary(
    summary: dict[str, object],
    result: dict[str, object],
) -> None:
    media_value = result.get("diagnostic_media")
    media = media_value if isinstance(media_value, dict) else {}
    selected_value = media.get("selected_candidate")
    selected = selected_value if isinstance(selected_value, dict) else {}
    summary.update(
        {
            "provider_call_attempted": True,
            "short_link_expansion_attempted": bool(
                result.get("short_link_expansion_attempted")
            ),
            "short_link_expansion_completed": bool(
                result.get("short_link_expansion_completed")
            ),
            "canonical_url_resolved": bool(result.get("canonical_url_resolved")),
            "numeric_id_resolved": bool(result.get("numeric_id_resolved")),
            "provider_input_kind": result.get("provider_input_kind") or "none",
            "provider_status_class": result.get("provider_status_class") or "unknown",
            "redirect_count": int(result.get("short_link_redirect_count") or 0),
            "contract_complete": bool(result.get("contract_complete")),
            "raw_video_candidate_count": int(
                result.get("raw_video_candidate_count") or 0
            ),
            "eligible_video_candidate_count": int(
                result.get("eligible_video_candidate_count") or 0
            ),
            "selected_candidate": result.get("selected_candidate") or "none",
            "selected_resolution_bucket": media.get("resolution_bucket") or "unknown",
            "selected_size_bucket": media.get("downloaded_size_bucket") or "unknown",
            "selected_codec": selected.get("codec") or "unknown",
            "selected_container": selected.get("container") or "unknown",
            "media_download_verified": bool(result.get("video_bytes_verified")),
            "media_signature_valid": bool(media.get("media_signature_valid")),
            "ffprobe_video_present": bool(media.get("final_video_present")),
            "ffprobe_audio_present": bool(media.get("final_audio_present")),
        }
    )


def _populate_error_summary(summary: dict[str, object], exc: ResolverError) -> None:
    for key in summary:
        if key in exc.diagnostic_state:
            summary[key] = exc.diagnostic_state[key]
    summary["provider_status_class"] = (
        exc.http_status_class
        or exc.diagnostic_state.get("provider_status_class")
        or "unknown"
    )


def _success_complete(summary: dict[str, object], final_path: Path) -> bool:
    return bool(
        summary.get("provider_call_attempted")
        and summary.get("contract_complete")
        and summary.get("numeric_id_resolved")
        and summary.get("media_download_verified")
        and summary.get("media_signature_valid")
        and summary.get("ffprobe_video_present")
        and summary.get("ffprobe_audio_present")
        and final_path.is_file()
    )


def _closed_failure_stage(value: str | None) -> str:
    return value if value in FAILURE_STAGES else "final_media_validation"


def _print_summary(summary: dict[str, object]) -> None:
    for key in OUTPUT_KEYS:
        print(f"{key}={_safe_scalar(summary.get(key))}")


def _safe_scalar(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
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
