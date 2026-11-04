"""Database tables. Every business table carries tenant_id: one seller never sees another's data."""
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MONEY = Numeric(12, 2)


class Base(DeclarativeBase):
    pass


def tenant_fk(index: bool = False) -> Mapped[int]:
    # Most tables already have a unique constraint that starts with tenant_id, and PostgreSQL uses it
    # for tenant_id lookups too, so a separate index would be redundant there.
    return mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=index)


class Tenant(Base):
    """A seller account (one brand or one group of brands) connected to Amazon."""
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 of the API key, never the key
    seller_id: Mapped[str] = mapped_column(String(40))
    ads_profile_id: Mapped[str] = mapped_column(String(40))
    refresh_token_enc: Mapped[str] = mapped_column(Text)  # Amazon LWA refresh token, Fernet-encrypted
    marketplaces: Mapped[list[str]] = mapped_column(ARRAY(String(2)))  # e.g. ["ES", "DE", "FR"]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Product(Base):
    """What the seller sells. Cost and lead time are entered by the seller: Amazon does not know them."""
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("tenant_id", "sku"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = tenant_fk()
    sku: Mapped[str] = mapped_column(String(64))
    asin: Mapped[str | None] = mapped_column(String(10))
    title: Mapped[str] = mapped_column(String(300), default="")
    unit_cost: Mapped[Decimal | None] = mapped_column(MONEY)
    cost_currency: Mapped[str] = mapped_column(String(3), default="EUR")
    lead_time_days: Mapped[int] = mapped_column(Integer, default=21)


class Offer(Base):
    """Current price and Amazon fees of one SKU in one marketplace."""
    __tablename__ = "offers"
    __table_args__ = (UniqueConstraint("tenant_id", "sku", "marketplace"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = tenant_fk()
    sku: Mapped[str] = mapped_column(String(64))
    marketplace: Mapped[str] = mapped_column(String(2))
    price: Mapped[Decimal] = mapped_column(MONEY)  # VAT included in EU marketplaces, as shown on Amazon
    currency: Mapped[str] = mapped_column(String(3))
    referral_fee: Mapped[Decimal] = mapped_column(MONEY)
    fba_fee: Mapped[Decimal] = mapped_column(MONEY)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Inventory(Base):
    """FBA stock of one SKU (FBA stock is pooled across EU marketplaces)."""
    __tablename__ = "inventory"
    __table_args__ = (UniqueConstraint("tenant_id", "sku"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = tenant_fk()
    sku: Mapped[str] = mapped_column(String(64))
    fulfillable: Mapped[int] = mapped_column(Integer, default=0)
    inbound: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DailySales(Base):
    __tablename__ = "daily_sales"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sku", "marketplace", "day"),
        Index("ix_daily_sales_tenant_day", "tenant_id", "day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = tenant_fk()
    sku: Mapped[str] = mapped_column(String(64))
    marketplace: Mapped[str] = mapped_column(String(2))
    day: Mapped[date] = mapped_column(Date)
    units: Mapped[int] = mapped_column(Integer)
    revenue: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3))


class KeywordStat(Base):
    """One day of Sponsored Products performance for one keyword."""
    __tablename__ = "keyword_stats"
    __table_args__ = (
        UniqueConstraint("tenant_id", "keyword_id", "day"),
        Index("ix_keyword_stats_tenant_day", "tenant_id", "day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = tenant_fk()
    day: Mapped[date] = mapped_column(Date)
    campaign_id: Mapped[str] = mapped_column(String(40))
    keyword_id: Mapped[str] = mapped_column(String(40))
    keyword_text: Mapped[str] = mapped_column(String(200))
    match_type: Mapped[str] = mapped_column(String(10))
    bid: Mapped[Decimal] = mapped_column(MONEY)
    impressions: Mapped[int] = mapped_column(Integer)
    clicks: Mapped[int] = mapped_column(Integer)
    spend: Mapped[Decimal] = mapped_column(MONEY)
    sales: Mapped[Decimal] = mapped_column(MONEY)
    orders: Mapped[int] = mapped_column(Integer)


class BidChange(Base):
    """Audit trail: every bid change the PPC rules applied on Amazon."""
    __tablename__ = "bid_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = tenant_fk(index=True)
    keyword_id: Mapped[str] = mapped_column(String(40))
    keyword_text: Mapped[str] = mapped_column(String(200))
    rule: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(20))
    old_bid: Mapped[Decimal] = mapped_column(MONEY)
    new_bid: Mapped[Decimal | None] = mapped_column(MONEY)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SyncRun(Base):
    """One execution of the daily Amazon sync, for observability."""
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = tenant_fk(index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(10), default="running")  # running | ok | failed
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
