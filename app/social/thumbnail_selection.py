from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

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
    selected: bool


def no_thumbnail() -> ThumbnailSelection:
    return ThumbnailSelection(url=None, source="fallback", selected=False)


def platform_thumbnail(url: Any) -> ThumbnailSelection:
    safe_url = safe_image_url(url)
    if not safe_url:
        return no_thumbnail()
    return ThumbnailSelection(
        url=safe_url,
        source="platform_thumbnail",
        selected=True,
    )


def select_ytdlp_thumbnail(info: dict[str, Any]) -> ThumbnailSelection:
    direct = safe_image_url(info.get("thumbnail"))
    if direct and not looks_like_avatar({"url": direct}):
        return ThumbnailSelection(
            url=direct,
            source="ytdlp_thumbnail",
            selected=True,
        )

    array_selection = select_from_thumbnails_array(info.get("thumbnails"))
    if array_selection.selected:
        return array_selection
    if direct:
        return ThumbnailSelection(
            url=direct,
            source="ytdlp_thumbnail",
            selected=True,
        )
    return no_thumbnail()


def select_from_thumbnails_array(value: Any) -> ThumbnailSelection:
    if not isinstance(value, list):
        return no_thumbnail()
    candidates = [item for item in value if isinstance(item, dict) and safe_image_url(item.get("url"))]
    if not candidates:
        return no_thumbnail()
    preferred = [item for item in candidates if not looks_like_avatar(item)]
    selected = max(preferred or candidates, key=thumbnail_score)
    return ThumbnailSelection(
        url=safe_image_url(selected.get("url")),
        source="ytdlp_thumbnails_array",
        selected=True,
    )


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
