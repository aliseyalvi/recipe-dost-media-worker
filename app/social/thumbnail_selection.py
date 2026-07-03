from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

THUMBNAIL_STRATEGY = "platform_first_v2"
YOUTUBE_THUMBNAIL_STRATEGY = "platform_first_v3"
GOOD_PLATFORM_SOURCES = {
    "youtube_oembed",
    "youtube_ytdlp_thumbnail",
    "youtube_ytdlp_array",
    "youtube_deterministic",
    "instagram_ytdlp_thumbnail",
    "instagram_ytdlp_array",
    "tiktok_ytdlp_thumbnail",
    "tiktok_ytdlp_array",
    "existing_platform_thumbnail",
}
LOW_PRIORITY_SOURCES = {
    "early_frame_fallback",
    "placeholder",
    "sampled_frame",
    "media_job_frame",
    "fallback",
    "youtube_oembed_frame_variant",
}
VIDEO_PATH_PATTERN = re.compile(
    r"\.(?:mp4|mov|webm|m4v|m3u8)(?:$|[?#])",
    re.IGNORECASE,
)
AVATAR_HINTS = (
    "avatar",
    "profile",
    "author",
    "userpic",
    "user_pic",
    "headshot",
    "s150x150",
    "s100x100",
)


@dataclass(frozen=True)
class ThumbnailSelection:
    url: str | None
    source: str
    provider: str
    strategy: str
    candidate_count: int
    selected: bool
    candidate_kind: str | None = None
    validation_status: str | None = None


def no_thumbnail(provider: str = "unknown") -> ThumbnailSelection:
    return ThumbnailSelection(
        url=None,
        source="placeholder",
        provider=provider,
        strategy=strategy_for_provider(provider),
        candidate_count=0,
        selected=False,
        candidate_kind="none",
        validation_status="not_checked",
    )


def platform_thumbnail(
    url: Any,
    *,
    source: str = "existing_platform_thumbnail",
    provider: str = "platform",
) -> ThumbnailSelection:
    safe_url = safe_image_url(url)
    if not safe_url:
        return no_thumbnail(provider)
    return ThumbnailSelection(
        url=safe_url,
        source=source,
        provider=provider,
        strategy=strategy_for_provider(provider),
        candidate_count=1,
        selected=True,
        candidate_kind="platform",
        validation_status="not_checked",
    )


def select_ytdlp_thumbnail(info: dict[str, Any], platform: str) -> ThumbnailSelection:
    provider = normalized_provider(platform)
    direct_source = f"{provider}_ytdlp_thumbnail"
    array_source = f"{provider}_ytdlp_array"
    direct = safe_image_url(info.get("thumbnail"))
    if direct and not looks_like_avatar({"url": direct}):
        return ThumbnailSelection(
            url=direct,
            source=direct_source,
            provider=provider,
            strategy=strategy_for_provider(provider),
            candidate_count=1,
            selected=True,
            candidate_kind="thumbnail",
            validation_status="not_checked",
        )

    array_selection = select_from_thumbnails_array(
        info.get("thumbnails"),
        source=array_source,
        provider=provider,
    )
    if array_selection.selected:
        return array_selection
    if direct:
        return ThumbnailSelection(
            url=direct,
            source=direct_source,
            provider=provider,
            strategy=strategy_for_provider(provider),
            candidate_count=1,
            selected=True,
            candidate_kind="thumbnail",
            validation_status="not_checked",
        )
    return no_thumbnail(provider)


def select_from_thumbnails_array(
    value: Any,
    *,
    source: str,
    provider: str,
) -> ThumbnailSelection:
    if not isinstance(value, list):
        return no_thumbnail(provider)
    candidates = [item for item in value if isinstance(item, dict) and safe_image_url(item.get("url"))]
    if not candidates:
        return no_thumbnail(provider)
    preferred = [item for item in candidates if not looks_like_avatar(item)]
    selected = max(preferred or candidates, key=thumbnail_score)
    return ThumbnailSelection(
        url=safe_image_url(selected.get("url")),
        source=source,
        provider=provider,
        strategy=strategy_for_provider(provider),
        candidate_count=len(candidates),
        selected=True,
        candidate_kind=str(selected.get("id") or "array"),
        validation_status="not_checked",
    )


