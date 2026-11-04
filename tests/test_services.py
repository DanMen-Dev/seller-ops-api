"""Business rules, tested as pure functions (no database, no HTTP)."""
from decimal import Decimal as D

import pytest

from app.services.margins import MarginInput, unit_margin
from app.services.ppc import Condition, KeywordPerf, Rule, default_rules, evaluate
from app.services.stock import StockInput, evaluate_stock

FX = {"EUR": 1.0, "GBP": 1.17, "USD": 0.92}


# ---------- stock ----------

def stock(fulfillable, inbound, units_30d, lead=21):
    return evaluate_stock(StockInput("SKU", "Title", fulfillable, inbound, units_30d, 30, lead), safety_days=7)


def test_reorder_now_when_stock_runs_out_before_a_shipment_can_arrive():
    s = stock(40, 0, 180)          # 6 a day -> 6.7 days, lead time 21
    assert (s.status, s.days_of_cover, s.avg_daily_units) == ("reorder_now", 6.7, 6.0)
    assert s.reorder_qty == 6 * (21 + 7 + 30) - 40


def test_reorder_soon_inside_the_safety_margin():
    s = stock(40, 10, 60)          # 2 a day -> 25 days: above 21, below 28
    assert (s.status, s.days_of_cover, s.reorder_qty) == ("reorder_soon", 25.0, 66)


def test_ok_when_cover_is_beyond_lead_time_plus_safety():
    s = stock(400, 0, 90)
    assert (s.status, s.reorder_qty) == ("ok", 0)


def test_inbound_stock_counts_toward_cover():
    assert stock(20, 200, 60).status == "ok"


def test_out_of_stock_even_with_inbound_units():
    assert stock(0, 120, 60).status == "out_of_stock"


def test_no_sales_means_no_cover_and_no_division_by_zero():
    s = stock(50, 0, 0)
    assert (s.status, s.days_of_cover, s.reorder_qty) == ("no_sales", None, 0)


# ---------- margins ----------

def margin(price, mkt="ES", ccy="EUR", referral="4.50", fba="4.20", cost="8.00", cost_ccy="EUR"):
    return unit_margin(MarginInput("SKU", mkt, D(price), ccy, D(referral), D(fba),
                                   D(cost) if cost else None, cost_ccy), FX)


def test_margin_removes_vat_before_fees_and_cost():
    m = margin("29.99")            # 29.99 / 1.21 = 24.79 net
    assert (m.net_price, m.vat, m.profit, m.margin_pct, m.profit_eur) == (
        D("24.79"), D("5.20"), D("8.09"), D("32.6"), D("8.09"))


def test_margin_converts_cost_to_the_marketplace_currency():
    m = margin("25.99", mkt="UK", ccy="GBP", referral="3.90")
    assert m.unit_cost == D("6.84")                     # 8 EUR in GBP
    assert (m.net_price, m.profit, m.profit_eur) == (D("21.66"), D("6.72"), D("7.86"))


def test_missing_cost_is_reported_not_guessed():
    m = margin("29.99", cost=None)
    assert m.profit is None and "unit cost missing" in m.warning


def test_loss_is_flagged():
    assert margin("12.99", cost="9.00").warning == "selling at a loss"


def test_unknown_currency_is_an_error():
    with pytest.raises(ValueError, match="no exchange rate"):
        margin("29.99", mkt="ES", cost_ccy="CNY")


# ---------- PPC rules ----------

def perf(clicks, orders, spend, sales, bid="1.00", impressions=1000):
    return KeywordPerf("kw", "yoga mat", "cmp", D(bid), impressions, clicks, D(spend), D(sales), orders)


RULES = default_rules(target_acos=0.30)


def test_high_acos_lowers_the_bid_15_percent():
    rec = evaluate(perf(clicks=40, orders=4, spend="45", sales="85"), RULES)   # ACoS 53 %
    assert (rec.action, rec.new_bid) == ("decrease_bid", D("0.85"))


def test_low_acos_with_orders_raises_the_bid():
    rec = evaluate(perf(clicks=80, orders=14, spend="42", sales="420"), RULES)  # ACoS 10 %
    assert (rec.action, rec.new_bid) == ("increase_bid", D("1.10"))


def test_clicks_without_orders_pause_the_keyword():
    rec = evaluate(perf(clicks=28, orders=0, spend="14", sales="0"), RULES)
    assert (rec.action, rec.new_bid) == ("pause", None)


def test_on_target_or_too_little_data_changes_nothing():
    assert evaluate(perf(clicks=42, orders=7, spend="29.4", sales="105"), RULES) is None  # ACoS 28 %
    assert evaluate(perf(clicks=7, orders=0, spend="3.5", sales="0"), RULES) is None       # 7 clicks only


def test_bids_stay_within_limits():
    rec = evaluate(perf(clicks=80, orders=14, spend="42", sales="420", bid="4.80"), RULES)
    assert rec.new_bid == D("5.00")
    assert evaluate(perf(clicks=80, orders=14, spend="42", sales="420", bid="5.00"), RULES) is None


def test_custom_rules_are_plain_data():
    rule = Rule(name="low CTR", action="decrease_bid", pct=20,
                conditions=[Condition(metric="ctr", op="<", value=0.002),
                            Condition(metric="clicks", op=">=", value=1)])
    rec = evaluate(perf(clicks=1, orders=0, spend="1", sales="0", impressions=5000), [rule])
    assert (rec.rule, rec.new_bid) == ("low CTR", D("0.80"))
