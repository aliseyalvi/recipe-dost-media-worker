from __future__ import annotations

from app.config import Settings
from app.social.cobalt_resolver import resolve_with_cobalt
from app.social.models import SocialResolverError, SocialVideoInfo
from app.social.tikwm_resolver import resolve_with_tikwm


def resolve_social_video(
    source_url: str,
    platform: str,
    settings: Settings,
    *,
    resolver: str = "auto",
) -> SocialVideoInfo:
    requested = resolver.strip().lower()
    if platform == "youtube":
        raise SocialResolverError(
            "unsupported_social_platform",
            "YouTube uses the native worker path.",
            {"platform": platform, "resolver": requested},
        )
    if requested not in {"auto", "tikwm", "cobalt"}:
        raise SocialResolverError(
            "social_resolver_unavailable",
            "Requested resolver is not supported.",
            {"platform": platform, "resolver": requested},
        )
    if requested == "tikwm":
        if platform != "tiktok":
            raise SocialResolverError(
                "unsupported_social_platform",
                "TikWM is only available for TikTok.",
                {"platform": platform, "resolver": "tikwm"},
            )
        return resolve_with_tikwm(source_url, platform, settings)
    if requested == "cobalt":
        if platform not in {"instagram", "facebook"}:
            raise SocialResolverError(
                "unsupported_social_platform",
                "Cobalt is only enabled for Instagram and Facebook.",
                {"platform": platform, "resolver": "cobalt"},
            )
        return resolve_with_cobalt(source_url, platform, settings)
    if platform == "tiktok":
        if "tiktok" not in settings.tikwm_enabled_platforms:
            raise SocialResolverError(
                "social_resolver_unavailable",
                "No TikTok resolver is enabled.",
                {"platform": platform, "resolver": "auto"},
            )
        return resolve_with_tikwm(source_url, platform, settings)
    if platform in {"instagram", "facebook"}:
        if platform not in settings.cobalt_enabled_platforms:
            raise SocialResolverError(
                "social_resolver_unavailable",
                "No resolver is enabled for this social platform.",
                {"platform": platform, "resolver": "auto"},
            )
        return resolve_with_cobalt(source_url, platform, settings)
    raise SocialResolverError(
        "unsupported_social_platform",
        "This social platform is not supported.",
        {"platform": platform, "resolver": requested},
    )
