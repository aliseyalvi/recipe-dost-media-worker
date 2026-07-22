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
    url_alternative: int = 1
    redirect_count: int = 0
    initial_host_category: str = "unknown"
    final_host_category: str = "unknown"
    signature_valid: bool = True


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
        deadline_monotonic: float | None = None,
        probe_alternatives: bool = False,
    ) -> DownloadedMedia:
        last_error: ResolverError | None = None
        overall_deadline = deadline_monotonic or (
            time.monotonic() + self._total_timeout
        )
        candidate_urls = candidate.urls()
        if probe_alternatives and len(candidate_urls) > 1:
            candidate_urls = self._rank_urls_by_throughput(
                candidate_urls,
                deadline_monotonic=overall_deadline,
            )
        for alternative, url in enumerate(candidate_urls, start=1):
            remaining = overall_deadline - time.monotonic()
            if remaining <= 0:
                break
            target = job_directory / filename
            target.unlink(missing_ok=True)
            effective_byte_limit = min(
                byte_limit or self._byte_limit,
                self._byte_limit,
            )
            range_limit = effective_byte_limit
            if candidate.size_bytes is not None and candidate.size_bytes > 0:
                range_limit = min(
                    effective_byte_limit,
                    candidate.size_bytes,
                )
            try:
                return self._download_url(
                    url,
                    target=target,
                    byte_limit=effective_byte_limit,
                    range_limit_bytes=range_limit,
                    expected_size_bytes=candidate.size_bytes,
                    url_alternative=alternative,
                    deadline_monotonic=min(
                        overall_deadline,
                        time.monotonic() + min(105, self._total_timeout),
                    ),
                )
            except ResolverError as exc:
                target.unlink(missing_ok=True)
                last_error = exc
        raise last_error or ResolverError(
            "media_download_failed",
            "Resolved media could not be downloaded.",
            retryable=True,
            fallback_allowed=True,
            failure_stage="media_download",
        )

    def _rank_urls_by_throughput(
        self,
        urls: tuple[str, ...],
        *,
        deadline_monotonic: float,
    ) -> tuple[str, ...]:
        scores: list[tuple[float, int, str]] = []
        for index, url in enumerate(urls):
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 3:
                scores.append((-1.0, index, url))
                continue
            try:
                score = self._probe_url_throughput(
                    url,
                    timeout_seconds=min(
                        6.0,
                        max(1.0, remaining / max(1, len(urls) - index)),
                    ),
                )
            except ResolverError:
                score = -1.0
            scores.append((score, index, url))
        scores.sort(key=lambda value: (value[0] < 0, -value[0], value[1]))
        return tuple(value[2] for value in scores)

    def _probe_url_throughput(self, url: str, *, timeout_seconds: float) -> float:
        parsed = validate_media_url(url, self._allowed_host_suffixes)
        resolved_ips = resolve_public_ips(parsed.hostname or "", parsed.port or 443)
        started = time.monotonic()
        total = 0
        first_bytes = bytearray()
        self._session.cookies.clear()
        try:
            response = self._session.get(
                url,
                stream=True,
                timeout=(
                    min(self._connect_timeout, max(0.001, timeout_seconds / 2)),
                    min(self._read_timeout, 3, max(0.001, timeout_seconds / 2)),
                ),
                allow_redirects=False,
                headers={
                    "Accept": "video/*,audio/*,application/octet-stream;q=0.5",
                    "Accept-Encoding": "identity",
                    "Range": "bytes=0-262143",
                },
            )
            with response:
                self._session.cookies.clear()
                peer_ip = connected_peer_ip(response)
                if peer_ip is None or peer_ip not in resolved_ips or not is_public_ip(peer_ip):
                    return -1.0
                if response.status_code >= 300:
                    return -1.0
                content_type = str(response.headers.get("Content-Type") or "").split(
                    ";", 1
                )[0].lower()
                if not (
                    content_type.startswith("video/")
                    or content_type.startswith("audio/")
                    or content_type == "application/octet-stream"
                ):
                    return -1.0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if len(first_bytes) < 64:
                        first_bytes.extend(chunk[: 64 - len(first_bytes)])
                    if total >= 256 * 1024 or time.monotonic() - started >= timeout_seconds:
                        break
        except (requests.Timeout, requests.RequestException, ResolverError):
            return -1.0
        if total <= 0 or not supported_media_signature(bytes(first_bytes)):
            return -1.0
        return total / max(0.001, time.monotonic() - started)

    def _download_url(
        self,
        url: str,
        *,
        target: Path,
        byte_limit: int,
        range_limit_bytes: int,
        expected_size_bytes: int | None,
        url_alternative: int = 1,
        deadline_monotonic: float | None = None,
    ) -> DownloadedMedia:
        current = url
        started = time.monotonic()
        deadline = deadline_monotonic or (started + self._total_timeout)
        initial_host_category = host_category(
            (urlparse(url).hostname or "").lower().rstrip("."),
            self._allowed_host_suffixes,
        )
        for redirect_count in range(self._max_redirects + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _download_timeout()
            parsed = validate_media_url(current, self._allowed_host_suffixes)
            resolved_ips = resolve_public_ips(parsed.hostname or "", parsed.port or 443)
            self._session.cookies.clear()
            try:
                response = self._session.get(
                    current,
                    stream=True,
                    timeout=(
                        min(self._connect_timeout, max(0.001, remaining / 2)),
                        min(self._read_timeout, 10, max(0.001, remaining / 2)),
                    ),
                    allow_redirects=False,
                    headers={
                        "Accept": "video/*,audio/*,application/octet-stream;q=0.5",
                        "Accept-Encoding": "identity",
                        "Range": f"bytes=0-{max(0, range_limit_bytes - 1)}",
                    },
                )
            except requests.Timeout:
                raise ResolverError(
                    "media_download_failed",
                    "Resolved media download timed out.",
                    retryable=True,
                    fallback_allowed=True,
                    failure_stage="media_download",
                ) from None
            except requests.RequestException:
                raise ResolverError(
                    "media_download_failed",
                    "Resolved media could not be downloaded.",
                    retryable=True,
                    fallback_allowed=True,
                    technical=True,
                    failure_stage="media_download",
                ) from None
            with response:
                self._session.cookies.clear()
                peer_ip = connected_peer_ip(response)
                if peer_ip is None or peer_ip not in resolved_ips or not is_public_ip(peer_ip):
                    raise ResolverError(
                        "media_validation_failed",
                        "Resolved media destination could not be verified.",
                        fallback_allowed=True,
                        technical=True,
                        failure_stage="media_redirect_validation",
                    )
                if response.status_code in {301, 302, 303, 307, 308}:
                    if redirect_count >= self._max_redirects:
                        raise ResolverError(
                            "media_download_failed",
                            "Resolved media exceeded the redirect limit.",
                            fallback_allowed=True,
                            failure_stage="media_redirect_validation",
                        )
                    location = response.headers.get("Location")
                    if not location:
                        raise ResolverError(
                            "media_download_failed",
                            "Resolved media returned an invalid redirect.",
                            fallback_allowed=True,
                            failure_stage="media_redirect_validation",
                        )
                    current = urljoin(current, location)
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    raise ResolverError(
                        "media_download_failed",
                        "Resolved media is temporarily unavailable.",
                        retryable=True,
                        fallback_allowed=True,
                        failure_stage="media_download",
                    )
                if response.status_code >= 400:
                    raise ResolverError(
                        "media_download_failed",
                        "Resolved media could not be downloaded.",
                        fallback_allowed=True,
                        failure_stage="media_download",
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
                        failure_stage="media_content_validation",
                    )
                declared = _positive_int(response.headers.get("Content-Length"))
                if declared is not None and declared > byte_limit:
                    raise ResolverError(
                        "media_size_exceeded",
                        "This video is too large to process safely.",
                        failure_stage="media_content_validation",
                    )
                total = 0
                first_bytes = bytearray()
                try:
                    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as handle:
                        for chunk in response.iter_content(chunk_size=256 * 1024):
                            now = time.monotonic()
                            if now > deadline:
                                raise _download_timeout()
                            if not chunk:
                                continue
                            remaining_expected = (
                                expected_size_bytes - total
                                if expected_size_bytes is not None
                                and expected_size_bytes > 0
                                else None
                            )
                            write_chunk = (
                                chunk[:remaining_expected]
                                if remaining_expected is not None
                                else chunk
                            )
                            total += len(write_chunk)
                            if total > byte_limit or not write_chunk:
                                raise ResolverError(
                                    "media_size_exceeded",
                                    "This video is too large to process safely.",
                                    failure_stage="media_content_validation",
                                )
                            if len(first_bytes) < 64:
                                first_bytes.extend(write_chunk[: 64 - len(first_bytes)])
                            handle.write(write_chunk)
                            elapsed = now - started
                            if elapsed >= 10 and total / elapsed < 16 * 1024:
                                raise _download_timeout()
                            if (
                                expected_size_bytes is not None
                                and expected_size_bytes > 0
                                and total >= expected_size_bytes
                            ):
                                break
                except (requests.Timeout, requests.ConnectionError):
                    target.unlink(missing_ok=True)
                    raise _download_timeout() from None
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                if total <= 0 or not supported_media_signature(bytes(first_bytes)):
                    target.unlink(missing_ok=True)
                    raise ResolverError(
                        "media_validation_failed",
                        "Resolved media was not a supported media file.",
                        fallback_allowed=True,
                        failure_stage="media_signature_validation",
                    )
                return DownloadedMedia(
                    target,
                    total,
                    content_type,
                    url_alternative=url_alternative,
                    redirect_count=redirect_count,
                    initial_host_category=initial_host_category,
                    final_host_category=host_category(
                        (parsed.hostname or "").lower().rstrip("."),
                        self._allowed_host_suffixes,
                    ),
                    signature_valid=True,
                )
        raise ResolverError(
            "media_download_failed",
            "Resolved media could not be downloaded.",
            fallback_allowed=True,
            failure_stage="media_download",
        )


def _download_timeout() -> ResolverError:
    return ResolverError(
        "media_download_failed",
        "Resolved media download exceeded the time budget.",
        retryable=True,
        fallback_allowed=True,
        failure_stage="media_download",
    )


def validate_media_url(url: str, allowed_host_suffixes: tuple[str, ...]):
    if len(url) > 8192:
        raise _unsafe_url()
    try:
        parsed = urlparse(url)
        _ = parsed.port
    except (TypeError, ValueError):
        raise _unsafe_url() from None
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


def host_category(host: str, allowed_host_suffixes: tuple[str, ...]) -> str:
    for suffix in allowed_host_suffixes:
        if host == suffix or host.endswith(f".{suffix}"):
            return suffix
    return "unknown"


def resolve_public_ips(host: str, port: int) -> set[str]:
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise ResolverError(
            "media_download_failed",
            "Resolved media host could not be reached.",
            retryable=True,
            fallback_allowed=True,
            failure_stage="media_redirect_validation",
        ) from None
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
    socket_candidates = [
        getattr(connection, "sock", None),
        _nested_attribute(raw, "_fp", "fp", "raw", "_sock"),
        _nested_attribute(raw, "_original_response", "fp", "raw", "_sock"),
    ]
    for socket_value in socket_candidates:
        try:
            peer = socket_value.getpeername() if socket_value is not None else None
        except OSError:
            continue
        if isinstance(peer, tuple) and peer:
            return str(peer[0])
    return None


def _nested_attribute(value: object, *names: str) -> object | None:
    current = value
    for name in names:
        current = getattr(current, name, None)
        if current is None:
            return None
    return current


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
        failure_stage="media_redirect_validation",
    )
