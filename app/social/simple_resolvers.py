from __future__ import annotations

import re
from html import unescape
from typing import Any

import requests

from app.config import Settings
from app.social.models import SocialResolverError, SocialVideoInfo

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def resolve_with_snapinsta(source_url: str, settings: Settings) -> SocialVideoInfo:
    if not settings.enable_render_instagram_resolver:
        raise SocialResolverError(
            "instagram_resolver_disabled",
            "Instagram processing is not enabled yet.",
            {"platform": "instagram", "resolver": "snapinsta"},
        )
    return _resolve_with_form_adapter(
        source_url,
        platform="instagram",
        resolver="snapinsta",
        endpoint=settings.snapinsta_api_url,
        data={"url": source_url, "action": "post"},
        failed_code="instagram_resolver_failed",
        failed_message="Instagram metadata could not be resolved.",
    )


def resolve_with_snapsave(source_url: str, settings: Settings) -> SocialVideoInfo:
    if not settings.enable_render_facebook_resolver:
        raise SocialResolverError(
            "facebook_resolver_disabled",
            "Facebook processing is not enabled yet.",
            {"platform": "facebook", "resolver": "snapsave"},
        )
    return _resolve_with_form_adapter(
        source_url,
        platform="facebook",
        resolver="snapsave",
        endpoint=settings.snapsave_api_url,
        data={"url": source_url},
        failed_code="facebook_resolver_failed",
        failed_message="Facebook metadata could not be resolved.",
    )


def _resolve_with_form_adapter(
    source_url: str,
    *,
    platform: str,
    resolver: str,
    endpoint: str,
    data: dict[str, str],
    failed_code: str,
    failed_message: str,
) -> SocialVideoInfo:
    try:
        response = requests.post(
            endpoint.rstrip("/"),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json,text/html,*/*",
            },
            data=data,
            timeout=20,
        )
    except requests.RequestException as exc:
        raise SocialResolverError(
            failed_code,
            failed_message,
            {"platform": platform, "resolver": resolver},
        ) from exc
    if response.status_code >= 400:
        raise SocialResolverError(
            failed_code,
            failed_message,
            {"platform": platform, "resolver": resolver, "status_code": response.status_code},
        )

    payload = parse_adapter_response(response)
    if not payload.get("media_url"):
        raise SocialResolverError(
            failed_code,
            failed_message,
            {"platform": platform, "resolver": resolver},
        )

    return SocialVideoInfo(
        platform=platform,
        resolver=resolver,
        title=safe_str(payload.get("title")),
        video_id=None,
        duration_seconds=None,
        thumbnail_url=safe_http_url(payload.get("thumbnail_url")),
        webpage_url=source_url,
        stream_url=str(payload["media_url"]),
        stream_headers={"User-Agent": USER_AGENT},
        ext=safe_ext(payload.get("media_url")),
        mime_type=safe_str(payload.get("mime_type")) or "video/mp4",
        extractor=resolver,
        filename=None,
        uploader=safe_str(payload.get("author_username")),
        filesize_approx=safe_int(payload.get("size_bytes")),
        raw_metadata={
            "title": safe_str(payload.get("title")),
            "description": safe_str(payload.get("description")),
            "thumbnail_present": bool(safe_http_url(payload.get("thumbnail_url"))),
            "media_present": True,
            "mime_type": safe_str(payload.get("mime_type")) or "video/mp4",
        },
        description=safe_str(payload.get("description") or payload.get("title")),
        width=safe_int(payload.get("width")),
        height=safe_int(payload.get("height")),
    )


def parse_adapter_response(response: requests.Response) -> dict[str, object]:
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type == "application/json" or response.text.lstrip().startswith(("{", "[")):
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return flatten_json_payload(parsed)
        if isinstance(parsed, list):
            return flatten_json_payload({"items": parsed})
    return parse_adapter_html(response.text)


def flatten_json_payload(payload: dict[str, Any]) -> dict[str, object]:
    values = list(walk_values(payload))
    media_url = first_url(
        values,
        suffixes=(".mp4", ".mov", ".webm", ".m4v"),
        keys=("url", "download", "video", "media"),
    )
    thumbnail_url = first_url(
        values,
        suffixes=(".jpg", ".jpeg", ".png", ".webp"),
        keys=("thumbnail", "thumb", "image", "cover"),
    )
    return {
        "media_url": media_url,
        "thumbnail_url": thumbnail_url,
        "title": first_text(payload, ("title", "caption", "description")),
        "description": first_text(payload, ("description", "caption", "title")),
        "mime_type": "video/mp4" if media_url else None,
    }


def parse_adapter_html(html: str) -> dict[str, object]:
    title = first_meta_content(html, "og:title") or first_title(html)
    description = first_meta_content(html, "og:description") or title
    thumbnail_url = first_meta_content(html, "og:image")
    media_url = first_meta_content(html, "og:video") or first_media_href(html)
    return {
        "media_url": media_url,
        "thumbnail_url": thumbnail_url,
        "title": title,
        "description": description,
        "mime_type": "video/mp4" if media_url else None,
    }


def walk_values(value: Any) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append((str(key).lower(), item))
            found.extend(walk_values(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(walk_values(item))
    return found


def first_url(
    values: list[tuple[str, Any]],
    *,
    suffixes: tuple[str, ...],
    keys: tuple[str, ...],
) -> str | None:
    for key, value in values:
        if not isinstance(value, str):
            continue
        url = safe_http_url(value)
        if not url:
            continue
        lower = url.lower().split("?", 1)[0]
        if any(part in key for part in keys) or lower.endswith(suffixes):
            return url
    return None


def first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key, value in walk_values(payload):
        if key in keys and isinstance(value, str) and not safe_http_url(value):
            cleaned = " ".join(value.split())
            if cleaned:
                return cleaned[:500]
    return None


def first_meta_content(html: str, property_name: str) -> str | None:
    pattern = (
        r'<meta[^>]+(?:property|name)=["\']'
        + re.escape(property_name)
        + r'["\'][^>]+content=["\']([^"\']+)["\']'
    )
    match = re.search(pattern, html, flags=re.IGNORECASE)
    return unescape(match.group(1)).strip() if match else None


def first_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    return " ".join(unescape(match.group(1)).split())[:500] if match else None


def first_media_href(html: str) -> str | None:
    match = re.search(
        r'https?://[^"\']+\.(?:mp4|mov|webm|m4v)(?:\?[^"\']*)?',
        html,
        flags=re.IGNORECASE,
    )
    return unescape(match.group(0)) if match else None


def safe_http_url(value: Any) -> str | None:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    return None


def safe_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def safe_ext(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    path = url.split("?", 1)[0].lower()
    for ext in ("mp4", "mov", "webm", "m4v"):
        if path.endswith(f".{ext}"):
            return ext
    return None
