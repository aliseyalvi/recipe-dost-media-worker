from __future__ import annotations

import getpass
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import requests

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import Settings, validate_external_resolver_runtime_config
from app.media_resolvers.contracts import MediaResolutionRequest, ResolverError
from app.media_resolvers.media_fetch import (
    connected_peer_ip,
    is_public_ip,
    resolve_public_ips,
    supported_media_signature,
    validate_media_url,
)
from app.media_resolvers.registry import build_resolver
from app.media_resolvers.selection import select_video_candidate
from app.runtime_config import selected_worker_environment


def main() -> int:
    selected, _source = selected_worker_environment()
    if selected != "development":
        print("validation=failed", file=sys.stderr)
        print("safe_failure_reason=development environment required", file=sys.stderr)
        return 1
    settings = Settings(
        _env_file=None,
        worker_api_secret="diagnostic-only",
        supabase_url="https://diagnostic.invalid",
        supabase_service_role_key="diagnostic-only",
    )
    try:
        validate_external_resolver_runtime_config(settings)
        source = getpass.getpass("TikTok URL or video ID (input hidden): ").strip()
        request = MediaResolutionRequest(
            platform="tiktok",
            canonical_source_url=source,
            correlation_id=f"diagnostic-{uuid4().hex}",
            capability="video_with_audio",
            byte_limit=settings.max_video_size_bytes,
            preferred_byte_target=settings.preferred_video_size_bytes,
            timeout_seconds=settings.external_provider_timeout_seconds,
        )
        resolution = build_resolver("akng_tiktok", settings).resolve(request)
        suffixes = tuple(settings.tiktok_media_host_suffixes)
        candidate = select_video_candidate(
            resolution.candidates,
            preferred_bytes=settings.preferred_video_size_bytes,
            hard_limit_bytes=settings.max_video_size_bytes,
            max_short_edge=settings.max_video_height,
            allowed_host_suffixes=suffixes,
        )
        summary = inspect_candidate(candidate.urls()[0], settings, suffixes)
    except ResolverError as exc:
        print("validation=failed", file=sys.stderr)
        print(f"normalized_error_code={exc.code}", file=sys.stderr)
        print(
            f"provider_status_class={exc.http_status_class or 'unknown'}",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        print("validation=failed", file=sys.stderr)
        print(f"safe_failure_reason={type(exc).__name__}", file=sys.stderr)
        return 1
    print("validation=passed")
    for key in (
        "initial_host_category",
        "http_status_class",
        "redirect_count",
        "final_hostname_suffix",
        "content_type_category",
        "content_length_bucket",
        "first_byte_media_signature",
        "body_download_completed",
    ):
        print(f"{key}={summary[key]}")
    return 0


def inspect_candidate(
    url: str,
    settings: Settings,
    suffixes: tuple[str, ...],
) -> dict[str, object]:
    session = requests.Session()
    session.trust_env = False
    current = url
    initial_suffix = matched_suffix(url, suffixes)
    redirects = 0
    for redirect_index in range(settings.external_media_max_redirects + 1):
        parsed = validate_media_url(current, suffixes)
        resolved_ips = resolve_public_ips(parsed.hostname or "", parsed.port or 443)
        response = session.get(
            current,
            stream=True,
            allow_redirects=False,
            timeout=(
                settings.external_media_download_connect_timeout_seconds,
                settings.external_media_download_read_timeout_seconds,
            ),
            headers={
                "Accept": "video/*,audio/*,application/octet-stream;q=0.5",
                "Range": "bytes=0-63",
            },
        )
        with response:
            peer_ip = connected_peer_ip(response)
            if peer_ip is None or peer_ip not in resolved_ips or not is_public_ip(peer_ip):
                raise ResolverError(
                    "media_validation_failed",
                    "Resolved media destination could not be verified.",
                    technical=True,
                )
            if response.status_code in {301, 302, 303, 307, 308}:
                if redirect_index >= settings.external_media_max_redirects:
                    raise ResolverError(
                        "media_download_failed",
                        "Resolved media exceeded the redirect limit.",
                    )
                location = response.headers.get("Location")
                if not location:
                    raise ResolverError(
                        "media_download_failed",
                        "Resolved media returned an invalid redirect.",
                    )
                current = urljoin(current, location)
                redirects += 1
                continue
            status_class = f"{response.status_code // 100}xx"
            if response.status_code >= 400:
                raise ResolverError(
                    "media_download_failed",
                    "Resolved media could not be inspected.",
                    http_status_class=status_class,
                )
            first_bytes = b""
            for chunk in response.iter_content(chunk_size=64):
                if chunk:
                    first_bytes = bytes(chunk[:64])
                    break
            content_type = str(response.headers.get("Content-Type") or "").lower()
            return {
                "initial_host_category": (
                    f"configured_suffix:{initial_suffix}" if initial_suffix else "not_allowlisted"
                ),
                "http_status_class": status_class,
                "redirect_count": redirects,
                "final_hostname_suffix": matched_suffix(current, suffixes) or "not_allowlisted",
                "content_type_category": content_type_category(content_type),
                "content_length_bucket": byte_bucket(response.headers.get("Content-Length")),
                "first_byte_media_signature": (
                    "valid" if supported_media_signature(first_bytes) else "invalid"
                ),
                "body_download_completed": False,
            }
    raise ResolverError("media_download_failed", "Resolved media could not be inspected.")


def matched_suffix(url: str, suffixes: tuple[str, ...]) -> str | None:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    for raw in suffixes:
        suffix = raw.lower().lstrip(".")
        if host == suffix or host.endswith("." + suffix):
            return suffix
    return None


def content_type_category(value: str) -> str:
    normalized = value.split(";", 1)[0].strip()
    if normalized.startswith("video/"):
        return "video"
    if normalized.startswith("audio/"):
        return "audio"
    if normalized == "application/octet-stream":
        return "binary"
    return "other"


def byte_bucket(value: object) -> str:
    try:
        size = int(str(value))
    except (TypeError, ValueError):
        return "unknown"
    mib = size / (1024 * 1024)
    if mib < 20:
        return "under_20_mib"
    if mib < 32:
        return "20_32_mib"
    if mib < 50:
        return "32_50_mib"
    return "50_mib_or_more"


if __name__ == "__main__":
    raise SystemExit(main())
