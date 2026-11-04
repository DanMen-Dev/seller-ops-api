"""Daily sync: pulls one seller's data from Amazon and upserts it into PostgreSQL.

Network calls run concurrently (asyncio.gather, capped by the client's semaphore); database
writes run afterwards in one session, because an AsyncSession must not be shared between tasks.
Upserts (INSERT ... ON CONFLICT DO UPDATE) make the sync idempotent: running it twice is safe.
"""
import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.amazon.client import AmazonClient
from app.models import DailySales, Inventory, KeywordStat, Offer, Product, SyncRun, Tenant


async def upsert(session: AsyncSession, model, rows: list[dict], keys: list[str], update: list[str]) -> int:
    for i in range(0, len(rows), 1000):
        stmt = insert(model).values(rows[i:i + 1000])
        set_ = {col: stmt.excluded[col] for col in update}
        if "updated_at" in model.__table__.c:
            set_["updated_at"] = func.now()
        await session.execute(stmt.on_conflict_do_update(index_elements=keys, set_=set_))
    return len(rows)


async def _offer_and_sales(client: AmazonClient, sku: str, marketplace: str, start: date, end: date):
    listing = await client.get_listing(sku, marketplace)
    if listing is None:
        return None, []
    referral, fba = await client.get_fees(sku, marketplace, listing.price, listing.currency)
    sales = await client.get_daily_sales(sku, marketplace, start, end)
    return (listing, referral, fba), sales


async def sync_tenant(session: AsyncSession, tenant: Tenant, client: AmazonClient,
                      *, days: int = 30, today: date | None = None) -> SyncRun:
    today = today or date.today()
    start, end = today - timedelta(days=days), today
    run = SyncRun(tenant_id=tenant.id, status="running", stats={})
    session.add(run)
    await session.commit()
    run_id, tenant_id = run.id, tenant.id

    try:
        # 1. FBA inventory (pooled across EU marketplaces, so one call per pool)
        items = [i async for i in client.iter_inventory(tenant.marketplaces[0])]
        await upsert(session, Product, [
            {"tenant_id": tenant_id, "sku": i.sku, "asin": i.asin, "title": i.title,
             "cost_currency": "EUR", "lead_time_days": 21} for i in items
        ], ["tenant_id", "sku"], ["asin", "title"])  # never overwrite cost or lead time entered by the seller
        await upsert(session, Inventory, [
            {"tenant_id": tenant_id, "sku": i.sku, "fulfillable": i.fulfillable, "inbound": i.inbound} for i in items
        ], ["tenant_id", "sku"], ["fulfillable", "inbound"])

        # 2. price, fees and daily sales for every SKU x marketplace, concurrently
        pairs = [(i.sku, m) for i in items for m in tenant.marketplaces]
        results = await asyncio.gather(*(_offer_and_sales(client, sku, m, start, end) for sku, m in pairs))
        offers, sales = [], []
        for (sku, m), (offer, daily) in zip(pairs, results, strict=True):
            if offer:
                listing, referral, fba = offer
                offers.append({"tenant_id": tenant_id, "sku": sku, "marketplace": m, "price": listing.price,
                               "currency": listing.currency, "referral_fee": referral, "fba_fee": fba})
            sales += [{"tenant_id": tenant_id, "sku": sku, "marketplace": m, "day": d, "units": u,
                       "revenue": r, "currency": c} for d, u, r, c in daily]
        await upsert(session, Offer, offers, ["tenant_id", "sku", "marketplace"],
                     ["price", "currency", "referral_fee", "fba_fee"])
        await upsert(session, DailySales, sales, ["tenant_id", "sku", "marketplace", "day"],
                     ["units", "revenue", "currency"])

        # 3. advertising performance (asynchronous report)
        report = await client.keyword_report(start, end)
        keywords = [{
            "tenant_id": tenant_id, "day": date.fromisoformat(r["date"]), "campaign_id": str(r["campaignId"]),
            "keyword_id": str(r["keywordId"]), "keyword_text": r["keyword"], "match_type": r["matchType"],
            "bid": Decimal(str(r["keywordBid"])), "impressions": r["impressions"], "clicks": r["clicks"],
            "spend": Decimal(str(r["cost"])), "sales": Decimal(str(r["sales14d"])), "orders": r["purchases14d"],
        } for r in report]
        await upsert(session, KeywordStat, keywords, ["tenant_id", "keyword_id", "day"],
                     ["campaign_id", "keyword_text", "match_type", "bid", "impressions", "clicks", "spend",
                      "sales", "orders"])

        run = await session.get(SyncRun, run_id)
        run.status = "ok"
        run.stats = {"products": len(items), "offers": len(offers), "sales_rows": len(sales),
                     "keyword_rows": len(keywords), "window": [start.isoformat(), end.isoformat()]}
    except Exception as exc:  # noqa: BLE001 - any failure is recorded on the run
        await session.rollback()  # nothing half-written: the data stays as of the last good sync
        run = await session.get(SyncRun, run_id)
        run.status = "failed"
        run.error = repr(exc)[:2000]

    run.finished_at = datetime.now(UTC)
    await session.commit()
    return run
