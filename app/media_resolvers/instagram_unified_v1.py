from __future__ import annotations

from collections import Counter
from urllib.parse import urlparse

from app.media_resolvers.contracts import (
    MediaCandidate,
    MediaResolution,
    MediaResolutionRequest,
    PublicMetadata,
    ResolverError,
)
from app.media_resolvers.http_client import SafeProviderHttpClient

ADAPTER_ID = "rapidapi_instagram_unified_v1"

_PHOTO_MESSAGE = "Instagram photo posts are not supported for AI video recipes."
_SIDECAR_MESSAGE = "Instagram carousel posts are not supported yet."
_PRIVATE_MESSAGE = "This Instagram post is private or unavailable."
_GENERIC_UNSUPPORTED_MESSAGE = "This Instagram media type is not supported."


class InstagramUnifiedV1Resolver:
    adapter_id = ADAPTER_ID

    def __init__(
        self,
        *,
        client: SafeProviderHttpClient,
        base_url: str,
        rapidapi_host: str,
        rapidapi_key: str,
        allowed_media_host_suffixes: tuple[str, ...],
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._host = rapidapi_host
        self._key = rapidapi_key
        self._allowed_media_host_suffixes = tuple(
            suffix.lower().lstrip(".")
            for suffix in allowed_media_host_suffixes
            if suffix
        )

    def resolve(self, request: MediaResolutionRequest) -> MediaResolution:
        try:
            payload, status_class, retry_after = self._client.get_json(
                base_url=self._base_url,
                path="/unified/index",
                params={"url": request.canonical_source_url},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-RapidAPI-Host": self._host,
                    "X-RapidAPI-Key": self._key,
                },
                expected_host=self._host,
            )
        except ResolverError as exc:
            raise _map_http_error(exc) from None

        diagnostic = _contract_diagnostic(payload, status_class)
        success = payload.get("success")
        if not isinstance(success, bool):
            raise _contract_error(diagnostic, "success")
        if not success:
            raise _provider_error(payload.get("error"), diagnostic)

        media_type = payload.get("media_type")
        if not isinstance(media_type, str) or not media_type.strip():
            raise _contract_error(diagnostic, "media_type")
        normalized_media_type = media_type.strip().lower()
        diagnostic["media_type"] = normalized_media_type
        if normalized_media_type == "photo":
            raise ResolverError(
                "unsupported_media",
                _PHOTO_MESSAGE,
                http_status_class=status_class,
                contract_diagnostic=diagnostic,
                failure_stage="provider_contract",
            )
        if normalized_media_type == "sidecar":
            raise ResolverError(
                "unsupported_media",
                _SIDECAR_MESSAGE,
                http_status_class=status_class,
                contract_diagnostic=diagnostic,
                failure_stage="provider_contract",
            )
        if normalized_media_type != "video":
            raise _contract_error(diagnostic, "media_type")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise _contract_error(diagnostic, "data")
        diagnostic["data_present"] = True
        content = data.get("content")
        if not isinstance(content, dict):
            raise _contract_error(diagnostic, "content")
        diagnostic["content_present"] = True

        media_url = _approved_https_url(
            content.get("media_url"),
            self._allowed_media_host_suffixes,
        )
        diagnostic["media_url_present"] = bool(media_url)
        if media_url is None:
            raise _contract_error(diagnostic, "media_url")

        thumbnail_url = _approved_https_url(
            content.get("thumbnail_url"),
            self._allowed_media_host_suffixes,
        )
        diagnostic["thumbnail_url_present"] = bool(thumbnail_url)

        candidates: list[MediaCandidate] = [
            MediaCandidate(
                media_type="video",
                _urls=(media_url,),
                container=_container_hint(media_url),
                audio_state="unknown",
                candidate_id="unified_video",
            )
        ]

        audio_value = content.get("audio")
        if audio_value is not None and not isinstance(audio_value, dict):
            diagnostic["audio_object_present"] = False
            raise _contract_error(diagnostic, "audio")
        audio = audio_value if isinstance(audio_value, dict) else {}
        diagnostic["audio_object_present"] = bool(audio_value is not None)
        downloadable = audio.get("downloadable")
        if downloadable is not None and not isinstance(downloadable, bool):
            raise _contract_error(diagnostic, "audio_downloadable")
        diagnostic["audio_downloadable"] = downloadable
        audio_url = _approved_https_url(
            audio.get("audio_url"),
            self._allowed_media_host_suffixes,
        )
        diagnostic["audio_url_present"] = bool(audio_url)
        raw_audio_url = audio.get("audio_url")
        if raw_audio_url is not None and raw_audio_url != "" and audio_url is None:
            raise _contract_error(diagnostic, "audio_url")
        if audio_url and downloadable is not False:
            candidates.append(
                MediaCandidate(
                    media_type="audio",
                    _urls=(audio_url,),
                    container=_container_hint(audio_url),
                    audio_state="present",
                    candidate_id="unified_audio",
                )
            )

        video_count = 1
        audio_count = sum(
            1 for candidate in candidates if candidate.media_type == "audio"
        )
        thumbnail_count = int(bool(thumbnail_url))
        summary = {
            "raw_candidate_count": video_count + audio_count,
            "raw_video_count": video_count,
            "raw_audio_count": audio_count,
            "unknown_role_count": 0,
            "declared_codec_present_count": 0,
            "declared_container_present_count": sum(
                1 for candidate in candidates if candidate.container
            ),
            "declared_mime_present_count": 0,
            "size_present_count": 0,
            "dimensions_present_count": 0,
            "eligible_declared_video_count": 0,
            "probe_required_video_count": 1,
            "eligible_audio_count": audio_count,
            "normalized_video_count": video_count,
            "normalized_audio_count": audio_count,
            "normalized_thumbnail_count": thumbnail_count,
            "rejection_counts": {},
        }
        diagnostic.update(
            {
                "normalized_video_count": video_count,
                "normalized_audio_count": audio_count,
                "normalized_thumbnail_count": thumbnail_count,
                "contract_complete": True,
                "instagram_candidate_normalization_summary": summary,
            }
        )
        return MediaResolution(
            adapter_id=self.adapter_id,
            media_mode="split_av" if audio_count else "progressive",
            candidates=tuple(candidates),
            metadata=PublicMetadata(
                title=_safe_text(data.get("title")),
                thumbnail_url=thumbnail_url,
            ),
            raw_video_candidate_count=video_count,
            raw_audio_candidate_count=audio_count,
            contract_diagnostic=diagnostic,
            retry_after_seconds=retry_after,
        )


