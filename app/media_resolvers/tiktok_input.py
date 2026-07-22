from __future__ import annotations

import http.client
import re
import socket
import ssl
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Literal
from urllib.parse import urljoin, urlparse, urlunparse

import requests

from app.media_resolvers.contracts import ResolverError
from app.media_resolvers.media_fetch import connected_peer_ip, is_public_ip, resolve_public_ips

TIKTOK_VIDEO_ID = re.compile(r"^[0-9]{10,24}$")
TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vt.tiktok.com",
    "vm.tiktok.com",
}
TIKTOK_SHORT_HOSTS = {"vt.tiktok.com", "vm.tiktok.com"}
TIKTOK_CANONICAL_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_HTML_BYTES = 128 * 1024
TikTokInputKind = Literal["short_url", "canonical_url", "numeric_id"]


@dataclass(frozen=True)
class TikTokCanonicalInput:
    canonical_url: str | None = field(repr=False, compare=False)
    video_id: str = field(repr=False, compare=False)
    original_input_kind: str
    redirect_count: int = 0
    canonical_source: str = "input_path"

    def safe_to_dict(self) -> dict[str, object]:
        return {
            "original_input_kind": self.original_input_kind,
            "redirect_count": self.redirect_count,
            "canonical_source": self.canonical_source,
            "video_id_resolved": True,
            "canonical_url_resolved": self.canonical_url is not None,
        }


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        *,
        port: int,
        resolved_ip: str,
        timeout: float,
    ) -> None:
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._resolved_ip = resolved_ip

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._resolved_ip, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(raw_socket, server_hostname=self.host)
        except Exception:
            raw_socket.close()
            raise


class _PinnedResponse:
    def __init__(
        self,
        response: http.client.HTTPResponse,
        connection: _PinnedHTTPSConnection,
        *,
        verified_peer_ip: str,
    ) -> None:
        self.status_code = response.status
        self.headers = response.headers
        self.verified_peer_ip = verified_peer_ip
        self._response = response
        self._connection = connection

    def iter_content(self, chunk_size: int) -> object:
        while True:
            chunk = self._response.read(chunk_size)
            if not chunk:
                return
            yield chunk

    def __enter__(self) -> _PinnedResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self._response.close()
        self._connection.close()


def resolve_tiktok_canonical_input(
    source: str,
    *,
    connect_timeout_seconds: int = 5,
    read_timeout_seconds: int = 10,
    total_timeout_seconds: int = 15,
    max_redirects: int = 5,
    session: requests.Session | None = None,
) -> TikTokCanonicalInput:
    text = str(source or "").strip()
    if TIKTOK_VIDEO_ID.fullmatch(text):
        input_kind: TikTokInputKind = "numeric_id"
    else:
        parsed = _validate_tiktok_url(text, failure_stage="input_validation")
        host = (parsed.hostname or "").lower().rstrip(".")
        input_kind = "short_url" if host in TIKTOK_SHORT_HOSTS else "canonical_url"
    return resolve_tiktok_input(
        text,
        input_kind=input_kind,
        connect_timeout_seconds=max(1, connect_timeout_seconds),
        read_timeout_seconds=max(1, read_timeout_seconds),
        total_timeout_seconds=max(1, total_timeout_seconds),
        max_redirects=max(0, min(max_redirects, 5)),
        session=session,
    )


