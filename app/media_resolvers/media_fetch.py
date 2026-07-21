from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from app.media_resolvers.contracts import MediaCandidate, ResolverError


@dataclass(frozen=True)
class DownloadedMedia:
    path: Path
    size_bytes: int
    content_type: str


class SecureMediaDownloader:
    def __init__(
        self,
        *,
        temp_root: str,
        allowed_host_suffixes: tuple[str, ...],
        byte_limit: int,
        connect_timeout_seconds: int,
        read_timeout_seconds: int,
        total_timeout_seconds: int,
        max_redirects: int,
    ) -> None:
        self._temp_root = Path(temp_root)
        self._allowed_host_suffixes = tuple(
            suffix.lower().lstrip(".") for suffix in allowed_host_suffixes if suffix
        )
        self._byte_limit = byte_limit
        self._connect_timeout = connect_timeout_seconds
        self._read_timeout = read_timeout_seconds
        self._total_timeout = total_timeout_seconds
        self._max_redirects = max_redirects
        self._session = requests.Session()
        self._session.trust_env = False

    def create_job_directory(self, correlation_id: str) -> Path:
        self._temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        safe_prefix = "".join(
            character for character in correlation_id if character.isalnum() or character in "-_"
        )[:32] or "job"
        directory = Path(tempfile.mkdtemp(prefix=f"external-{safe_prefix}-", dir=self._temp_root))
        directory.chmod(0o700)
        return directory

    def download_candidate(
        self,
        candidate: MediaCandidate,
        *,
        job_directory: Path,
        filename: str,
        byte_limit: int | None = None,
    ) -> DownloadedMedia:
        last_error: ResolverError | None = None
        for url in candidate.urls():
            target = job_directory / filename
            target.unlink(missing_ok=True)
            try:
                return self._download_url(
                    url,
                    target=target,
                    byte_limit=min(byte_limit or self._byte_limit, self._byte_limit),
                )
            except ResolverError as exc:
                target.unlink(missing_ok=True)
                last_error = exc
        raise last_error or ResolverError(
            "media_download_failed",
            "Resolved media could not be downloaded.",
            retryable=True,
            fallback_allowed=True,
        )

    def _download_url(self, url: str, *, target: Path, byte_limit: int) -> DownloadedMedia:
        current = url
        started = time.monotonic()
        for redirect_count in range(self._max_redirects + 1):
            parsed = validate_media_url(current, self._allowed_host_suffixes)
            resolved_ips = resolve_public_ips(parsed.hostname or "", parsed.port or 443)
            try:
                response = self._session.get(
                    current,
                    stream=True,
                    timeout=(self._connect_timeout, self._read_timeout),
                    allow_redirects=False,
                    headers={"Accept": "video/*,audio/*,application/octet-stream;q=0.5"},
                )
            except requests.Timeout as exc:
                raise ResolverError(
                    "media_download_failed",
                    "Resolved media download timed out.",
                    retryable=True,
                    fallback_allowed=True,
                ) from exc
            except requests.RequestException as exc:
                raise ResolverError(
                    "media_download_failed",
                    "Resolved media could not be downloaded.",
                    retryable=True,
                    fallback_allowed=True,
                    technical=True,
                ) from exc
            with response:
                peer_ip = connected_peer_ip(response)
                if peer_ip is None or peer_ip not in resolved_ips or not is_public_ip(peer_ip):
                    raise ResolverError(
                        "media_validation_failed",
                        "Resolved media destination could not be verified.",
                        fallback_allowed=True,
                        technical=True,
                    )
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect_count >= self._max_redirects:
                        raise ResolverError(
                            "media_download_failed",
                            "Resolved media exceeded the redirect limit.",
                            fallback_allowed=True,
                        )
                    location = response.headers.get("Location")
                    if not location:
                        raise ResolverError(
                            "media_download_failed",
                            "Resolved media returned an invalid redirect.",
                            fallback_allowed=True,
                        )
                    current = urljoin(current, location)
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    raise ResolverError(
                        "media_download_failed",
                        "Resolved media is temporarily unavailable.",
                        retryable=True,
                        fallback_allowed=True,
                    )
                if response.status_code >= 400:
                    raise ResolverError(
                        "media_download_failed",
                        "Resolved media could not be downloaded.",
                        fallback_allowed=True,
                    )
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                if not (
                    content_type.startswith("video/")
                    or content_type.startswith("audio/")
                    or content_type == "application/octet-stream"
                ):
                    raise ResolverError(
                        "media_validation_failed",
                        "Resolved media returned an unsupported content type.",
                        fallback_allowed=True,
                    )
                declared = _positive_int(response.headers.get("Content-Length"))
                if declared is not None and declared > byte_limit:
                    raise ResolverError(
                        "media_size_exceeded",
                        "This video is too large to process safely.",
                    )
                total = 0
                first_bytes = bytearray()
                try:
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as handle:
                        for chunk in response.iter_content(chunk_size=256 * 1024):
                            if time.monotonic() - started > self._total_timeout:
                                raise ResolverError(
                                    "media_download_failed",
                                    "Resolved media download exceeded the time budget.",
                                    retryable=True,
                                    fallback_allowed=True,
                                )
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > byte_limit:
                                raise ResolverError(
                                    "media_size_exceeded",
                                    "This video is too large to process safely.",
                                )
                            if len(first_bytes) < 64:
                                first_bytes.extend(chunk[: 64 - len(first_bytes)])
                            handle.write(chunk)
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                if total <= 0 or not supported_media_signature(bytes(first_bytes)):
                    target.unlink(missing_ok=True)
                    raise ResolverError(
                        "media_validation_failed",
                        "Resolved media was not a supported media file.",
                        fallback_allowed=True,
                    )
                return DownloadedMedia(target, total, content_type)
        raise ResolverError(
            "media_download_failed",
            "Resolved media could not be downloaded.",
            fallback_allowed=True,
        )