def _contract_diagnostic(
    payload: dict[str, object],
    status_class: str | None,
) -> dict[str, object]:
    success = payload.get("success")
    data = payload.get("data")
    content = data.get("content") if isinstance(data, dict) else None
    audio = content.get("audio") if isinstance(content, dict) else None
    return {
        "provider_call_attempted": True,
        "provider_status_class": status_class or "unknown",
        "json_parsed": True,
        "success_present": "success" in payload,
        "success_value": success if isinstance(success, bool) else None,
        "media_type": (
            payload.get("media_type")
            if isinstance(payload.get("media_type"), str)
            else "unknown"
        ),
        "data_present": isinstance(data, dict),
        "content_present": isinstance(content, dict),
        "media_url_present": bool(
            isinstance(content, dict)
            and isinstance(content.get("media_url"), str)
            and content.get("media_url", "").strip()
        ),
        "thumbnail_url_present": bool(
            isinstance(content, dict)
            and isinstance(content.get("thumbnail_url"), str)
            and content.get("thumbnail_url", "").strip()
        ),
        "audio_object_present": isinstance(audio, dict),
        "audio_url_present": bool(
            isinstance(audio, dict)
            and isinstance(audio.get("audio_url"), str)
            and audio.get("audio_url", "").strip()
        ),
        "audio_downloadable": (
            audio.get("downloadable")
            if isinstance(audio, dict)
            and isinstance(audio.get("downloadable"), bool)
            else None
        ),
        "normalized_video_count": 0,
        "normalized_audio_count": 0,
        "normalized_thumbnail_count": 0,
        "contract_complete": False,
    }