def resolve_tiktok_input(
    source: str,
    *,
    input_kind: TikTokInputKind,
    connect_timeout_seconds: int = 5,
    read_timeout_seconds: int = 10,
    total_timeout_seconds: int = 15,
    max_redirects: int = 5,
    session: requests.Session | None = None,
) -> TikTokCanonicalInput:
    text = str(source or "").strip()
    if input_kind == "numeric_id":
        if not TIKTOK_VIDEO_ID.fullmatch(text):
            raise _input_error(
                "A numeric TikTok video ID is required.",
                failure_stage="input_validation",
            )
        return TikTokCanonicalInput(
            canonical_url=None,
            video_id=text,
            original_input_kind="numeric_id",
            canonical_source="numeric_input",
        )

    parsed = _validate_tiktok_url(text, failure_stage="input_validation")
    host = (parsed.hostname or "").lower().rstrip(".")
    if input_kind == "canonical_url":
        if host not in TIKTOK_CANONICAL_HOSTS:
            raise _input_error(
                "A canonical TikTok video URL is required.",
                failure_stage="input_validation",
            )
        video_id = _video_id_from_path(parsed.path)
        if not video_id:
            raise _input_error(
                "The canonical TikTok URL does not contain a video ID.",
                failure_stage="numeric_id_extraction",
            )
        return TikTokCanonicalInput(
            canonical_url=_canonical_url(parsed, video_id),
            video_id=video_id,
            original_input_kind="canonical_url",
        )

    if input_kind != "short_url" or host not in TIKTOK_SHORT_HOSTS or not parsed.path.strip("/"):
        raise _input_error(
            "A TikTok short URL is required.",
            failure_stage="input_validation",
        )
    return _expand_short_url(
        urlunparse(("https", host, parsed.path, "", "", "")),
        connect_timeout_seconds=max(1, connect_timeout_seconds),
        read_timeout_seconds=max(1, read_timeout_seconds),
        total_timeout_seconds=max(1, total_timeout_seconds),
        max_redirects=max(0, min(max_redirects, 5)),
        session=session,
    )


def normalize_full_tiktok_url(source: str) -> tuple[str, str]:
    """Normalize only already-expanded provider inputs; short links must be expanded first."""
    text = str(source or "").strip()
    if TIKTOK_VIDEO_ID.fullmatch(text):
        return text, "numeric_id"
    parsed = _validate_tiktok_url(text, failure_stage="input_validation")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host in TIKTOK_SHORT_HOSTS:
        raise _input_error(
            "This TikTok short link must be expanded before provider resolution.",
            failure_stage="input_validation",
        )
    video_id = _video_id_from_path(parsed.path)
    if not video_id:
        raise _input_error(failure_stage="numeric_id_extraction")
    return _canonical_url(parsed, video_id), "canonical_url"


