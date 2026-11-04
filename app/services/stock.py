"""Stock alerts: warn before a product runs out, while there is still time to reorder.

days_of_cover = (fulfillable + inbound) / average units sold per day
- reorder_now:  the stock runs out before a new shipment could arrive (cover < lead time)
- reorder_soon: it would arrive, but inside the safety margin (cover < lead time + safety days)
Pure functions: no database, no HTTP, so they are tested directly.
"""
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class StockInput:
    sku: str
    title: str
    fulfillable: int
    inbound: int
    units_sold: int        # units sold over the window, all marketplaces
    window_days: int
    lead_time_days: int


@dataclass(frozen=True)
class StockStatus:
    sku: str
    title: str
    fulfillable: int
    inbound: int
    avg_daily_units: float
    days_of_cover: float | None   # None when nothing sold in the window
    status: str                   # out_of_stock | reorder_now | reorder_soon | ok | no_sales
    reorder_qty: int


def evaluate_stock(item: StockInput, safety_days: int = 7, target_days: int = 30) -> StockStatus:
    avg = item.units_sold / item.window_days if item.window_days > 0 else 0.0
    available = item.fulfillable + item.inbound

    if avg == 0:
        status = "out_of_stock" if item.fulfillable == 0 else "no_sales"
        return StockStatus(item.sku, item.title, item.fulfillable, item.inbound, 0.0, None, status, 0)

    cover = available / avg
    if item.fulfillable == 0:
        status = "out_of_stock"
    elif cover < item.lead_time_days:
        status = "reorder_now"
    elif cover < item.lead_time_days + safety_days:
        status = "reorder_soon"
    else:
        status = "ok"

    reorder_qty = 0
    if status != "ok":
        # enough to cover the lead time, the safety margin and target_days of sales after the shipment arrives
        reorder_qty = max(0, math.ceil(avg * (item.lead_time_days + safety_days + target_days)) - available)

    return StockStatus(item.sku, item.title, item.fulfillable, item.inbound,
                       round(avg, 2), round(cover, 1), status, reorder_qty)


SEVERITY = {"out_of_stock": 0, "reorder_now": 1, "reorder_soon": 2, "no_sales": 3, "ok": 4}
