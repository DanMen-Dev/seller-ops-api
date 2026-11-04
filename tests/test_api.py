"""End to end: HTTP API + PostgreSQL + the mock Amazon APIs."""
from decimal import Decimal

from sqlalchemy import func, select

import mock_amazon.app as mock
from app.models import BidChange, DailySales, Offer


async def sync(api, headers):
    resp = await api.post("/v1/sync", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_requests_without_a_valid_key_are_rejected(api):
    assert (await api.get("/v1/stock/alerts")).status_code == 401
    assert (await api.get("/v1/stock/alerts", headers={"X-API-Key": "so_wrong"})).status_code == 401


async def test_sync_loads_products_offers_sales_and_ads(api, make_tenant):
    run = await sync(api, await make_tenant())
    assert run["status"] == "ok", run["error"]
    # 5 SKUs; JUMP-ROPE is listed only in ES and DE, so 4 x 5 + 2 = 22 offers
    assert run["stats"]["products"] == 5
    assert run["stats"]["offers"] == 22
    assert run["stats"]["sales_rows"] == 22 * 30
    assert run["stats"]["keyword_rows"] == 5 * 30


async def test_sync_is_idempotent(api, make_tenant, session_factory):
    headers = await make_tenant()
    await sync(api, headers)
    await sync(api, headers)
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(DailySales)) == 22 * 30
        assert await s.scalar(select(func.count()).select_from(Offer)) == 22


async def test_sync_survives_amazon_throttling(api, make_tenant, monkeypatch):
    monkeypatch.setenv("MOCK_FAIL_EVERY", "3")   # every third call answers 429
    run = await sync(api, await make_tenant())
    assert run["status"] == "ok", run["error"]
    assert run["stats"]["offers"] == 22


async def test_a_failed_sync_is_recorded_and_writes_nothing(api, make_tenant, session_factory):
    headers = await make_tenant(refresh_token="revoked-token")   # LWA rejects it
    run = await sync(api, headers)
    assert run["status"] == "failed" and "invalid_grant" in run["error"]
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(Offer)) == 0
    runs = (await api.get("/v1/sync/runs", headers=headers)).json()
    assert [r["status"] for r in runs] == ["failed"]


async def test_stock_alerts_most_urgent_first(api, make_tenant):
    headers = await make_tenant()
    await sync(api, headers)
    alerts = (await api.get("/v1/stock/alerts", headers=headers)).json()
    assert [(a["sku"], a["status"]) for a in alerts] == [
        ("RESIST-BANDS", "out_of_stock"), ("YOGA-MAT-BLU", "reorder_now"), ("FOAM-ROLLER", "reorder_soon")]
    roller = alerts[2]
    assert (roller["avg_daily_units"], roller["days_of_cover"], roller["reorder_qty"]) == (2.0, 25.0, 66)

    everything = (await api.get("/v1/stock/alerts?only_alerts=false", headers=headers)).json()
    assert len(everything) == 5


async def test_lead_time_entered_by_the_seller_changes_the_alert(api, make_tenant):
    headers = await make_tenant()
    await sync(api, headers)
    await api.put("/v1/products/FOAM-ROLLER", headers=headers, json={"lead_time_days": 10})
    alerts = (await api.get("/v1/stock/alerts", headers=headers)).json()
    assert "FOAM-ROLLER" not in [a["sku"] for a in alerts]   # 25 days of cover vs 10 + 7 needed


async def test_margins_per_marketplace(api, make_tenant):
    headers = await make_tenant()
    await sync(api, headers)
    resp = await api.put("/v1/products/YOGA-MAT-BLU", headers=headers, json={"unit_cost": "8.00"})
    assert resp.status_code == 200

    rows = {m["marketplace"]: m for m in (await api.get("/v1/margins", headers=headers)).json()
            if m["sku"] == "YOGA-MAT-BLU"}
    assert set(rows) == {"ES", "DE", "FR", "IT", "UK"}
    es, uk = rows["ES"], rows["UK"]
    assert (Decimal(es["net_price"]), Decimal(es["profit"]), Decimal(es["margin_pct"])) == (
        Decimal("24.79"), Decimal("8.09"), Decimal("32.6"))
    assert uk["currency"] == "GBP" and Decimal(uk["profit_eur"]) == Decimal("7.86")

    only_de = (await api.get("/v1/margins?marketplace=DE", headers=headers)).json()
    assert {m["marketplace"] for m in only_de} == {"DE"}
    no_cost = next(m for m in only_de if m["sku"] == "BOTTLE-750")
    assert no_cost["profit"] is None and "unit cost missing" in no_cost["warning"]


async def test_ppc_recommendations_then_apply(api, make_tenant, session_factory):
    headers = await make_tenant()
    await sync(api, headers)

    recs = (await api.get("/v1/ppc/recommendations?target_acos=0.30", headers=headers)).json()
    assert {(r["keyword_id"], r["action"], r["new_bid"]) for r in recs} == {
        ("kw-1001", "increase_bid", "0.88"),   # ACoS 10 %
        ("kw-1002", "decrease_bid", "0.94"),   # ACoS 53 %
        ("kw-1003", "pause", None),            # 28 clicks, 0 orders
    }

    dry = (await api.post("/v1/ppc/apply", headers=headers)).json()   # dry run by default
    assert dry["dry_run"] is True and len(dry["applied"]) == 3
    assert mock.BIDS["kw-1001"]["bid"] == 0.80

    applied = (await api.post("/v1/ppc/apply?dry_run=false", headers=headers)).json()
    assert len(applied["applied"]) == 3
    assert mock.BIDS["kw-1001"]["bid"] == 0.88
    assert mock.BIDS["kw-1002"]["bid"] == 0.94
    assert mock.BIDS["kw-1003"]["state"] == "PAUSED"
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(BidChange)) == 3

    # changed keywords rest for the cooldown period: their stats still reflect the old bids
    assert (await api.get("/v1/ppc/recommendations", headers=headers)).json() == []


async def test_tenants_never_see_each_others_data(api, make_tenant):
    brand_a = await make_tenant(name="Brand A")
    brand_b = await make_tenant(name="Brand B")
    await sync(api, brand_a)

    assert len((await api.get("/v1/products", headers=brand_a)).json()) == 5
    assert (await api.get("/v1/products", headers=brand_b)).json() == []
    assert (await api.get("/v1/margins", headers=brand_b)).json() == []
    assert (await api.get("/v1/stock/alerts", headers=brand_b)).json() == []
    assert (await api.get("/v1/ppc/recommendations", headers=brand_b)).json() == []
    resp = await api.put("/v1/products/YOGA-MAT-BLU", headers=brand_b, json={"unit_cost": "1.00"})
    assert resp.status_code == 404


async def test_input_is_validated(api, make_tenant):
    headers = await make_tenant()
    await sync(api, headers)
    assert (await api.put("/v1/products/YOGA-MAT-BLU", headers=headers, json={"unit_cost": "-3"})).status_code == 422
    assert (await api.get("/v1/margins?marketplace=spain", headers=headers)).status_code == 422
    assert (await api.get("/v1/ppc/recommendations?target_acos=0", headers=headers)).status_code == 422
