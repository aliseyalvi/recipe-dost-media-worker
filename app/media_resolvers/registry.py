from __future__ import annotations

from app.config import Settings
from app.media_resolvers.contracts import MediaResolver, ResolverError
from app.media_resolvers.http_client import SafeProviderHttpClient
from app.media_resolvers.instagram_unified_v1 import (
    ADAPTER_ID as INSTAGRAM_UNIFIED_V1_ADAPTER_ID,
    InstagramUnifiedV1Resolver,
)
from app.media_resolvers.tiktok_akng import (
    ADAPTER_ID as TIKTOK_ADAPTER_ID,
    TikTokAkngResolver,
)


def registered_adapter_ids() -> set[str]:
    return {
        INSTAGRAM_UNIFIED_V1_ADAPTER_ID,
        TIKTOK_ADAPTER_ID,
    }


def build_resolver(adapter_id: str, settings: Settings) -> MediaResolver:
    client = SafeProviderHttpClient(
        timeout_seconds=settings.external_provider_timeout_seconds,
        response_max_bytes=settings.external_provider_response_max_bytes,
    )
    if adapter_id == INSTAGRAM_UNIFIED_V1_ADAPTER_ID:
        if settings.instagram_rapidapi_key is None:
            raise _configuration_error()
        return InstagramUnifiedV1Resolver(
            client=client,
            base_url=settings.instagram_rapidapi_base_url,
            rapidapi_host=settings.instagram_rapidapi_host,
            rapidapi_key=settings.instagram_rapidapi_key.get_secret_value(),
            allowed_media_host_suffixes=tuple(
                settings.instagram_media_host_suffixes
            ),
        )
    if adapter_id == TIKTOK_ADAPTER_ID:
        if settings.tiktok_akng_access_token is None:
            raise _configuration_error()
        return TikTokAkngResolver(
            client=client,
            base_url=settings.tiktok_akng_base_url,
            access_token=settings.tiktok_akng_access_token.get_secret_value(),
        )
    raise _configuration_error()


def _configuration_error() -> ResolverError:
    return ResolverError(
        "resolver_configuration_error",
        "External media resolution is not configured.",
        technical=True,
    )