def _expand_short_url(
    short_url: str,
    *,
    connect_timeout_seconds: int,
    read_timeout_seconds: int,
    total_timeout_seconds: int,
    max_redirects: int,
    session: requests.Session | None,
) -> TikTokCanonicalInput:
    client = session
    if client is not None:
        client.trust_env = False
    current = short_url
    started = time.monotonic()
    redirect_count = 0
    try:
        while True:
            remaining_seconds = total_timeout_seconds - (time.monotonic() - started)
            if remaining_seconds <= 0:
                raise _input_error(
                    "TikTok short-link expansion timed out.",
                    retryable=True,
                    failure_stage="short_link_http_request",
                    redirect_count=redirect_count,
                )
            parsed = _validate_tiktok_url(
                current,
                failure_stage="short_link_redirect_validation",
                redirect_count=redirect_count,
            )
            try:
                resolved_ips = resolve_public_ips(parsed.hostname or "", parsed.port or 443)
            except ResolverError as exc:
                raise _input_error(
                    "TikTok short-link host could not be resolved safely.",
                    retryable=exc.retryable,
                    failure_stage="short_link_dns_resolution",
                    redirect_count=redirect_count,
                ) from None
            if client is not None:
                client.cookies.clear()
            phase_timeout = max(0.001, remaining_seconds / 2)
            try:
                if client is None:
                    response = _pinned_tiktok_get(
                        parsed,
                        resolved_ips=resolved_ips,
                        connect_timeout_seconds=min(
                            connect_timeout_seconds, phase_timeout
                        ),
                        read_timeout_seconds=min(read_timeout_seconds, phase_timeout),
                    )
                else:
                    response = client.get(
                        current,
                        stream=True,
                        allow_redirects=False,
                        timeout=(
                            min(connect_timeout_seconds, phase_timeout),
                            min(read_timeout_seconds, phase_timeout),
                        ),
                        headers={
                            "Accept": "*/*",
                            "User-Agent": "curl/8.7.1",
                        },
                    )
            except ResolverError:
                raise
            except requests.Timeout:
                raise _input_error(
                    "TikTok short-link expansion timed out.",
                    retryable=True,
                    failure_stage="short_link_http_request",
                    redirect_count=redirect_count,
                ) from None
            except (OSError, ssl.SSLError, http.client.HTTPException, requests.RequestException):
                raise _input_error(
                    "TikTok short-link expansion failed.",
                    retryable=True,
                    failure_stage="short_link_http_request",
                    redirect_count=redirect_count,
                ) from None
            with response:
                if client is not None:
                    client.cookies.clear()
                peer_ip = (
                    response.verified_peer_ip
                    if isinstance(response, _PinnedResponse)
                    else connected_peer_ip(response)
                )
                if peer_ip is None or peer_ip not in resolved_ips or not is_public_ip(peer_ip):
                    raise _input_error(
                        "TikTok short-link destination could not be verified.",
                        failure_stage="short_link_redirect_validation",
                        redirect_count=redirect_count,
                    )
                if response.status_code in _REDIRECT_STATUSES:
                    if redirect_count >= max_redirects:
                        raise _input_error(
                            "TikTok short link exceeded the redirect limit.",
                            failure_stage="short_link_redirect_validation",
                            redirect_count=redirect_count,
                        )
                    location = response.headers.get("Location")
                    if not location:
                        raise _input_error(
                            failure_stage="short_link_redirect_validation",
                            redirect_count=redirect_count,
                        )
                    target = urljoin(current, location)
                    _validate_tiktok_url(
                        target,
                        failure_stage="short_link_redirect_validation",
                        redirect_count=redirect_count + 1,
                    )
                    current = target
                    redirect_count += 1
                    continue
                final_parsed = _validate_tiktok_url(
                    current,
                    failure_stage="short_link_redirect_validation",
                    redirect_count=redirect_count,
                )
                video_id = _video_id_from_path(final_parsed.path)
                if video_id:
                    return TikTokCanonicalInput(
                        canonical_url=_canonical_url(final_parsed, video_id),
                        video_id=video_id,
                        original_input_kind="short_url",
                        redirect_count=redirect_count,
                        canonical_source="redirect_path",
                    )
                if response.status_code >= 500:
                    raise _input_error(
                        "TikTok short-link expansion is temporarily unavailable.",
                        retryable=True,
                        failure_stage="short_link_http_request",
                        redirect_count=redirect_count,
                    )
                if response.status_code >= 400:
                    raise _input_error(
                        failure_stage="short_link_canonical_extraction",
                        redirect_count=redirect_count,
                    )
                canonical = _bounded_html_canonical(
                    response,
                    deadline=started + total_timeout_seconds,
                )
                if canonical:
                    canonical_parsed = _validate_tiktok_url(
                        urljoin(current, canonical),
                        failure_stage="short_link_redirect_validation",
                        redirect_count=redirect_count,
                    )
                    video_id = _video_id_from_path(canonical_parsed.path)
                    if video_id:
                        return TikTokCanonicalInput(
                            canonical_url=_canonical_url(canonical_parsed, video_id),
                            video_id=video_id,
                            original_input_kind="short_url",
                            redirect_count=redirect_count,
                            canonical_source="html_canonical",
                        )
                    raise _input_error(
                        "TikTok canonical URL did not contain a video ID.",
                        failure_stage="numeric_id_extraction",
                        redirect_count=redirect_count,
                    )
                raise _input_error(
                    "TikTok short link did not resolve to a public video.",
                    failure_stage="short_link_canonical_extraction",
                    redirect_count=redirect_count,
                )
    finally:
        if client is not None:
            client.cookies.clear()