def early_frame_thumbnail_selected(provider: str = "worker") -> ThumbnailSelection:
    return ThumbnailSelection(
        url=None,
        source="early_frame_fallback",
        provider=provider,
        strategy=strategy_for_provider(provider),
        candidate_count=4,
        selected=True,
        candidate_kind="early_frame",
        validation_status="not_checked",
    )


def select_youtube_oembed_thumbnail(
    *,
    video_id: str | None,
    oembed_url: Any,
) -> ThumbnailSelection:
    safe_oembed_url = safe_image_url(oembed_url)
    candidate_count = len(build_youtube_thumbnail_candidates(video_id))
    if safe_oembed_url and not is_low_priority_youtube_frame_variant(safe_oembed_url):
        return ThumbnailSelection(
            url=safe_oembed_url,
            source="youtube_oembed",
            provider="youtube",
            strategy=YOUTUBE_THUMBNAIL_STRATEGY,
            candidate_count=max(1, candidate_count + 1),
            selected=True,
            candidate_kind=youtube_candidate_kind(safe_oembed_url) or "oembed",
            validation_status="not_checked",
        )

    deterministic = select_valid_youtube_deterministic_thumbnail(video_id)
    if deterministic.selected:
        return deterministic

    if safe_oembed_url:
        return ThumbnailSelection(
            url=safe_oembed_url,
            source="youtube_oembed_frame_variant",
            provider="youtube",
            strategy=YOUTUBE_THUMBNAIL_STRATEGY,
            candidate_count=max(1, candidate_count + 1),
            selected=True,
            candidate_kind=youtube_candidate_kind(safe_oembed_url) or "oembed_frame_variant",
            validation_status="deterministic_unavailable",
        )
    return no_thumbnail("youtube")


def select_valid_youtube_deterministic_thumbnail(video_id: str | None) -> ThumbnailSelection:
    candidates = build_youtube_thumbnail_candidates(video_id)
    for candidate in candidates:
        status = validate_youtube_thumbnail_candidate(candidate["url"])
        if status == "ok":
            return ThumbnailSelection(
                url=candidate["url"],
                source="youtube_deterministic",
                provider="youtube",
                strategy=YOUTUBE_THUMBNAIL_STRATEGY,
                candidate_count=len(candidates),
                selected=True,
                candidate_kind=candidate["kind"],
                validation_status=status,
            )
    return ThumbnailSelection(
        url=None,
        source="placeholder",
        provider="youtube",
        strategy=YOUTUBE_THUMBNAIL_STRATEGY,
        candidate_count=len(candidates),
        selected=False,
        candidate_kind="deterministic",
        validation_status="not_found",
    )


