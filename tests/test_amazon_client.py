"""The Amazon client's resilience: tokens, retries, limits and pagination."""
from datetime import date

import httpx
import pytest

from app.amazon.client import AmazonAPIError, AmazonClient
from tests.conftest import amazon_http


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def make_client(handler, sleeps=None, clock=None, max_retries=3):
    async def fake_sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)

    return AmazonClient(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), refresh_token="Atzr|x", client_id="id",
        client_secret="secret", lwa_url="https://lwa", sp_api_url="https://sp", ads_api_url="https://ads",
        seller_id="S", ads_profile_id="P", max_retries=max_retries, sleep=fake_sleep, clock=clock or FakeClock(),
    )


def router(responses: list[httpx.Response], calls: dict):
    """LWA always succeeds; API calls get the scripted responses in order."""
    def handler(request: httpx.Request):
        if request.url.host == "lwa":
            calls["token"] = calls.get("token", 0) + 1
            return httpx.Response(200, json={"access_token": f"Atza|{calls['token']}", "expires_in": 3600})
        calls.setdefault("api", []).append(request.headers.get("x-amz-access-token"))
        return responses.pop(0)
    return handler


OK = httpx.Response(200, json={"payload": []})


async def test_retries_429_honouring_retry_after():
    calls, sleeps = {}, []
    client = make_client(router([httpx.Response(429, headers={"Retry-After": "1.5"}),
                                 httpx.Response(503), OK], calls), sleeps)
    resp = await client._request("GET", "https://sp/sales")
    assert resp.status_code == 200
    assert len(calls["api"]) == 3
    assert sleeps[0] == 1.5 and 0 <= sleeps[1] <= 1.0   # Retry-After, then backoff with jitter


async def test_gives_up_after_max_retries():
    calls = {}
    client = make_client(router([httpx.Response(429)] * 4, calls), sleeps=[], max_retries=3)
    with pytest.raises(AmazonAPIError) as err:
        await client._request("GET", "https://sp/sales")
    assert err.value.status == 429 and len(calls["api"]) == 4


async def test_client_errors_are_not_retried():
    calls = {}
    client = make_client(router([httpx.Response(400, json={"errors": ["bad marketplace"]})], calls), sleeps=[])
    with pytest.raises(AmazonAPIError, match="400"):
        await client._request("GET", "https://sp/sales")
    assert len(calls["api"]) == 1


async def test_access_token_is_cached_and_refreshed_before_expiry():
    calls, clock = {}, FakeClock()
    client = make_client(router([OK, OK, OK], calls), clock=clock)
    await client._request("GET", "https://sp/a")
    await client._request("GET", "https://sp/b")
    assert calls["token"] == 1
    clock.now = 3600 - 30          # inside the last minute of the token's life
    await client._request("GET", "https://sp/c")
    assert calls["token"] == 2 and calls["api"] == ["Atza|1", "Atza|1", "Atza|2"]


async def test_401_triggers_one_token_refresh():
    calls = {}
    client = make_client(router([httpx.Response(401), OK], calls))
    assert (await client._request("GET", "https://sp/a")).status_code == 200
    assert calls["token"] == 2 and calls["api"] == ["Atza|1", "Atza|2"]


async def test_inventory_follows_pagination():
    async with amazon_http() as http:
        client = AmazonClient(http=http, refresh_token="Atzr|demo", client_id="id", client_secret="s",
                              lwa_url="http://mock-amazon", sp_api_url="http://mock-amazon",
                              ads_api_url="http://mock-amazon", seller_id="S", ads_profile_id="P")
        items = [i async for i in client.iter_inventory("ES")]
    assert [i.sku for i in items] == ["BOTTLE-750", "FOAM-ROLLER", "JUMP-ROPE", "RESIST-BANDS", "YOGA-MAT-BLU"]
    assert next(i for i in items if i.sku == "RESIST-BANDS").inbound == 120


async def test_report_is_polled_and_its_download_retried():
    calls, sleeps = {}, []
    responses = [
        httpx.Response(200, json={"reportId": "r1"}),                       # create
        httpx.Response(200, json={"status": "PENDING"}),                    # poll 1
        httpx.Response(200, json={"status": "COMPLETED", "url": "https://s3/r1"}),
        httpx.Response(503),                                                # download fails once
        httpx.Response(200, json=[{"keywordId": "kw-1"}]),
    ]
    client = make_client(router(responses, calls), sleeps)
    rows = await client.keyword_report(date(2026, 10, 1), date(2026, 10, 15))
    assert rows == [{"keywordId": "kw-1"}]
    assert calls["api"][-1] is None          # the pre-signed download carries no Amazon token
