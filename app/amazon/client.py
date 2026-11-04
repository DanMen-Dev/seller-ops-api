"""Async client for Amazon's Selling Partner API (SP-API) and Advertising API.

What it handles, because Amazon's APIs demand it:
- Login with Amazon (LWA): exchanges the seller's refresh token for a 1-hour access token, caches it
  and refreshes it before it expires (or once more if Amazon answers 401).
- Rate limits: SP-API answers 429 when an operation's quota is spent. Retries use the Retry-After
  header when present, otherwise exponential backoff with jitter; 5xx and network errors retry too.
- Concurrency: a semaphore caps parallel requests per seller, so asyncio.gather() cannot flood Amazon.
- Pagination (nextToken) and asynchronous reports (create, poll, download).
"""
import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

import httpx

MARKETPLACE_IDS = {
    "ES": "A1RKKUPIHCS9HS", "DE": "A1PA6795UKMFR9", "FR": "A13V1IB3VIYZZH",
    "IT": "APJ6JRA9NG5V4", "UK": "A1F83G8C2ARO7P", "US": "ATVPDKIKX0DER",
}
RETRYABLE = {429, 500, 502, 503, 504}


class AmazonAPIError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"Amazon API error {status}: {body[:300]}")
        self.status = status


@dataclass(frozen=True)
class InventoryItem:
    sku: str
    asin: str
    title: str
    fulfillable: int
    inbound: int


@dataclass(frozen=True)
class Listing:
    sku: str
    asin: str
    title: str
    price: Decimal
    currency: str


