"""Request and response models (Pydantic validates input and documents the API in /docs)."""
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class StockAlert(BaseModel):
    sku: str
    title: str
    fulfillable: int
    inbound: int
    avg_daily_units: float
    days_of_cover: float | None
    lead_time_days: int
    status: str
    reorder_qty: int


class Margin(BaseModel):
    sku: str
    marketplace: str
    currency: str
    price: Decimal
    vat: Decimal
    net_price: Decimal
    referral_fee: Decimal
    fba_fee: Decimal
    unit_cost: Decimal | None
    profit: Decimal | None
    margin_pct: Decimal | None
    profit_eur: Decimal | None
    warning: str | None


class BidRecommendation(BaseModel):
    keyword_id: str
    keyword_text: str
    rule: str
    action: str
    old_bid: Decimal
    new_bid: Decimal | None
    acos: float | None
    clicks: int
    orders: int


class ApplyResult(BaseModel):
    dry_run: bool
    applied: list[BidRecommendation]


class ProductUpdate(BaseModel):
    unit_cost: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=2)
    cost_currency: str | None = Field(None, pattern="^[A-Z]{3}$")
    lead_time_days: int | None = Field(None, ge=1, le=365)


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    sku: str
    asin: str | None
    title: str
    unit_cost: Decimal | None
    cost_currency: str
    lead_time_days: int


class SyncRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    stats: dict
    error: str | None
