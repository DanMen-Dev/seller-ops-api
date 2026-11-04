"""HTTP endpoints. Every query is filtered by the tenant resolved from the X-API-Key header."""
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.amazon.client import AmazonClient
from app.config import get_settings
from app.db import get_session
from app.deps import get_amazon_client
from app.models import BidChange, DailySales, Inventory, KeywordStat, Offer, Product, SyncRun, Tenant
from app.schemas import (
    ApplyResult, BidRecommendation, Margin, ProductOut, ProductUpdate, StockAlert, SyncRunOut,
)
from app.security import get_tenant
from app.services.margins import MarginInput, unit_margin
from app.services.ppc import KeywordPerf, Recommendation, default_rules, evaluate
from app.services.stock import SEVERITY, StockInput, evaluate_stock
from app.services.sync import sync_tenant

router = APIRouter(prefix="/v1")
TenantDep = Annotated[Tenant, Depends(get_tenant)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ClientDep = Annotated[AmazonClient, Depends(get_amazon_client)]


# ---------- stock ----------

@router.get("/stock/alerts", response_model=list[StockAlert])
async def stock_alerts(tenant: TenantDep, session: SessionDep, only_alerts: bool = True):
    """Days of stock left per SKU and what to reorder. Most urgent first."""
    s = get_settings()
    since = date.today() - timedelta(days=s.stock_window_days)
    sold = (select(DailySales.sku, func.sum(DailySales.units).label("units"))
            .where(DailySales.tenant_id == tenant.id, DailySales.day >= since)
            .group_by(DailySales.sku).subquery())
    rows = (await session.execute(
        select(Product.sku, Product.title, Product.lead_time_days, Inventory.fulfillable, Inventory.inbound,
               func.coalesce(sold.c.units, 0))
        .join(Inventory, and_(Inventory.tenant_id == Product.tenant_id, Inventory.sku == Product.sku))
        .outerjoin(sold, sold.c.sku == Product.sku)
        .where(Product.tenant_id == tenant.id)
    )).all()

    alerts = []
    for sku, title, lead, fulfillable, inbound, units in rows:
        st = evaluate_stock(StockInput(sku, title, fulfillable, inbound, int(units), s.stock_window_days, lead),
                            safety_days=s.safety_days)
        if only_alerts and st.status in ("ok", "no_sales"):
            continue
        alerts.append(StockAlert(**st.__dict__, lead_time_days=lead))
    return sorted(alerts, key=lambda a: (SEVERITY[a.status], a.days_of_cover or 0))


# ---------- margins ----------

@router.get("/margins", response_model=list[Margin])
async def margins(tenant: TenantDep, session: SessionDep,
                  marketplace: Annotated[str | None, Query(pattern="^[A-Z]{2}$")] = None):
    """Unit price, VAT, Amazon fees, cost and margin per SKU and marketplace (before advertising)."""
    q = (select(Offer, Product)
         .join(Product, and_(Product.tenant_id == Offer.tenant_id, Product.sku == Offer.sku))
         .where(Offer.tenant_id == tenant.id)
         .order_by(Offer.sku, Offer.marketplace))
    if marketplace:
        q = q.where(Offer.marketplace == marketplace)
    fx = get_settings().fx_rates_to_eur
    out = []
    for offer, product in (await session.execute(q)).all():
        result = unit_margin(MarginInput(offer.sku, offer.marketplace, offer.price, offer.currency,
                                         offer.referral_fee, offer.fba_fee, product.unit_cost,
                                         product.cost_currency), fx)
        out.append(Margin(**result.__dict__))
    return out


# ---------- PPC ----------

async def _recommendations(session: AsyncSession, tenant: Tenant, target_acos: float, days: int,
                           cooldown_days: int) -> list[Recommendation]:
    since = date.today() - timedelta(days=days)
    totals = (select(KeywordStat.keyword_id,
                     func.sum(KeywordStat.impressions).label("impressions"), func.sum(KeywordStat.clicks).label("clicks"),
                     func.sum(KeywordStat.spend).label("spend"), func.sum(KeywordStat.sales).label("sales"),
                     func.sum(KeywordStat.orders).label("orders"))
              .where(KeywordStat.tenant_id == tenant.id, KeywordStat.day >= since)
              .group_by(KeywordStat.keyword_id).subquery())
    # current bid = the bid on the most recent day (PostgreSQL DISTINCT ON)
    latest = (select(KeywordStat.keyword_id, KeywordStat.keyword_text, KeywordStat.campaign_id, KeywordStat.bid)
              .where(KeywordStat.tenant_id == tenant.id)
              .distinct(KeywordStat.keyword_id)
              .order_by(KeywordStat.keyword_id, KeywordStat.day.desc()).subquery())
    # keywords changed recently are skipped: their stats still reflect the old bid
    recently_changed = (select(BidChange.keyword_id)
                        .where(BidChange.tenant_id == tenant.id,
                               BidChange.applied_at >= func.now() - timedelta(days=cooldown_days)))
    rows = (await session.execute(
        select(latest, totals.c.impressions, totals.c.clicks, totals.c.spend, totals.c.sales, totals.c.orders)
        .join(totals, totals.c.keyword_id == latest.c.keyword_id)
        .where(latest.c.keyword_id.not_in(recently_changed))
        .order_by(latest.c.keyword_id)
    )).all()

    rules = default_rules(target_acos)
    recs = []
    for r in rows:
        perf = KeywordPerf(r.keyword_id, r.keyword_text, r.campaign_id, r.bid, int(r.impressions), int(r.clicks),
                           r.spend, r.sales, int(r.orders))
        if rec := evaluate(perf, rules):
            recs.append(rec)
    return recs


@router.get("/ppc/recommendations", response_model=list[BidRecommendation])
async def ppc_recommendations(tenant: TenantDep, session: SessionDep,
                              target_acos: Annotated[float, Query(gt=0, lt=2)] = 0.30,
                              days: Annotated[int, Query(ge=3, le=60)] = 14,
                              cooldown_days: Annotated[int, Query(ge=0, le=30)] = 7):
    """What the PPC rules would change, without touching Amazon."""
    return [BidRecommendation(**r.__dict__) for r in await _recommendations(session, tenant, target_acos, days, cooldown_days)]


@router.post("/ppc/apply", response_model=ApplyResult)
async def ppc_apply(tenant: TenantDep, session: SessionDep, client: ClientDep,
                    target_acos: Annotated[float, Query(gt=0, lt=2)] = 0.30,
                    days: Annotated[int, Query(ge=3, le=60)] = 14,
                    cooldown_days: Annotated[int, Query(ge=0, le=30)] = 7,
                    dry_run: bool = True):
    """Applies the recommendations on Amazon and records each change. dry_run is on by default."""
    recs = await _recommendations(session, tenant, target_acos, days, cooldown_days)
    if recs and not dry_run:
        await client.update_keywords([
            {"keywordId": r.keyword_id, "state": "PAUSED"} if r.action == "pause"
            else {"keywordId": r.keyword_id, "bid": float(r.new_bid)} for r in recs
        ])
        session.add_all(BidChange(tenant_id=tenant.id, keyword_id=r.keyword_id, keyword_text=r.keyword_text,
                                  rule=r.rule, action=r.action, old_bid=r.old_bid, new_bid=r.new_bid) for r in recs)
        await session.commit()
    return ApplyResult(dry_run=dry_run, applied=[BidRecommendation(**r.__dict__) for r in recs])


# ---------- products ----------

@router.get("/products", response_model=list[ProductOut])
async def list_products(tenant: TenantDep, session: SessionDep):
    return (await session.scalars(select(Product).where(Product.tenant_id == tenant.id).order_by(Product.sku))).all()


@router.put("/products/{sku}", response_model=ProductOut)
async def update_product(sku: str, body: ProductUpdate, tenant: TenantDep, session: SessionDep):
    """Cost and lead time come from the seller, not from Amazon."""
    product = await session.scalar(select(Product).where(Product.tenant_id == tenant.id, Product.sku == sku))
    if product is None:
        raise HTTPException(404, "Product not found")  # same answer whether the SKU is missing or another tenant's
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(product, field, value)
    await session.commit()
    return product


# ---------- sync ----------

@router.post("/sync", response_model=SyncRunOut)
async def run_sync(tenant: TenantDep, session: SessionDep, client: ClientDep,
                   days: Annotated[int, Query(ge=1, le=90)] = 30):
    """Runs the Amazon sync now. In production this runs nightly from a scheduler (see app/cli.py)."""
    return await sync_tenant(session, tenant, client, days=days)


@router.get("/sync/runs", response_model=list[SyncRunOut])
async def sync_runs(tenant: TenantDep, session: SessionDep):
    return (await session.scalars(select(SyncRun).where(SyncRun.tenant_id == tenant.id)
                                  .order_by(SyncRun.id.desc()).limit(20))).all()
