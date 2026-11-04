"""Shared dependencies. Tests override get_http to talk to the mock Amazon app in-process."""
from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import Depends

from app.amazon.client import AmazonClient
from app.config import get_settings
from app.models import Tenant
from app.security import decrypt, get_tenant


async def get_http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=30) as http:
        yield http


def build_client(tenant: Tenant, http: httpx.AsyncClient) -> AmazonClient:
    s = get_settings()
    return AmazonClient(
        http=http, refresh_token=decrypt(tenant.refresh_token_enc), client_id=s.lwa_client_id,
        client_secret=s.lwa_client_secret, lwa_url=s.lwa_url, sp_api_url=s.sp_api_url, ads_api_url=s.ads_api_url,
        seller_id=tenant.seller_id, ads_profile_id=tenant.ads_profile_id, max_retries=s.amazon_max_retries,
        max_concurrency=s.amazon_max_concurrency, report_poll_seconds=s.amazon_report_poll_seconds,
    )


async def get_amazon_client(
    tenant: Annotated[Tenant, Depends(get_tenant)], http: Annotated[httpx.AsyncClient, Depends(get_http)],
) -> AmazonClient:
    return build_client(tenant, http)
