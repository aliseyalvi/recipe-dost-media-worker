from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

THUMBNAIL_STRATEGY = "platform_first_v2"
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


def no_thumbnail(provider: str = "unknown") -> ThumbnailSelection:
    return ThumbnailSelection(
        url=None,
        source="placeholder",
        provider=provider,
        strategy=THUMBNAIL_STRATEGY,
        candidate_count=0,
        selected=False,
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
        strategy=THUMBNAIL_STRATEGY,
        candidate_count=1,
        selected=True,
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
            strategy=THUMBNAIL_STRATEGY,
            candidate_count=1,
            selected=True,
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
            strategy=THUMBNAIL_STRATEGY,
            candidate_count=1,
            selected=True,
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
        strategy=THUMBNAIL_STRATEGY,
        candidate_count=len(candidates),
        selected=True,
    )


def early_frame_thumbnail_selected(provider: str = "worker") -> ThumbnailSelection:
    return ThumbnailSelection(
        url=None,
        source="early_frame_fallback",
        provider=provider,
        strategy=THUMBNAIL_STRATEGY,
        candidate_count=4,
        selected=True,
    )


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
