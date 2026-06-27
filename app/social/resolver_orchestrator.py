from __future__ import annotations

from app.config import Settings
from app.social.models import SocialResolverError, SocialVideoInfo
from app.social.simple_resolvers import resolve_with_snapinsta, resolve_with_snapsave
from app.social.tikwm_resolver import resolve_with_tikwm
from app.social.yt_dlp_resolver import resolve_youtube_metadata


def resolve_social_video(
    source_url: str,
    platform: str,
    settings: Settings,
    *,
    resolver: str = "auto",
) -> SocialVideoInfo:
    requested = resolver.strip().lower()
    if requested not in {"auto", "tikwm", "ytdlp", "snapinsta", "snapsave"}:
        raise SocialResolverError(
            "social_resolver_unavailable",
            "Requested resolver is not supported.",
            {"platform": platform, "resolver": requested},
        )
    if requested == "ytdlp":
        if platform != "youtube":
            raise SocialResolverError(
                "unsupported_social_platform",
                "yt-dlp metadata resolver is only enabled for YouTube.",
                {"platform": platform, "resolver": "ytdlp"},
            )
        return resolve_youtube_metadata(source_url, settings)
    if requested == "tikwm":
        if platform != "tiktok":
            raise SocialResolverError(
                "unsupported_social_platform",
                "TikWM is only available for TikTok.",
                {"platform": platform, "resolver": "tikwm"},
            )
        return resolve_with_tikwm(source_url, platform, settings)
    if requested == "snapinsta":
        if platform != "instagram":
            raise SocialResolverError(
                "unsupported_social_platform",
                "SnapInsta is only enabled for Instagram.",
                {"platform": platform, "resolver": "snapinsta"},
            )
        return resolve_with_snapinsta(source_url, settings)
    if requested == "snapsave":
        if platform != "facebook":
            raise SocialResolverError(
                "unsupported_social_platform",
                "SnapSave is only enabled for Facebook.",
                {"platform": platform, "resolver": "snapsave"},
            )
        return resolve_with_snapsave(source_url, settings)
    if platform == "youtube":
        return resolve_youtube_metadata(source_url, settings)
    if platform == "tiktok":
        if "tiktok" not in settings.tikwm_enabled_platforms:
            raise SocialResolverError(
                "social_resolver_unavailable",
                "No TikTok resolver is enabled.",
                {"platform": platform, "resolver": "auto"},
            )
        return resolve_with_tikwm(source_url, platform, settings)
    if platform == "instagram":
        return resolve_with_snapinsta(source_url, settings)
    if platform == "facebook":
        return resolve_with_snapsave(source_url, settings)
    raise SocialResolverError(
        "unsupported_social_platform",
        "This social platform is not supported.",
        {"platform": platform, "resolver": requested},
    )