class AmazonClient:
    def __init__(
        self, *, http: httpx.AsyncClient, refresh_token: str, client_id: str, client_secret: str,
        lwa_url: str, sp_api_url: str, ads_api_url: str, seller_id: str, ads_profile_id: str,
        max_retries: int = 5, max_concurrency: int = 4, report_poll_seconds: float = 2.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock: Callable[[], float] = time.monotonic,
    ):
        self.http = http
        self._refresh_token = refresh_token
        self._client_id, self._client_secret = client_id, client_secret
        self.lwa_url, self.sp_api_url, self.ads_api_url = lwa_url, sp_api_url, ads_api_url
        self.seller_id, self.ads_profile_id = seller_id, ads_profile_id
        self.max_retries = max_retries
        self.report_poll_seconds = report_poll_seconds
        self._sem = asyncio.Semaphore(max_concurrency)
        self._token_lock = asyncio.Lock()
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._sleep, self._clock = sleep, clock

    # ---------- auth and transport ----------

    async def _access_token(self) -> str:
        async with self._token_lock:  # concurrent requests share one refresh
            if self._token and self._clock() < self._token_expires_at - 60:
                return self._token
            resp = await self.http.post(f"{self.lwa_url}/auth/o2/token", data={
                "grant_type": "refresh_token", "refresh_token": self._refresh_token,
                "client_id": self._client_id, "client_secret": self._client_secret,
            })
            if resp.status_code != 200:
                raise AmazonAPIError(resp.status_code, resp.text)
            body = resp.json()
            self._token = body["access_token"]
            self._token_expires_at = self._clock() + int(body.get("expires_in", 3600))
            return self._token

    def _backoff(self, attempt: int, resp: httpx.Response | None) -> float:
        if resp is not None and resp.headers.get("Retry-After"):
            try:
                return float(resp.headers["Retry-After"])
            except ValueError:
                pass
        return random.uniform(0, min(30.0, 0.5 * 2 ** attempt))  # exponential backoff, "full jitter"

    async def _headers(self, auth: str) -> dict[str, str]:
        if auth == "none":  # pre-signed download URLs carry their own signature
            return {}
        token = await self._access_token()
        if auth == "ads":
            return {"Authorization": f"Bearer {token}", "Amazon-Advertising-API-ClientId": self._client_id,
                    "Amazon-Advertising-API-Scope": self.ads_profile_id}
        return {"x-amz-access-token": token}

    async def _request(self, method: str, url: str, *, auth: str = "sp", **kwargs) -> httpx.Response:
        """auth: "sp" (SP-API header), "ads" (Advertising API headers) or "none"."""
        refreshed = False
        async with self._sem:
            attempt = 0
            while True:
                headers = await self._headers(auth)
                resp = None
                try:
                    resp = await self.http.request(method, url, headers=headers, **kwargs)
                except httpx.TransportError:
                    if attempt >= self.max_retries:
                        raise
                else:
                    if resp.status_code == 401 and auth != "none" and not refreshed:  # token revoked or expired early
                        self._token, refreshed = None, True
                        continue
                    if resp.status_code not in RETRYABLE:
                        if resp.is_error:
                            raise AmazonAPIError(resp.status_code, resp.text)
                        return resp
                    if attempt >= self.max_retries:
                        raise AmazonAPIError(resp.status_code, resp.text)
                await self._sleep(self._backoff(attempt, resp))
                attempt += 1

    # ---------- SP-API ----------

    async def iter_inventory(self, marketplace: str) -> AsyncIterator[InventoryItem]:
        """FBA Inventory API, following nextToken until the last page."""
        params = {"granularityType": "Marketplace", "granularityId": MARKETPLACE_IDS[marketplace],
                  "marketplaceIds": MARKETPLACE_IDS[marketplace], "details": "true"}
        while True:
            body = (await self._request("GET", f"{self.sp_api_url}/fba/inventory/v1/summaries", params=params)).json()
            for s in body["payload"]["inventorySummaries"]:
                d = s.get("inventoryDetails", {})
                inbound = d.get("inboundWorkingQuantity", 0) + d.get("inboundShippedQuantity", 0)
                yield InventoryItem(s["sellerSku"], s.get("asin", ""), s.get("productName", ""),
                                    d.get("fulfillableQuantity", 0), inbound)
            next_token = (body.get("pagination") or {}).get("nextToken")
            if not next_token:
                return
            params = {**params, "nextToken": next_token}

    async def get_listing(self, sku: str, marketplace: str) -> Listing | None:
        resp = await self._request(
            "GET", f"{self.sp_api_url}/listings/2021-08-01/items/{self.seller_id}/{sku}",
            params={"marketplaceIds": MARKETPLACE_IDS[marketplace], "includedData": "summaries,offers"},
        )
        body = resp.json()
        offers = body.get("offers") or []
        if not offers:
            return None  # not listed in this marketplace
        summary = (body.get("summaries") or [{}])[0]
        price = offers[0]["price"]
        return Listing(sku, summary.get("asin", ""), summary.get("itemName", ""),
                       Decimal(str(price["amount"])), price["currencyCode"])

    async def get_fees(self, sku: str, marketplace: str, price: Decimal, currency: str) -> tuple[Decimal, Decimal]:
        """Product Fees API: returns (referral fee, FBA fee) for one unit at this price."""
        resp = await self._request("POST", f"{self.sp_api_url}/products/fees/v0/listings/{sku}/feesEstimate", json={
            "FeesEstimateRequest": {
                "MarketplaceId": MARKETPLACE_IDS[marketplace], "IsAmazonFulfilled": True, "Identifier": sku,
                "PriceToEstimateFees": {"ListingPrice": {"CurrencyCode": currency, "Amount": float(price)}},
            }
        })
        details = resp.json()["payload"]["FeesEstimateResult"]["FeesEstimate"]["FeeDetailList"]
        fees = {f["FeeType"]: Decimal(str(f["FinalFee"]["Amount"])) for f in details}
        return fees.get("ReferralFee", Decimal("0")), fees.get("FBAFees", Decimal("0"))

    async def get_daily_sales(self, sku: str, marketplace: str, start: date, end: date) -> list[tuple[date, int, Decimal, str]]:
        """Sales API orderMetrics by day for one SKU: [(day, units, revenue, currency)], end exclusive."""
        resp = await self._request("GET", f"{self.sp_api_url}/sales/v1/orderMetrics", params={
            "marketplaceIds": MARKETPLACE_IDS[marketplace], "granularity": "Day", "sku": sku,
            "interval": f"{start.isoformat()}T00:00:00Z--{end.isoformat()}T00:00:00Z",
        })
        rows = []
        for m in resp.json()["payload"]:
            day = date.fromisoformat(m["interval"][:10])
            rows.append((day, m["unitCount"], Decimal(str(m["totalSales"]["amount"])), m["totalSales"]["currencyCode"]))
        return rows

    # ---------- Advertising API ----------

    async def keyword_report(self, start: date, end: date, max_polls: int = 30) -> list[dict]:
        """Sponsored Products keyword report (Ads API v3): reports are asynchronous, so create, poll, download."""
        created = await self._request("POST", f"{self.ads_api_url}/reporting/reports", auth="ads", json={
            "startDate": start.isoformat(), "endDate": (end - timedelta(days=1)).isoformat(),
            "configuration": {"adProduct": "SPONSORED_PRODUCTS", "reportTypeId": "spTargeting", "groupBy": ["targeting"],
                              "timeUnit": "DAILY", "format": "GZIP_JSON"},
        })
        report_id = created.json()["reportId"]
        for _ in range(max_polls):
            status = (await self._request("GET", f"{self.ads_api_url}/reporting/reports/{report_id}", auth="ads")).json()
            if status["status"] == "COMPLETED":
                return (await self._request("GET", status["url"], auth="none")).json()
            if status["status"] == "FAILED":
                raise AmazonAPIError(500, f"report {report_id} failed")
            await self._sleep(self.report_poll_seconds)
        raise TimeoutError(f"report {report_id} not ready after {max_polls} polls")

    async def update_keywords(self, changes: list[dict]) -> None:
        """changes: [{"keywordId": ..., "bid": 0.55}] or [{"keywordId": ..., "state": "PAUSED"}]"""
        await self._request("PUT", f"{self.ads_api_url}/sp/keywords", auth="ads", json={"keywords": changes})
