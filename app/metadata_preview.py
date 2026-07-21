from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import requests

from app.config import Settings
from app.social.platforms import detect_social_platform
from app.social.stream_download import (
    extract_opengraph_metadata,
)
from app.social.thumbnail_selection import (
    platform_thumbnail,
    safe_image_url as select_safe_image_url,
    select_youtube_oembed_thumbnail,
    select_ytdlp_thumbnail,
)
from app.social.yt_dlp_resolver import platform_headers, safe_float

SUPPORTED_METADATA_PLATFORMS = ["youtube", "instagram", "tiktok", "facebook", "other"]
SOCIAL_YTDLP_PLATFORMS = {"instagram", "tiktok", "facebook"}
URL_PATTERN = re.compile(r"https?://", re.IGNORECASE)
HASHTAG_PATTERN = re.compile(r"(?:^|\s)#[\w.-]+")


class MetadataPreviewError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class MetadataPreview:
    platform: str
    resolver: str
    title: str | None
    description: str | None
    thumbnail_url: str | None
    author_username: str | None
    duration_seconds: int | None
    tags: list[str]
    metadata_status: str
    is_too_long: bool
    max_duration_seconds: int
    thumbnail_source: str
    thumbnail_provider: str
    thumbnail_strategy: str
    thumbnail_candidate_count: int
    thumbnail_selected: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "resolver": self.resolver,
            "title": self.title,
            "description": self.description,
            "thumbnail_url": self.thumbnail_url,
            "author_username": self.author_username,
            "duration_seconds": self.duration_seconds,
            "tags": self.tags,
            "metadata_status": self.metadata_status,
            "is_too_long": self.is_too_long,
            "max_duration_seconds": self.max_duration_seconds,
            "thumbnail_source": self.thumbnail_source,
            "thumbnail_provider": self.thumbnail_provider,
            "thumbnail_strategy": self.thumbnail_strategy,
            "thumbnail_candidate_count": self.thumbnail_candidate_count,
            "thumbnail_selected": self.thumbnail_selected,
        }


def fetch_metadata_preview(
    *,
    url: str,
    platform: str | None,
    settings: Settings,
) -> MetadataPreview:
    source_url = validate_source_url(url)
    resolved_platform = normalize_platform(platform, source_url)
    if resolved_platform == "youtube":
        return fetch_youtube_preview(source_url, settings)
    if resolved_platform in SOCIAL_YTDLP_PLATFORMS:
        return fetch_social_ytdlp_preview(source_url, resolved_platform, settings)
    return fetch_opengraph_preview(source_url, "other", settings)


def validate_source_url(url: str) -> str:
    value = str(url or "").strip()
    if len(value) > 2048:
        raise MetadataPreviewError("invalid_url", "URL is too long.", 422)
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise MetadataPreviewError("invalid_url", "URL must be valid.", 422) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MetadataPreviewError("invalid_url", "URL must be http or https.", 422)
    if parsed.username or parsed.password:
        raise MetadataPreviewError("invalid_url", "URL credentials are not allowed.", 422)
    return value


def normalize_platform(platform: str | None, url: str) -> str:
    provided = str(platform or "").strip().lower()
    if provided in SUPPORTED_METADATA_PLATFORMS:
        return provided
    detected = detect_social_platform(url)
    if detected in {"youtube", "instagram", "tiktok", "facebook"}:
        return detected
    return "other"


def extract_youtube_video_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/", 1)[0]
        return candidate if is_youtube_video_id(candidate) else None
    query_id = parse_qs(parsed.query).get("v", [None])[0]
    if is_youtube_video_id(query_id):
        return query_id
    parts = [part for part in parsed.path.split("/") if part]
    for marker in ("shorts", "embed"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts) and is_youtube_video_id(parts[index + 1]):
                return parts[index + 1]
    return None


