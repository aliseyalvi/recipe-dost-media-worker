from __future__ import annotations

import json
from typing import Mapping
from urllib.parse import urljoin, urlparse

import requests

from app.media_resolvers.contracts import ResolverError


class SafeProviderHttpClient:
    def __init__(self, *, timeout_seconds: int, response_max_bytes: int) -> None:
        self._timeout_seconds = timeout_seconds
        self._response_max_bytes = response_max_bytes
        self._session = requests.Session()
        self._session.trust_env = False

    def get_json(
        self,
        *,
        base_url: str,
        path: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        expected_host: str,
    ) -> tuple[dict[str, object], str | None, int | None]:
        endpoint = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname.lower() != expected_host.strip().lower()
            or parsed.username
            or parsed.password
        ):
            raise ResolverError(
                "resolver_configuration_error",
                "External media resolver configuration is invalid.",
                technical=True,
            )
        try:
            response = self._session.get(
                endpoint,
                params=dict(params),
                headers=dict(headers),
                timeout=(min(10, self._timeout_seconds), self._timeout_seconds),
                allow_redirects=False,
                stream=True,
            )
        except requests.Timeout as exc:
            raise ResolverError(
                "resolver_temporarily_unavailable",
                "The media resolver timed out. Please try again.",
                retryable=True,
                fallback_allowed=True,
                technical=False,
            ) from exc
        except requests.RequestException as exc:
            raise ResolverError(
                "resolver_temporarily_unavailable",
                "The media resolver is temporarily unavailable.",
                retryable=True,
                fallback_allowed=True,
                technical=True,
            ) from exc

        with response:
            status_class = f"{response.status_code // 100}xx"
            retry_after = _safe_retry_after(
                response.headers.get("Retry-After") or response.headers.get("retry-after")
            )
            if response.status_code in {401, 403}:
                raise ResolverError(
                    "resolver_configuration_error",
                    "The media resolver is not configured correctly.",
                    http_status_class=status_class,
                    technical=True,
                )
            if response.status_code == 404:
                raise ResolverError(
                    "media_private_or_deleted",
                    "This media is private, deleted, or unavailable.",
                    http_status_class=status_class,
                )
            if response.status_code == 429:
                raise ResolverError(
                    "resolver_rate_limited",
                    "The media resolver is busy. Please try again.",
                    retryable=True,
                    fallback_allowed=True,
                    http_status_class=status_class,
                )
            if 400 <= response.status_code < 500:
                raise ResolverError(
                    "media_unavailable",
                    "This media link could not be resolved.",
                    http_status_class=status_class,
                )
            if response.status_code >= 500:
                raise ResolverError(
                    "resolver_temporarily_unavailable",
                    "The media resolver is temporarily unavailable.",
                    retryable=True,
                    fallback_allowed=True,
                    http_status_class=status_class,
                    technical=True,
                )
            content_type = str(
                response.headers.get("content-type")
                or response.headers.get("Content-Type")
                or ""
            ).lower()
            if "json" not in content_type:
                raise ResolverError(
                    "resolver_contract_error",
                    "The media resolver returned an unsupported response.",
                    fallback_allowed=True,
                    http_status_class=status_class,
                    technical=True,
                    contract_diagnostic={
                        "provider_status_class": status_class,
                        "json_parsed": False,
                    },
                )
            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > self._response_max_bytes:
                    raise ResolverError(
                        "resolver_contract_error",
                        "The media resolver response exceeded the safe limit.",
                        fallback_allowed=True,
                        http_status_class=status_class,
                        technical=True,
                        contract_diagnostic={
                            "provider_status_class": status_class,
                            "json_parsed": False,
                        },
                    )
            try:
                payload = json.loads(bytes(body))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ResolverError(
                    "resolver_contract_error",
                    "The media resolver returned an invalid response.",
                    fallback_allowed=True,
                    http_status_class=status_class,
                    technical=True,
                    contract_diagnostic={
                        "provider_status_class": status_class,
                        "json_parsed": False,
                    },
                ) from exc
            if not isinstance(payload, dict):
                raise ResolverError(
                    "resolver_contract_error",
                    "The media resolver returned an invalid response.",
                    fallback_allowed=True,
                    http_status_class=status_class,
                    technical=True,
                    contract_diagnostic={
                        "provider_status_class": status_class,
                        "json_parsed": True,
                        "data_type": type(payload).__name__,
                    },
                )
            return payload, status_class, retry_after


def _safe_retry_after(value: object) -> int | None:
    try:
        seconds = int(str(value))
    except (TypeError, ValueError):
        return None
    return max(0, min(seconds, 300))
