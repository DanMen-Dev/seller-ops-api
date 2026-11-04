"""Unit margin per product and marketplace, before advertising.

In EU marketplaces the price shown on Amazon includes VAT, and Amazon's referral fee is charged on
that VAT-inclusive price. So per unit:

    net price = price / (1 + VAT)
    profit    = net price - referral fee - FBA fee - product cost (converted to the marketplace currency)
    margin %  = profit / net price
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

VAT_RATES = {"ES": Decimal("0.21"), "DE": Decimal("0.19"), "FR": Decimal("0.20"),
             "IT": Decimal("0.22"), "UK": Decimal("0.20"), "US": Decimal("0")}  # US prices exclude sales tax
CENT = Decimal("0.01")


@dataclass(frozen=True)
class MarginInput:
    sku: str
    marketplace: str
    price: Decimal
    currency: str
    referral_fee: Decimal
    fba_fee: Decimal
    unit_cost: Decimal | None
    cost_currency: str


@dataclass(frozen=True)
class MarginResult:
    sku: str
    marketplace: str
    currency: str
    price: Decimal
    vat: Decimal
    net_price: Decimal
    referral_fee: Decimal
    fba_fee: Decimal
    unit_cost: Decimal | None   # in the marketplace currency
    profit: Decimal | None
    margin_pct: Decimal | None
    profit_eur: Decimal | None
    warning: str | None = None


def convert(amount: Decimal, from_ccy: str, to_ccy: str, fx_to_eur: dict[str, float]) -> Decimal:
    if from_ccy == to_ccy:
        return amount
    try:
        return amount * Decimal(str(fx_to_eur[from_ccy])) / Decimal(str(fx_to_eur[to_ccy]))
    except KeyError as missing:
        raise ValueError(f"no exchange rate for {missing}") from None


def unit_margin(item: MarginInput, fx_to_eur: dict[str, float]) -> MarginResult:
    vat_rate = VAT_RATES.get(item.marketplace)
    if vat_rate is None:
        raise ValueError(f"unknown marketplace {item.marketplace}")
    net = (item.price / (1 + vat_rate)).quantize(CENT, ROUND_HALF_UP)
    vat = item.price - net

    if item.unit_cost is None:
        return MarginResult(item.sku, item.marketplace, item.currency, item.price, vat, net,
                            item.referral_fee, item.fba_fee, None, None, None, None,
                            warning="unit cost missing: set it with PUT /v1/products/{sku}")

    cost = convert(item.unit_cost, item.cost_currency, item.currency, fx_to_eur).quantize(CENT, ROUND_HALF_UP)
    profit = net - item.referral_fee - item.fba_fee - cost
    margin = (profit / net * 100).quantize(Decimal("0.1"), ROUND_HALF_UP) if net else None
    profit_eur = convert(profit, item.currency, "EUR", fx_to_eur).quantize(CENT, ROUND_HALF_UP)
    warning = "selling at a loss" if profit < 0 else None
    return MarginResult(item.sku, item.marketplace, item.currency, item.price, vat, net,
                        item.referral_fee, item.fba_fee, cost, profit, margin, profit_eur, warning)