def _pinned_tiktok_get(
    parsed,
    *,
    resolved_ips: set[str],
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
) -> _PinnedResponse:
    host = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    for resolved_ip in sorted(resolved_ips):
        connection = _PinnedHTTPSConnection(
            host,
            port=port,
            resolved_ip=resolved_ip,
            timeout=max(0.001, connect_timeout_seconds),
        )
        try:
            connection.connect()
            peer = connection.sock.getpeername() if connection.sock else None
            peer_ip = str(peer[0]) if isinstance(peer, tuple) and peer else ""
            if peer_ip != resolved_ip or not is_public_ip(peer_ip):
                raise _input_error(
                    "TikTok short-link destination could not be verified.",
                    failure_stage="short_link_redirect_validation",
                )
            connection.sock.settimeout(max(0.001, read_timeout_seconds))
            connection.request(
                "GET",
                path,
                headers={"Accept": "*/*", "User-Agent": "curl/8.7.1"},
            )
            response = connection.getresponse()
            return _PinnedResponse(
                response,
                connection,
                verified_peer_ip=peer_ip,
            )
        except ResolverError:
            connection.close()
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException):
            connection.close()
            continue
    raise _input_error(
        "TikTok short-link expansion failed.",
        retryable=True,
        failure_stage="short_link_http_request",
    )


def _bounded_html_canonical(
    response: requests.Response,
    *,
    deadline: float,
) -> str | None:
    content_type = str(response.headers.get("Content-Type") or "").lower()
    if "html" not in content_type:
        return None
    declared = _positive_int(response.headers.get("Content-Length"))
    if declared is not None and declared > _MAX_HTML_BYTES:
        raise _input_error(
            "TikTok canonical page exceeded the safe response limit.",
            failure_stage="short_link_canonical_extraction",
        )
    body = bytearray()
    for chunk in response.iter_content(chunk_size=16 * 1024):
        if time.monotonic() >= deadline:
            raise _input_error(
                "TikTok short-link expansion timed out.",
                retryable=True,
                failure_stage="short_link_http_request",
            )
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > _MAX_HTML_BYTES:
            raise _input_error(
                "TikTok canonical page exceeded the safe response limit.",
                failure_stage="short_link_canonical_extraction",
            )
    parser = _CanonicalParser()
    try:
        parser.feed(bytes(body).decode("utf-8", errors="replace"))
    except (TypeError, ValueError):
        return None
    return parser.canonical_url


class _CanonicalParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.canonical_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.canonical_url:
            return
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag.lower() == "link" and "canonical" in values.get("rel", "").lower().split():
            self.canonical_url = values.get("href") or None
        elif tag.lower() == "meta" and values.get("property", "").lower() == "og:url":
            self.canonical_url = values.get("content") or None


def _validate_tiktok_url(
    value: str,
    *,
    failure_stage: str,
    redirect_count: int = 0,
):
    if len(value) > 4096:
        raise _input_error(failure_stage=failure_stage, redirect_count=redirect_count)
    try:
        parsed = urlparse(value)
        _ = parsed.port
    except (TypeError, ValueError) as exc:
        raise _input_error(
            failure_stage=failure_stage,
            redirect_count=redirect_count,
        ) from None
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or host not in TIKTOK_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        raise _input_error(failure_stage=failure_stage, redirect_count=redirect_count)
    return parsed


def _video_id_from_path(path: str) -> str | None:
    parts = [part for part in path.split("/") if part]
    for index, part in enumerate(parts):
        if part == "video" and index + 1 < len(parts):
            candidate = parts[index + 1]
            if TIKTOK_VIDEO_ID.fullmatch(candidate):
                return candidate
    return None


def _canonical_url(parsed, video_id: str) -> str:
    parts = [part for part in parsed.path.split("/") if part]
    video_index = parts.index("video")
    path = "/" + "/".join(parts[: video_index + 1] + [video_id])
    return urlunparse(("https", "www.tiktok.com", path, "", "", ""))


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _input_error(
    message: str = "This TikTok link is invalid.",
    *,
    retryable: bool = False,
    failure_stage: str = "input_validation",
    redirect_count: int = 0,
) -> ResolverError:
    return ResolverError(
        "media_unavailable" if not retryable else "resolver_temporarily_unavailable",
        message,
        retryable=retryable,
        failure_stage=failure_stage,
        diagnostic_state={
            "provider_call_attempted": False,
            "short_link_expansion_attempted": failure_stage.startswith("short_link_"),
            "short_link_expansion_completed": False,
            "canonical_url_resolved": False,
            "numeric_id_resolved": False,
            "redirect_count": max(0, min(redirect_count, 5)),
        },
    )