def is_youtube_video_id(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", value))


def fetch_youtube_preview(url: str, settings: Settings) -> MetadataPreview:
    try:
        response = requests.get(
            "https://www.youtube.com/oembed"
            f"?url={quote(url, safe='')}&format=json",
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
        if isinstance(body, dict):
            thumbnail = select_youtube_oembed_thumbnail(
                video_id=extract_youtube_video_id(url),
                oembed_url=body.get("thumbnail_url"),
            )
            preview = preview_from_mapping(
                platform="youtube",
                resolver="youtube_oembed",
                source={
                    "title": body.get("title"),
                    "description": body.get("title"),
                    "thumbnail": thumbnail,
                    "author_username": body.get("author_name"),
                },
                settings=settings,
            )
            if preview.title or preview.thumbnail_url:
                return preview
    except Exception:
        pass
    try:
        return fetch_ytdlp_preview(url, "youtube", settings, resolver="youtube_ytdlp")
    except MetadataPreviewError:
        deterministic = select_youtube_oembed_thumbnail(
            video_id=extract_youtube_video_id(url),
            oembed_url=None,
        )
        if deterministic.url:
            return preview_from_mapping(
                platform="youtube",
                resolver="youtube_deterministic",
                source={"thumbnail": deterministic},
                settings=settings,
            )
        return failed_preview("youtube", "youtube_ytdlp", settings)


def fetch_social_ytdlp_preview(
    url: str,
    platform: str,
    settings: Settings,
) -> MetadataPreview:
    try:
        return fetch_ytdlp_preview(url, platform, settings, resolver=f"{platform}_ytdlp")
    except MetadataPreviewError:
        return fetch_opengraph_preview(url, platform, settings)


def fetch_ytdlp_preview(
    url: str,
    platform: str,
    settings: Settings,
    *,
    resolver: str,
) -> MetadataPreview:
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise MetadataPreviewError(
            "metadata_unavailable",
            "Metadata extraction is not available.",
            503,
        ) from exc

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": settings.ytdlp_socket_timeout_seconds,
        "retries": settings.ytdlp_retries,
        "fragment_retries": settings.ytdlp_retries,
        "extractor_retries": settings.ytdlp_retries,
        "http_headers": platform_headers(platform),
    }
    if settings.ytdlp_user_agent:
        opts["http_headers"] = {
            **platform_headers(platform),
            "User-Agent": settings.ytdlp_user_agent,
        }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        raise MetadataPreviewError(
            "metadata_unavailable",
            "Metadata could not be fetched.",
            502,
        ) from exc
    if not isinstance(info, dict):
        raise MetadataPreviewError(
            "metadata_unavailable",
            "Metadata could not be fetched.",
            502,
        )
    thumbnail = select_ytdlp_thumbnail(info, platform)
    return preview_from_mapping(
        platform=platform,
        resolver=resolver,
        source={
            "title": info.get("title"),
            "description": info.get("description"),
            "thumbnail": thumbnail,
            "author_username": (
                info.get("uploader_id")
                or info.get("uploader")
                or info.get("channel")
                or info.get("creator")
            ),
            "duration_seconds": info.get("duration"),
            "tags": tags_from_ytdlp(info),
        },
        settings=settings,
    )


def fetch_opengraph_preview(
    url: str,
    platform: str,
    settings: Settings,
) -> MetadataPreview:
    result = extract_opengraph_metadata(url, platform, settings)
    if not result.get("success"):
        return failed_preview(
            platform,
            str(result.get("resolver") or f"{platform}_opengraph"),
            settings,
        )
    return preview_from_mapping(
        platform=platform,
        resolver=str(result.get("resolver") or f"{platform}_opengraph"),
        source={
            "title": result.get("title"),
            "description": result.get("description"),
            "thumbnail_url": result.get("thumbnail_url"),
            "thumbnail": platform_thumbnail(
                result.get("thumbnail_url"),
                source="existing_platform_thumbnail",
                provider=platform,
            ),
            "author_username": result.get("author_username"),
            "duration_seconds": result.get("duration_seconds"),
        },
        settings=settings,
    )


def failed_preview(platform: str, resolver: str, settings: Settings) -> MetadataPreview:
    thumbnail_strategy = "platform_first_v3" if platform == "youtube" else "platform_first_v2"
    return MetadataPreview(
        platform=platform,
        resolver=resolver,
        title=None,
        description=None,
        thumbnail_url=None,
        author_username=None,
        duration_seconds=None,
        tags=[],
        metadata_status="failed",
        is_too_long=False,
        max_duration_seconds=settings.max_video_duration_seconds,
        thumbnail_source="placeholder",
        thumbnail_provider=platform,
        thumbnail_strategy=thumbnail_strategy,
        thumbnail_candidate_count=0,
        thumbnail_selected=False,
    )


def preview_from_mapping(
    *,
    platform: str,
    resolver: str,
    source: dict[str, Any],
    settings: Settings,
) -> MetadataPreview:
    duration_seconds = safe_duration_seconds(source.get("duration_seconds"))
    title = clean_title(source.get("title"))
    description = clean_description(source.get("description"))
    thumbnail = source.get("thumbnail")
    thumbnail_url = safe_image_url(source.get("thumbnail_url"))
    thumbnail_source = "placeholder"
    thumbnail_provider = platform
    thumbnail_strategy = "platform_first_v2"
    thumbnail_candidate_count = 0
    thumbnail_selected = bool(thumbnail_url)
    if hasattr(thumbnail, "url"):
        thumbnail_url = safe_image_url(getattr(thumbnail, "url"))
        thumbnail_source = str(getattr(thumbnail, "source", "placeholder"))
        thumbnail_provider = str(getattr(thumbnail, "provider", platform))
        thumbnail_strategy = str(getattr(thumbnail, "strategy", "platform_first_v2"))
        thumbnail_candidate_count = int(getattr(thumbnail, "candidate_count", 0))
        thumbnail_selected = bool(getattr(thumbnail, "selected", False))
    tags = sanitize_tags(source.get("tags"))
    has_core_metadata = bool(title or description or thumbnail_url)
    metadata_status = "completed" if title and thumbnail_url else (
        "partial" if has_core_metadata else "failed"
    )
    return MetadataPreview(
        platform=platform,
        resolver=resolver,
        title=title,
        description=description,
        thumbnail_url=thumbnail_url,
        author_username=clean_text(source.get("author_username"), max_length=120),
        duration_seconds=duration_seconds,
        tags=tags,
        metadata_status=metadata_status,
        is_too_long=(
            duration_seconds is not None
            and duration_seconds > settings.max_video_duration_seconds
        ),
        max_duration_seconds=settings.max_video_duration_seconds,
        thumbnail_source=thumbnail_source,
        thumbnail_provider=thumbnail_provider,
        thumbnail_strategy=thumbnail_strategy,
        thumbnail_candidate_count=thumbnail_candidate_count,
        thumbnail_selected=thumbnail_selected,
    )


def clean_title(value: Any) -> str | None:
    text = clean_text(value, max_length=180)
    if not text:
        return None
    hashtags = HASHTAG_PATTERN.findall(text)
    if len(hashtags) >= 4:
        text = HASHTAG_PATTERN.sub(" ", text)
        text = compact_whitespace(text)
    return text[:180] or None


def clean_description(value: Any) -> str | None:
    return clean_text(value, max_length=600)


def clean_text(value: Any, *, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = html.unescape(value)
    text = compact_whitespace(text)
    if not text:
        return None
    return text[:max_length].rstrip()


def compact_whitespace(value: str) -> str:
    return " ".join(value.replace("\x00", " ").split())


def safe_duration_seconds(value: Any) -> int | None:
    parsed = safe_float(value)
    if parsed is None or parsed < 0:
        return None
    return int(parsed)


def safe_image_url(value: Any) -> str | None:
    return select_safe_image_url(value)


def tags_from_ytdlp(info: dict[str, Any]) -> list[str]:
    raw: list[Any] = []
    for key in ("tags", "categories"):
        value = info.get(key)
        if isinstance(value, list):
            raw.extend(value)
    return sanitize_tags(raw)


def sanitize_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    tags: list[str] = []
    seen: set[str] = set()
    for item in value:
        tag = clean_text(item, max_length=40)
        if not tag or URL_PATTERN.search(tag):
            continue
        tag = tag.lstrip("#").strip(" ,.;:").lower()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
        if len(tags) >= 8:
            break
    return tags
