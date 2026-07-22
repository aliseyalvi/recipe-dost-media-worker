from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.media_resolvers.contracts import (
    MediaCandidate,
    MediaResolution,
    MediaResolutionRequest,
    PublicMetadata,
    ResolverError,
    SourceContext,
)
from app.media_resolvers.http_client import SafeProviderHttpClient
from app.media_resolvers.tiktok_input import (
    TikTokCanonicalInput,
    normalize_full_tiktok_url,
    resolve_tiktok_canonical_input,
    resolve_tiktok_input,
)

ADAPTER_ID = "akng_tiktok"
INPUT_NOT_VALID_CODES = {"input_not_valid", "invalid_input", "invalid_url", "invalid_source"}


@dataclass(frozen=True)
class AkngInputResult:
    payload: dict[str, object] = field(repr=False, compare=False)
    status_class: str | None
    retry_after_seconds: int | None
    diagnostic: dict[str, object]


class TikTokAkngResolver:
    adapter_id = ADAPTER_ID

    def __init__(
        self,
        *,
        client: SafeProviderHttpClient,
        base_url: str,
        access_token: str,
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._token = access_token

    def resolve(self, request: MediaResolutionRequest) -> MediaResolution:
        if request.source_input_kind in {"short_url", "canonical_url", "numeric_id"}:
            canonical = resolve_tiktok_input(
                request.canonical_source_url,
                input_kind=request.source_input_kind,  # type: ignore[arg-type]
                connect_timeout_seconds=min(5, request.timeout_seconds),
                read_timeout_seconds=min(10, request.timeout_seconds),
                total_timeout_seconds=request.timeout_seconds,
                max_redirects=5,
            )
        else:
            canonical = resolve_tiktok_canonical_input(
                request.canonical_source_url,
                connect_timeout_seconds=min(5, request.timeout_seconds),
                read_timeout_seconds=min(10, request.timeout_seconds),
                total_timeout_seconds=request.timeout_seconds,
                max_redirects=5,
            )
        inputs: list[tuple[str, str]] = [(canonical.video_id, "numeric_id")]
        if canonical.canonical_url:
            inputs.append((canonical.canonical_url, "canonical_url"))
        first_diagnostic: dict[str, object] = {}
        for provider_attempt, (provider_input, input_kind) in enumerate(inputs, start=1):
            try:
                inspected = self.inspect_provider_input(
                    provider_input,
                    input_kind=input_kind,
                    provider_attempt=provider_attempt,
                    alternate_input_attempted=provider_attempt > 1,
                )
            except ResolverError as exc:
                if not exc.failure_stage:
                    exc.failure_stage = (
                        "akng_json_parse"
                        if exc.code == "resolver_contract_error"
                        and exc.contract_diagnostic.get("json_parsed") is False
                        else "akng_request"
                    )
                exc.diagnostic_state.update(
                    {
                        "provider_call_attempted": True,
                        "provider_input_kind": input_kind,
                        "provider_status_class": exc.http_status_class or "unknown",
                        "short_link_expansion_attempted": (
                            canonical.original_input_kind == "short_url"
                        ),
                        "short_link_expansion_completed": (
                            canonical.original_input_kind == "short_url"
                        ),
                        "canonical_url_resolved": canonical.canonical_url is not None,
                        "numeric_id_resolved": True,
                        "redirect_count": canonical.redirect_count,
                    }
                )
                raise
            try:
                return self._parse_resolution(
                    inspected,
                    canonical=canonical,
                    first_diagnostic=first_diagnostic,
                )
            except ResolverError as exc:
                if provider_attempt == 1 and _eligible_alternate_input(exc):
                    first_diagnostic = dict(exc.contract_diagnostic)
                    continue
                if provider_attempt == 2 and _empty_input_contract(exc):
                    raise ResolverError(
                        "media_unavailable",
                        "This TikTok video did not expose usable media.",
                        http_status_class=exc.http_status_class,
                        contract_diagnostic=exc.contract_diagnostic,
                        failure_stage="akng_contract_validation",
                        diagnostic_state=_diagnostic_state(exc.contract_diagnostic),
                    ) from exc
                raise
        raise ResolverError("media_unavailable", "This TikTok video did not expose usable media.")

    def inspect_provider_input(
        self,
        provider_input: str,
        *,
        input_kind: str,
        provider_attempt: int,
        alternate_input_attempted: bool,
    ) -> AkngInputResult:
        host = urlparse(self._base_url).hostname or ""
        payload, status_class, retry_after = self._client.get_json(
            base_url=self._base_url,
            path="/tiktok/video",
            params={"input": provider_input, "access_token": self._token},
            headers={"Accept": "application/json"},
            expected_host=host,
        )
        diagnostic = contract_fingerprint(
            payload,
            status_class,
            input_kind=input_kind,
            provider_attempt=provider_attempt,
            alternate_input_attempted=alternate_input_attempted,
        )
        return AkngInputResult(payload, status_class, retry_after, diagnostic)

    def _parse_resolution(
        self,
        inspected: AkngInputResult,
        *,
        canonical: TikTokCanonicalInput,
        first_diagnostic: dict[str, object],
    ) -> MediaResolution:
        payload = inspected.payload
        status_class = inspected.status_class
        retry_after = inspected.retry_after_seconds
        diagnostic = {
            **inspected.diagnostic,
            "short_link_expanded": canonical.original_input_kind == "short_url",
            "short_link_expansion_attempted": canonical.original_input_kind == "short_url",
            "short_link_expansion_completed": canonical.original_input_kind == "short_url",
            "short_link_redirect_count": canonical.redirect_count,
            "canonical_url_resolved": canonical.canonical_url is not None,
            "numeric_id_resolved": True,
            "provider_call_attempted": True,
            "alternate_input_attempted": bool(first_diagnostic),
            "first_input_contract_complete": bool(first_diagnostic.get("contract_complete")),
        }
        if payload.get("success") is not True:
            message_code = _message_code(payload.get("message"))
            if any(marker in message_code for marker in ("private", "deleted", "not_found")):
                raise _resolver_error(
                    "media_private_or_deleted",
                    "This TikTok video is private, deleted, or unavailable.",
                    status_class=status_class,
                    diagnostic=diagnostic,
                )
            if message_code in INPUT_NOT_VALID_CODES or "not_valid" in message_code:
                raise _resolver_error(
                    "resolver_input_rejected",
                    "This TikTok link or video ID is invalid.",
                    status_class=status_class,
                    diagnostic=diagnostic,
                )
            raise _resolver_error(
                "resolver_temporarily_unavailable",
                "TikTok media could not be resolved.",
                retryable=True,
                fallback_allowed=True,
                status_class=status_class,
                diagnostic=diagnostic,
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            raise _contract_error(status_class, diagnostic)
        item_info = data.get("itemInfo")
        if "itemInfo" not in data:
            raise _empty_contract(status_class, diagnostic)
        if not isinstance(item_info, dict):
            raise _contract_error(status_class, diagnostic)
        item = item_info.get("itemStruct")
        if not isinstance(item, dict):
            raise _contract_error(status_class, diagnostic)
        video = item.get("video")
        if not isinstance(video, dict):
            raise _contract_error(status_class, diagnostic)
        bitrate_info = video.get("bitrateInfo")
        if not isinstance(bitrate_info, list):
            raise _contract_error(status_class, diagnostic)

        rejection_counts: Counter[str] = Counter()
        videos: list[MediaCandidate] = []
        for value in bitrate_info:
            candidate, reason = _video_candidate(value)
            if candidate is not None:
                videos.append(candidate)
            elif reason:
                rejection_counts[reason] += 1

        progressive = _progressive_video_candidate(video)
        if progressive is not None:
            videos.append(progressive)

        audio_info = video.get("bitrateAudioInfo")
        audio_values = audio_info if isinstance(audio_info, list) else []
        audios: list[MediaCandidate] = []
        for value in audio_values:
            candidate, reason = _audio_candidate(value)
            if candidate is not None:
                audios.append(candidate)
            elif reason:
                rejection_counts[reason] += 1

        if bitrate_info and not videos:
            raise ResolverError(
                "resolver_contract_error",
                "The TikTok resolver returned malformed media candidates.",
                fallback_allowed=True,
                http_status_class=status_class,
                technical=True,
                contract_diagnostic=diagnostic,
                failure_stage="candidate_normalization",
                diagnostic_state=_diagnostic_state(diagnostic),
                raw_video_candidate_count=len(bitrate_info),
                raw_audio_candidate_count=len(audio_values),
                normalization_rejection_counts=dict(rejection_counts),
            )
        if not videos:
            raise ResolverError(
                "unsupported_media",
                "TikTok did not return a supported video candidate.",
                http_status_class=status_class,
                contract_diagnostic=diagnostic,
                failure_stage="candidate_selection",
                diagnostic_state=_diagnostic_state(diagnostic),
                raw_video_candidate_count=len(bitrate_info),
                raw_audio_candidate_count=len(audio_values),
                normalization_rejection_counts=dict(rejection_counts),
            )

        duration = _positive_int(video.get("duration"))
        author = _mapping(item.get("author"))
        return MediaResolution(
            adapter_id=self.adapter_id,
            media_mode="progressive",
            candidates=tuple(videos + audios),
            metadata=PublicMetadata(
                title=_untrusted_text(item.get("desc"), 500),
                author=_untrusted_text(author.get("nickname"), 120),
                thumbnail_url=_https_url(video.get("cover")),
                duration_seconds=duration,
            ),
            source_context=_source_context(item, data, duration),
            raw_video_candidate_count=len(bitrate_info),
            raw_audio_candidate_count=len(audio_values),
            normalization_rejection_counts=dict(rejection_counts),
            contract_diagnostic=diagnostic,
            retry_after_seconds=retry_after,
        )


def normalize_akng_input(source: str) -> tuple[str, str]:
    return normalize_full_tiktok_url(source)


def contract_fingerprint(
    payload: dict[str, object],
    status_class: str | None,
    *,
    input_kind: str,
    provider_attempt: int = 1,
    alternate_input_attempted: bool = False,
) -> dict[str, object]:
    data = payload.get("data")
    data_map = _mapping(data)
    item_info = data_map.get("itemInfo")
    item_info_map = _mapping(item_info)
    item_struct = item_info_map.get("itemStruct")
    item_map = _mapping(item_struct)
    video = item_map.get("video")
    video_map = _mapping(video)
    bitrate_info = video_map.get("bitrateInfo")
    bitrate_audio_info = video_map.get("bitrateAudioInfo")
    download_urls = data_map.get("download_urls")
    return {
        "provider_status_class": status_class or "unknown",
        "json_parsed": True,
        "provider_input_kind": input_kind,
        "provider_attempt": max(1, min(provider_attempt, 2)),
        "alternate_input_attempted": alternate_input_attempted,
        "success_present": "success" in payload,
        "success_type": _safe_type(payload.get("success")),
        "success_value": payload.get("success") if isinstance(payload.get("success"), bool) else None,
        "data_present": "data" in payload,
        "data_type": _safe_type(data),
        "item_info_present": isinstance(item_info, dict),
        "item_struct_present": isinstance(item_struct, dict),
        "video_present": isinstance(video, dict),
        "bitrate_info_present": "bitrateInfo" in video_map,
        "bitrate_info_type": _safe_type(bitrate_info),
        "bitrate_info_count": len(bitrate_info) if isinstance(bitrate_info, list) else 0,
        "bitrate_audio_info_present": "bitrateAudioInfo" in video_map,
        "bitrate_audio_info_type": _safe_type(bitrate_audio_info),
        "bitrate_audio_info_count": (
            len(bitrate_audio_info) if isinstance(bitrate_audio_info, list) else 0
        ),
        "play_addr_struct_present": isinstance(video_map.get("PlayAddrStruct"), dict),
        "download_urls_count": len(download_urls) if isinstance(download_urls, list) else 0,
        "status_code": _bounded_int(data_map.get("statusCode"), minimum=-1, maximum=9999),
        "status_msg_present": "statusMsg" in data_map,
        "contract_complete": (
            isinstance(data, dict)
            and isinstance(item_info, dict)
            and isinstance(item_struct, dict)
            and isinstance(video, dict)
            and isinstance(bitrate_info, list)
        ),
    }


def _empty_contract(status_class: str | None, diagnostic: dict[str, object]) -> ResolverError:
    return ResolverError(
        "resolver_input_rejected",
        "The TikTok resolver did not accept this input form.",
        fallback_allowed=True,
        http_status_class=status_class,
        contract_diagnostic=diagnostic,
        failure_stage="akng_contract_validation",
        diagnostic_state=_diagnostic_state(diagnostic),
    )


def _empty_input_contract(exc: ResolverError) -> bool:
    return exc.code == "resolver_input_rejected" and not bool(
        exc.contract_diagnostic.get("item_info_present")
    )


def _eligible_alternate_input(exc: ResolverError) -> bool:
    return exc.code == "resolver_input_rejected" and exc.http_status_class in {None, "2xx", "4xx"}


def _contract_error(
    status_class: str | None,
    diagnostic: dict[str, object],
) -> ResolverError:
    return ResolverError(
        "resolver_contract_error",
        "The TikTok resolver returned an unexpected media contract.",
        fallback_allowed=True,
        http_status_class=status_class,
        technical=True,
        contract_diagnostic=diagnostic,
        failure_stage="akng_contract_validation",
        diagnostic_state=_diagnostic_state(diagnostic),
    )


def _resolver_error(
    code: str,
    message: str,
    *,
    status_class: str | None,
    diagnostic: dict[str, object],
    retryable: bool = False,
    fallback_allowed: bool = False,
) -> ResolverError:
    return ResolverError(
        code,
        message,
        retryable=retryable,
        fallback_allowed=fallback_allowed,
        http_status_class=status_class,
        contract_diagnostic=diagnostic,
        failure_stage="akng_contract_validation",
        diagnostic_state=_diagnostic_state(diagnostic),
    )


def _diagnostic_state(diagnostic: dict[str, object]) -> dict[str, object]:
    return {
        "provider_call_attempted": True,
        "provider_input_kind": diagnostic.get("provider_input_kind") or "unknown",
        "provider_status_class": diagnostic.get("provider_status_class") or "unknown",
        "contract_complete": bool(diagnostic.get("contract_complete")),
        "short_link_expansion_attempted": bool(
            diagnostic.get("short_link_expansion_attempted")
        ),
        "short_link_expansion_completed": bool(
            diagnostic.get("short_link_expansion_completed")
        ),
        "canonical_url_resolved": bool(diagnostic.get("canonical_url_resolved")),
        "numeric_id_resolved": bool(diagnostic.get("numeric_id_resolved")),
        "redirect_count": int(diagnostic.get("short_link_redirect_count") or 0),
        "raw_video_candidate_count": int(diagnostic.get("bitrate_info_count") or 0),
    }


def _video_candidate(value: object) -> tuple[MediaCandidate | None, str | None]:
    if not isinstance(value, dict):
        return None, "malformed_candidate"
    play_addr = value.get("PlayAddr")
    if not isinstance(play_addr, dict):
        return None, "missing_play_addr"
    raw_urls = play_addr.get("UrlList")
    if not isinstance(raw_urls, list):
        return None, "invalid_url_type"
    urls = _url_tuple(raw_urls)
    if not urls:
        return None, _url_rejection_reason(raw_urls)
    size = _positive_int(play_addr.get("DataSize"))
    if size is None:
        return None, "invalid_size"
    width = _positive_int(play_addr.get("Width"))
    height = _positive_int(play_addr.get("Height"))
    if width is None or height is None:
        return None, "invalid_dimensions"
    codec = _safe_codec(value.get("CodecType"))
    container = _safe_container(value.get("Format"))
    if codec is None:
        return None, "unsupported_codec"
    if container is None:
        return None, "unsupported_format"
    return (
        MediaCandidate(
            media_type="video",
            _urls=urls,
            width=width,
            height=height,
            size_bytes=size,
            codec=codec,
            container=container,
            bitrate=_positive_int(value.get("Bitrate")),
            quality_label=_untrusted_text(value.get("GearName"), 80),
            audio_state="unknown",
            candidate_id=_safe_identifier(value.get("GearName")),
        ),
        None,
    )


def _audio_candidate(value: object) -> tuple[MediaCandidate | None, str | None]:
    if not isinstance(value, dict):
        return None, "malformed_candidate"
    url_data = value.get("UrlList")
    if not isinstance(url_data, dict):
        return None, "invalid_url_type"
    urls = _url_tuple(
        [url_data.get("MainUrl"), url_data.get("BackupUrl"), url_data.get("FallbackUrl")]
    )
    if not urls:
        return None, _url_rejection_reason(list(url_data.values()))
    size = _positive_int(value.get("AudioDataSize"))
    if size is None:
        return None, "invalid_size"
    return (
        MediaCandidate(
            media_type="audio",
            _urls=urls,
            size_bytes=size,
            codec=_safe_codec(value.get("CodecType")),
            container=_safe_container(value.get("AudioFormat") or value.get("Format")),
            bitrate=_positive_int(value.get("Bitrate")),
            quality_label=_untrusted_text(value.get("AudioQualityString"), 80),
            audio_state="present",
            candidate_id=_safe_identifier(value.get("FileId")),
        ),
        None,
    )


def _progressive_video_candidate(video: dict[str, object]) -> MediaCandidate | None:
    play_addr = video.get("PlayAddrStruct")
    if not isinstance(play_addr, dict):
        return None
    raw_urls: list[object] = [video.get("playAddr"), video.get("downloadAddr")]
    structured_urls = play_addr.get("UrlList")
    if isinstance(structured_urls, list):
        raw_urls.extend(structured_urls)
    urls = _url_tuple(raw_urls)
    size = _positive_int(play_addr.get("DataSize"))
    width = _positive_int(play_addr.get("Width") or video.get("width"))
    height = _positive_int(play_addr.get("Height") or video.get("height"))
    if not urls or size is None or width is None or height is None:
        return None
    return MediaCandidate(
        media_type="video",
        _urls=urls,
        width=width,
        height=height,
        size_bytes=size,
        codec="",
        container="mp4",
        bitrate=_positive_int(video.get("bitrate")),
        quality_label="top_level_progressive",
        audio_state="unknown",
        candidate_id="top_level_progressive",
    )


def _source_context(
    item: dict[str, object],
    data: dict[str, object],
    duration: int | None,
) -> SourceContext:
    author = _mapping(item.get("author"))
    music = _mapping(item.get("music"))
    share_meta = _mapping(data.get("shareMeta"))
    caption = _untrusted_text(item.get("desc") or share_meta.get("desc"), 1500)
    hashtags: list[str] = []
    for challenge in item.get("challenges") if isinstance(item.get("challenges"), list) else []:
        value = _untrusted_text(_mapping(challenge).get("title"), 60)
        if value and value.casefold() not in {tag.casefold() for tag in hashtags}:
            hashtags.append(value)
        if len(hashtags) >= 20:
            break
    if len(hashtags) < 20:
        for extra in item.get("textExtra") if isinstance(item.get("textExtra"), list) else []:
            value = _untrusted_text(_mapping(extra).get("hashtagName"), 60)
            if value and value.casefold() not in {tag.casefold() for tag in hashtags}:
                hashtags.append(value)
            if len(hashtags) >= 20:
                break
    on_screen: list[str] = []
    for sticker in item.get("stickersOnItem") if isinstance(item.get("stickersOnItem"), list) else []:
        sticker_text = _mapping(sticker).get("stickerText")
        values = sticker_text if isinstance(sticker_text, list) else []
        for raw in values:
            remaining = 500 - sum(len(text) for text in on_screen)
            if remaining <= 0:
                break
            value = _untrusted_text(raw, min(250, remaining))
            if value and value.casefold() not in {text.casefold() for text in on_screen}:
                on_screen.append(value)
            if sum(len(text) for text in on_screen) >= 500:
                break
    return SourceContext(
        caption=caption,
        creator_display_name=_untrusted_text(author.get("nickname"), 120),
        duration_seconds=duration,
        text_language=_safe_identifier(item.get("textLanguage")),
        hashtags=tuple(hashtags),
        on_screen_text=tuple(on_screen),
        music_title=_untrusted_text(music.get("title"), 120),
        music_author=_untrusted_text(music.get("authorName"), 120),
    )


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _url_tuple(value: object) -> tuple[str, ...]:
    values = value if isinstance(value, list) else []
    urls: list[str] = []
    for item in values:
        url = _https_url(item)
        if url and url not in urls:
            urls.append(url)
    return tuple(urls[:3])


def _url_rejection_reason(values: list[object]) -> str:
    nonempty = [value for value in values if value is not None and value != ""]
    if not nonempty:
        return "empty_url_list"
    if any(not isinstance(value, str) for value in nonempty):
        return "invalid_url_type"
    if any(urlparse(str(value)).scheme.lower() != "https" for value in nonempty):
        return "non_https_url"
    return "malformed_candidate"


def _https_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        parsed = urlparse(text)
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.port not in {None, 443}
    ):
        return None
    return text


def _positive_int(value: object) -> int | None:
    return _bounded_int(value, minimum=1, maximum=2**63 - 1)


def _bounded_int(value: object, *, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if minimum <= parsed <= maximum else None


def _safe_codec(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text[:40] if re.fullmatch(r"[a-z0-9_.-]+", text) else None


def _safe_container(value: object) -> str | None:
    text = str(value or "").strip().lower()
    return text[:20] if re.fullmatch(r"[a-z0-9_.-]+", text) else None


def _untrusted_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value)
    text = re.sub(r"https?://\S+|www\.\S+", " ", text, flags=re.I)
    text = re.sub(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", " ", text)
    text = re.sub(r"(?<!\d)(?:\+?\d[\d\s().-]{6,}\d)(?!\d)", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit].rstrip() if text else None


def _safe_identifier(value: object) -> str | None:
    text = str(value or "").strip()
    return text[:80] if re.fullmatch(r"[A-Za-z0-9_.-]+", text) else None


def _message_code(value: object) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")[:80]


def _safe_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    return "other"
