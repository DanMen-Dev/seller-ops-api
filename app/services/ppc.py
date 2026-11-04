"""PPC rules: data-driven rules that adjust Sponsored Products bids.

Rules are data (Pydantic models), not code, so a seller could edit them from a UI.
They are evaluated in order and the first rule whose conditions all hold wins.
A condition on a metric that cannot be computed (ACoS with zero sales) is false.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, Field

Metric = Literal["acos", "clicks", "orders", "spend", "ctr", "cvr"]
OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b,
       "<=": lambda a, b: a <= b, "==": lambda a, b: a == b}


class Condition(BaseModel):
    metric: Metric
    op: Literal[">", ">=", "<", "<=", "=="]
    value: float


class Rule(BaseModel):
    name: str
    conditions: list[Condition] = Field(min_length=1)
    action: Literal["decrease_bid", "increase_bid", "pause"]
    pct: float = Field(0, ge=0, le=100)


@dataclass(frozen=True)
class KeywordPerf:
    keyword_id: str
    keyword_text: str
    campaign_id: str
    bid: Decimal
    impressions: int
    clicks: int
    spend: Decimal
    sales: Decimal
    orders: int

    def metric(self, name: str) -> float | None:
        if name == "acos":   # advertising cost of sales
            return float(self.spend / self.sales) if self.sales else None
        if name == "ctr":    # click-through rate
            return self.clicks / self.impressions if self.impressions else None
        if name == "cvr":    # conversion rate
            return self.orders / self.clicks if self.clicks else None
        return float(getattr(self, name))


@dataclass(frozen=True)
class Recommendation:
    keyword_id: str
    keyword_text: str
    rule: str
    action: str
    old_bid: Decimal
    new_bid: Decimal | None   # None when the action is pause
    acos: float | None
    clicks: int
    orders: int


def default_rules(target_acos: float) -> list[Rule]:
    return [
        Rule(name="no orders after enough clicks", action="pause",
             conditions=[Condition(metric="clicks", op=">=", value=25), Condition(metric="orders", op="==", value=0)]),
        Rule(name="ACoS well above target", action="decrease_bid", pct=15,
             conditions=[Condition(metric="acos", op=">", value=round(target_acos * 1.3, 4)),
                         Condition(metric="clicks", op=">=", value=15)]),
        Rule(name="ACoS well below target with orders", action="increase_bid", pct=10,
             conditions=[Condition(metric="acos", op="<", value=round(target_acos * 0.7, 4)),
                         Condition(metric="orders", op=">=", value=3)]),
    ]


def _holds(perf: KeywordPerf, cond: Condition) -> bool:
    value = perf.metric(cond.metric)
    return value is not None and OPS[cond.op](value, cond.value)


def evaluate(perf: KeywordPerf, rules: list[Rule], min_bid: Decimal = Decimal("0.10"),
             max_bid: Decimal = Decimal("5.00")) -> Recommendation | None:
    for rule in rules:
        if not all(_holds(perf, c) for c in rule.conditions):
            continue
        if rule.action == "pause":
            new_bid = None
        else:
            factor = Decimal(1) + (Decimal(str(rule.pct)) / 100) * (1 if rule.action == "increase_bid" else -1)
            new_bid = min(max_bid, max(min_bid, (perf.bid * factor).quantize(Decimal("0.01"), ROUND_HALF_UP)))
            if new_bid == perf.bid:
                return None  # already at the limit
        acos = perf.metric("acos")
        return Recommendation(perf.keyword_id, perf.keyword_text, rule.name, rule.action, perf.bid, new_bid,
                              round(acos, 4) if acos is not None else None, perf.clicks, perf.orders)
    return None