def validate_media_url(url: str, allowed_host_suffixes: tuple[str, ...]):
    if len(url) > 8192:
        raise _unsafe_url()
    try:
        parsed = urlparse(url)
        _ = parsed.port
    except (TypeError, ValueError) as exc:
        raise _unsafe_url() from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.fragment
        or (parsed.port not in {None, 443})
        or not host_allowed(host, allowed_host_suffixes)
    ):
        raise _unsafe_url()
    return parsed


def host_allowed(host: str, allowed_host_suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in allowed_host_suffixes)


def resolve_public_ips(host: str, port: int) -> set[str]:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ResolverError(
            "media_download_failed",
            "Resolved media host could not be reached.",
            retryable=True,
            fallback_allowed=True,
        ) from exc
    values = {str(item[4][0]) for item in addresses if item and item[4]}
    if not values or any(not is_public_ip(value) for value in values):
        raise _unsafe_url()
    return values


def is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(address.is_global and not address.is_multicast and not address.is_unspecified)


def connected_peer_ip(response: requests.Response) -> str | None:
    raw = getattr(response, "raw", None)
    connection = getattr(raw, "_connection", None) or getattr(raw, "connection", None)
    socket_value = getattr(connection, "sock", None)
    try:
        peer = socket_value.getpeername() if socket_value is not None else None
    except OSError:
        return None
    return str(peer[0]) if isinstance(peer, tuple) and peer else None


def supported_media_signature(value: bytes) -> bool:
    if len(value) >= 12 and value[4:8] == b"ftyp":
        return True
    return value.startswith(b"\x1aE\xdf\xa3")


def cleanup_job_directory(path: Path | None) -> None:
    if path is None:
        return
    shutil.rmtree(path, ignore_errors=True)


def sweep_stale_job_directories(
    temp_root: str,
    *,
    older_than_seconds: int,
    now: float | None = None,
) -> int:
    root = Path(temp_root)
    if not root.exists() or not root.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - max(60, older_than_seconds)
    removed = 0
    for child in root.iterdir():
        try:
            if (
                child.is_symlink()
                or not child.is_dir()
                or not child.name.startswith("external-")
                or child.stat().st_mtime >= cutoff
            ):
                continue
            shutil.rmtree(child)
            removed += 1
        except OSError:
            continue
    return removed


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _unsafe_url() -> ResolverError:
    return ResolverError(
        "media_validation_failed",
        "Resolved media destination was rejected by safety checks.",
        fallback_allowed=True,
        technical=True,
    )