def _provider_error(
    value: object,
    diagnostic: dict[str, object],
) -> ResolverError:
    if not isinstance(value, dict):
        return _contract_error(diagnostic, "error")
    raw_code = value.get("code")
    if not isinstance(raw_code, str) or not raw_code.strip():
        return _contract_error(diagnostic, "error_code")
    provider_code = raw_code.strip().upper()
    mapping: dict[str, tuple[str, str, bool, bool]] = {
        "PRIVATE_URL": (
            "provider_media_unavailable",
            _PRIVATE_MESSAGE,
            False,
            False,
        ),
        "URL_PROCESSING_ERROR": (
            "provider_processing_failed",
            "Instagram media could not be processed.",
            True,
            False,
        ),
        "SHORTCODE_EXTRACTION_ERROR": (
            "provider_processing_failed",
            "Instagram media could not be processed.",
            False,
            True,
        ),
        "UNSUPPORTED_AUDIO": (
            "unsupported_media",
            _GENERIC_UNSUPPORTED_MESSAGE,
            False,
            False,
        ),
        "UNSUPPORTED_LINK": (
            "unsupported_media",
            _GENERIC_UNSUPPORTED_MESSAGE,
            False,
            False,
        ),
        "UNSUPPORTED_STORY": (
            "unsupported_media",
            "Instagram stories are not supported.",
            False,
            False,
        ),
        "PARSING_ERROR": (
            "provider_contract_invalid",
            "The Instagram resolver returned an invalid response.",
            False,
            True,
        ),
        "API_ERROR": (
            "provider_server_error",
            "The Instagram resolver is temporarily unavailable.",
            True,
            True,
        ),
        "UNEXPECTED_ERROR": (
            "provider_server_error",
            "The Instagram resolver is temporarily unavailable.",
            True,
            True,
        ),
    }
    code, message, retryable, technical = mapping.get(
        provider_code,
        (
            "provider_server_error",
            "The Instagram resolver is temporarily unavailable.",
            True,
            True,
        ),
    )
    diagnostic["normalized_error_code"] = code
    return ResolverError(
        code,
        message,
        retryable=retryable,
        fallback_allowed=code
        in {
            "provider_processing_failed",
            "provider_contract_invalid",
            "provider_server_error",
        },
        http_status_class=str(diagnostic["provider_status_class"]),
        technical=technical,
        contract_diagnostic=diagnostic,
        failure_stage="provider_contract",
    )


def _map_http_error(exc: ResolverError) -> ResolverError:
    status_class = exc.http_status_class or "unknown"
    diagnostic = {
        "provider_call_attempted": True,
        "provider_status_class": status_class,
        "json_parsed": False,
        "success_present": False,
        "success_value": None,
        "media_type": "unknown",
        "data_present": False,
        "content_present": False,
        "media_url_present": False,
        "thumbnail_url_present": False,
        "audio_object_present": False,
        "audio_url_present": False,
        "audio_downloadable": None,
        "normalized_video_count": 0,
        "normalized_audio_count": 0,
        "normalized_thumbnail_count": 0,
        "contract_complete": False,
    }
    if exc.code == "resolver_configuration_error" and status_class == "4xx":
        code = "provider_authentication_failed"
        message = "The Instagram resolver is not configured correctly."
        retryable = False
    elif exc.code == "resolver_rate_limited":
        code = "provider_rate_limited"
        message = "The Instagram resolver is busy. Please try again."
        retryable = True
    elif exc.code == "resolver_temporarily_unavailable" and status_class == "5xx":
        code = "provider_server_error"
        message = "The Instagram resolver is temporarily unavailable."
        retryable = True
    elif exc.code == "media_private_or_deleted":
        code = "provider_media_unavailable"
        message = _PRIVATE_MESSAGE
        retryable = False
    else:
        return exc
    diagnostic["normalized_error_code"] = code
    return ResolverError(
        code,
        message,
        retryable=retryable,
        fallback_allowed=True,
        http_status_class=status_class,
        technical=code
        in {
            "provider_authentication_failed",
            "provider_server_error",
        },
        contract_diagnostic=diagnostic,
        failure_stage="provider_http",
    )


def _contract_error(
    diagnostic: dict[str, object],
    failure_field: str,
) -> ResolverError:
    safe_diagnostic = dict(diagnostic)
    safe_diagnostic["normalized_error_code"] = "provider_contract_invalid"
    safe_diagnostic["failure_stage"] = "provider_contract"
    safe_diagnostic["contract_complete"] = False
    rejections: Counter[str] = Counter()
    rejections[f"invalid_{failure_field}"] += 1
    return ResolverError(
        "provider_contract_invalid",
        "The Instagram resolver returned an invalid response.",
        fallback_allowed=True,
        http_status_class=str(diagnostic.get("provider_status_class") or "unknown"),
        technical=True,
        contract_diagnostic=safe_diagnostic,
        normalization_rejection_counts=dict(rejections),
        failure_stage="provider_contract",
    )


def _approved_https_url(
    value: object,
    allowed_host_suffixes: tuple[str, ...],
) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if len(text) > 8192:
        return None
    try:
        parsed = urlparse(text)
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.port not in {None, 443}
        or not any(
            host == suffix or host.endswith("." + suffix)
            for suffix in allowed_host_suffixes
        )
    ):
        return None
    return text


def _container_hint(url: str) -> str | None:
    path = urlparse(url).path.lower()
    if path.endswith((".mp4", ".m4v")):
        return "mp4"
    if path.endswith((".m4a", ".aac")):
        return "m4a"
    return None


def _safe_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:500] if text else None