def build_youtube_thumbnail_candidates(video_id: str | None) -> list[dict[str, str]]:
    if not video_id:
        return []
    return [
        {
            "kind": "maxresdefault",
            "url": f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
        },
        {
            "kind": "sddefault",
            "url": f"https://i.ytimg.com/vi/{video_id}/sddefault.jpg",
        },
        {
            "kind": "hqdefault",
            "url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        },
        {
            "kind": "mqdefault",
            "url": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
        },
        {
            "kind": "webp_maxresdefault",
            "url": f"https://i.ytimg.com/vi_webp/{video_id}/maxresdefault.webp",
        },
        {
            "kind": "webp_sddefault",
            "url": f"https://i.ytimg.com/vi_webp/{video_id}/sddefault.webp",
        },
        {
            "kind": "webp_hqdefault",
            "url": f"https://i.ytimg.com/vi_webp/{video_id}/hqdefault.webp",
        },
    ]


def validate_youtube_thumbnail_candidate(url: str) -> str:
    try:
        response = requests.head(url, timeout=(3, 5), allow_redirects=True)
        if response.status_code in {403, 405}:
            response = requests.get(
                url,
                timeout=(3, 5),
                allow_redirects=True,
                headers={"Range": "bytes=0-2047", "User-Agent": "Mozilla/5.0"},
                stream=True,
            )
        return validation_status_from_response(response)
    except requests.RequestException:
        return "request_failed"


def validation_status_from_response(response: requests.Response) -> str:
    if response.status_code != 200:
        return f"http_{response.status_code}"
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if not content_type.startswith("image/"):
        return "not_image"
    content_length = safe_int(response.headers.get("content-length"))
    if content_length is not None and content_length < 1024:
        return "too_small"
    return "ok"


def is_low_priority_youtube_frame_variant(url: str | None) -> bool:
    safe_url = safe_image_url(url)
    if not safe_url:
        return False
    parsed = urlparse(safe_url)
    if "ytimg.com" not in (parsed.hostname or "").lower():
        return False
    filename = parsed.path.rsplit("/", 1)[-1].lower()
    return bool(re.fullmatch(r"(?:hq|mq|sd|default)[1-9]\.(?:jpg|webp)", filename))


def youtube_candidate_kind(url: str | None) -> str | None:
    safe_url = safe_image_url(url)
    if not safe_url:
        return None
    filename = urlparse(safe_url).path.rsplit("/", 1)[-1].lower()
    return filename.rsplit(".", 1)[0] if filename else None


def should_update_thumbnail(
    *,
    existing_url: str | None,
    existing_source: str | None,
    new_source: str | None,
) -> bool:
    if not existing_url:
        return True
    if is_good_existing_thumbnail(existing_url, existing_source) and (
        new_source in LOW_PRIORITY_SOURCES or not new_source
    ):
        return False
    return True


def is_good_existing_thumbnail(url: str | None, source: str | None) -> bool:
    if source in GOOD_PLATFORM_SOURCES:
        return True
    safe_url = safe_image_url(url)
    if not safe_url:
        return False
    host = (urlparse(safe_url).hostname or "").lower()
    return any(
        token in host
        for token in (
            "i.ytimg.com",
            "ytimg.com",
            "tiktokcdn",
            "cdninstagram.com",
            "fbcdn.net",
        )
    )


def normalized_provider(platform: str) -> str:
    value = (platform or "").strip().lower()
    return value if value in {"youtube", "instagram", "tiktok", "facebook"} else "platform"


def strategy_for_provider(provider: str) -> str:
    return YOUTUBE_THUMBNAIL_STRATEGY if provider == "youtube" else THUMBNAIL_STRATEGY


def isLowPriorityYoutubeFrameVariant(url: str | None) -> bool:
    return is_low_priority_youtube_frame_variant(url)


def buildYoutubeThumbnailCandidates(video_id: str | None) -> list[dict[str, str]]:
    return build_youtube_thumbnail_candidates(video_id)


def thumbnail_score(item: dict[str, Any]) -> tuple[int, int, int]:
    width = safe_int(item.get("width")) or 0
    height = safe_int(item.get("height")) or 0
    area = width * height
    tiny_penalty = 0 if width < 160 or height < 120 else 1
    preference = safe_int(item.get("preference")) or 0
    return (tiny_penalty, area, preference)


def safe_image_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith(("http://", "https://")):
        return None
    parsed = urlparse(value)
    if parsed.username or parsed.password or not parsed.hostname:
        return None
    lowered = value.lower()
    if VIDEO_PATH_PATTERN.search(parsed.path.lower()) or "m3u8" in lowered:
        return None
    return value


def looks_like_avatar(item: dict[str, Any]) -> bool:
    text = " ".join(
        str(item.get(key) or "").lower()
        for key in ("id", "url", "name", "note", "resolution")
    )
    width = safe_int(item.get("width"))
    height = safe_int(item.get("height"))
    square_tiny = (
        width is not None
        and height is not None
        and width <= 180
        and height <= 180
        and abs(width - height) <= 20
    )
    return square_tiny or any(hint in text for hint in AVATAR_HINTS)


def safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
