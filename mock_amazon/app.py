"""A small stand-in for Amazon's APIs, so the project runs end to end without a seller account.

It mimics the shape of the real responses (LWA token, FBA Inventory with nextToken, Listings,
Product Fees, Sales orderMetrics, Ads API v3 asynchronous reports and keyword updates) with
deterministic demo data for a sports brand. Set MOCK_FAIL_EVERY=N to answer 429 to every Nth call
and watch the client's retries.

    uvicorn mock_amazon.app:app --port 8001
"""
import os
import random
import uuid
from datetime import date, timedelta
from itertools import count
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock Amazon APIs")

MARKETPLACES = {"A1RKKUPIHCS9HS": ("ES", "EUR"), "A1PA6795UKMFR9": ("DE", "EUR"), "A13V1IB3VIYZZH": ("FR", "EUR"),
                "APJ6JRA9NG5V4": ("IT", "EUR"), "A1F83G8C2ARO7P": ("UK", "GBP")}

# sku: asin, title, ES price, FBA fee, fulfillable, inbound, units/day per marketplace as (base, chance of +1)
PRODUCTS = {
    "YOGA-MAT-BLU": ("B0C1YOGA01", "Non-slip yoga mat, blue", 29.99, 4.20, 40, 0,
                     {"ES": (2, 0.5), "DE": (2, 0.5), "FR": (1, 0.0), "IT": (0, 0.5), "UK": (0, 0.3)}),
    "BOTTLE-750": ("B0C1BOTL02", "Insulated steel bottle 750 ml", 19.99, 3.80, 400, 0,
                   {"ES": (1, 0.5), "DE": (1, 0.0), "FR": (0, 0.5), "IT": (0, 0.5), "UK": (0, 0.2)}),
    "RESIST-BANDS": ("B0C1BAND03", "Resistance bands set", 15.99, 3.10, 0, 120,
                     {"ES": (1, 0.0), "DE": (1, 0.0), "FR": (0, 0.0), "IT": (0, 0.0), "UK": (0, 0.0)}),
    "FOAM-ROLLER": ("B0C1ROLL04", "Massage foam roller", 24.99, 5.40, 40, 10,
                    {"ES": (1, 0.0), "DE": (1, 0.0), "FR": (0, 0.0), "IT": (0, 0.0), "UK": (0, 0.0)}),
    "JUMP-ROPE": ("B0C1ROPE05", "Speed jump rope with bearings", 12.99, 2.90, 200, 0,
                  {"ES": (1, 0.0), "DE": (0, 0.0)}),  # not listed in FR, IT or UK
}
PAGE_SIZE = 2

# keyword_id: campaign, text, match type, starting bid, per-day (impressions, clicks, spend, sales, orders)
KEYWORDS = {
    "kw-1001": ("cmp-11", "yoga mat non slip", "EXACT", 0.80, (900, 6, 3.00, 30.00, 1)),       # cheap sales -> raise
    "kw-1002": ("cmp-11", "yoga mat", "BROAD", 1.10, (1500, 4, 3.20, 6.00, 0.25)),            # ACoS 53 % -> lower
    "kw-1003": ("cmp-12", "cheap water bottle", "PHRASE", 0.60, (700, 2, 1.00, 0.00, 0)),     # clicks, no sales -> pause
    "kw-1004": ("cmp-12", "insulated bottle", "EXACT", 0.70, (600, 3, 2.10, 7.50, 0.5)),      # ACoS 28 % -> keep
    "kw-1005": ("cmp-13", "foam roller", "BROAD", 0.50, (300, 0.5, 0.25, 0.00, 0)),           # too little data -> keep
}
BIDS = {kw: {"bid": v[3], "state": "ENABLED"} for kw, v in KEYWORDS.items()}
TOKENS: set[str] = set()
REPORTS: dict[str, dict] = {}
_calls = count(1)


@app.middleware("http")
async def inject_throttling(request: Request, call_next):
    every = int(os.environ.get("MOCK_FAIL_EVERY", "0"))
    if every and not request.url.path.startswith("/auth") and next(_calls) % every == 0:
        return JSONResponse({"errors": [{"code": "QuotaExceeded", "message": "You exceeded your quota"}]},
                            status_code=429, headers={"Retry-After": "0.05"})
    return await call_next(request)


def _auth(request: Request) -> None:
    bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
    if request.headers.get("x-amz-access-token") not in TOKENS and bearer not in TOKENS:
        raise HTTPException(401, "Access token is missing or expired")


def _marketplace(marketplace_id: str) -> tuple[str, str]:
    if marketplace_id not in MARKETPLACES:
        raise HTTPException(400, f"Invalid marketplaceId {marketplace_id}")
    return MARKETPLACES[marketplace_id]


def _price(sku: str, mkt: str) -> float:
    base = PRODUCTS[sku][2]
    return {"DE": base + 1.0, "UK": round(base * 0.86) - 0.01}.get(mkt, base)


# ---------- Login with Amazon ----------

@app.post("/auth/o2/token")
async def token(request: Request):
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    if form.get("grant_type") != "refresh_token" or not form.get("refresh_token", "").startswith("Atzr|"):
        raise HTTPException(400, "invalid_grant")
    access = f"Atza|{uuid.uuid4().hex}"
    TOKENS.add(access)
    return {"access_token": access, "token_type": "bearer", "expires_in": 3600}


# ---------- SP-API ----------

@app.get("/fba/inventory/v1/summaries")
async def inventory(request: Request, marketplaceIds: str, nextToken: str | None = None):
    _auth(request)
    _marketplace(marketplaceIds)
    skus = sorted(PRODUCTS)
    start = int(nextToken or 0)
    page = skus[start:start + PAGE_SIZE]
    summaries = [{
        "sellerSku": sku, "asin": PRODUCTS[sku][0], "productName": PRODUCTS[sku][1],
        "inventoryDetails": {"fulfillableQuantity": PRODUCTS[sku][4], "inboundWorkingQuantity": 0,
                             "inboundShippedQuantity": PRODUCTS[sku][5]},
    } for sku in page]
    nxt = str(start + PAGE_SIZE) if start + PAGE_SIZE < len(skus) else None
    return {"payload": {"inventorySummaries": summaries}, "pagination": {"nextToken": nxt} if nxt else {}}


@app.get("/listings/2021-08-01/items/{seller_id}/{sku}")
async def listing(request: Request, seller_id: str, sku: str, marketplaceIds: str):
    _auth(request)
    mkt, ccy = _marketplace(marketplaceIds)
    if sku not in PRODUCTS:
        raise HTTPException(404, "SKU not found")
    listed = mkt in PRODUCTS[sku][6]
    return {
        "sku": sku,
        "summaries": [{"marketplaceId": marketplaceIds, "asin": PRODUCTS[sku][0], "itemName": PRODUCTS[sku][1]}],
        "offers": [{"offerType": "B2C", "price": {"currencyCode": ccy, "amount": _price(sku, mkt)}}] if listed else [],
    }


@app.post("/products/fees/v0/listings/{sku}/feesEstimate")
async def fees(request: Request, sku: str):
    _auth(request)
    req = (await request.json())["FeesEstimateRequest"]
    _, ccy = _marketplace(req["MarketplaceId"])
    price = req["PriceToEstimateFees"]["ListingPrice"]["Amount"]
    referral = round(price * 0.15, 2)  # 15 % referral fee (sports category), on the VAT-inclusive price
    fba = PRODUCTS[sku][3]
    return {"payload": {"FeesEstimateResult": {"Status": "Success", "FeesEstimate": {
        "TotalFeesEstimate": {"CurrencyCode": ccy, "Amount": round(referral + fba, 2)},
        "FeeDetailList": [{"FeeType": "ReferralFee", "FinalFee": {"CurrencyCode": ccy, "Amount": referral}},
                          {"FeeType": "FBAFees", "FinalFee": {"CurrencyCode": ccy, "Amount": fba}}],
    }}}}


@app.get("/sales/v1/orderMetrics")
async def order_metrics(request: Request, marketplaceIds: str, interval: str, granularity: str, sku: str):
    _auth(request)
    mkt, ccy = _marketplace(marketplaceIds)
    start, end = (date.fromisoformat(part[:10]) for part in interval.split("--"))
    base, chance = PRODUCTS.get(sku, (None,) * 6 + ({},))[6].get(mkt, (0, 0.0))
    out, day = [], start
    while day < end:
        units = base + (1 if random.Random(f"{sku}|{mkt}|{day}").random() < chance else 0)
        out.append({"interval": f"{day}T00:00:00Z--{day + timedelta(days=1)}T00:00:00Z", "unitCount": units,
                    "orderCount": units, "totalSales": {"currencyCode": ccy, "amount": round(units * _price(sku, mkt), 2)}})
        day += timedelta(days=1)
    return {"payload": out}


# ---------- Advertising API ----------

@app.post("/reporting/reports")
async def create_report(request: Request):
    _auth(request)
    body = await request.json()
    report_id = uuid.uuid4().hex
    REPORTS[report_id] = {"start": date.fromisoformat(body["startDate"]), "end": date.fromisoformat(body["endDate"]),
                          "polls": 0}
    return {"reportId": report_id, "status": "PENDING"}


@app.get("/reporting/reports/{report_id}")
async def report_status(request: Request, report_id: str):
    _auth(request)
    report = REPORTS.get(report_id) or {}
    if not report:
        raise HTTPException(404, "Report not found")
    report["polls"] += 1
    if report["polls"] < 2:  # the first poll is always PENDING, like the real API
        return {"reportId": report_id, "status": "PENDING"}
    return {"reportId": report_id, "status": "COMPLETED", "url": f"{request.base_url}downloads/{report_id}"}


def _whole(value: float, odd_day: int) -> int:
    """Fractional daily values (0.5 clicks) become whole numbers on alternate days."""
    return int(value) + (1 if odd_day and value % 1 else 0)


@app.get("/downloads/{report_id}")
async def download(report_id: str):
    """Pre-signed download (the real one is gzip-compressed JSON on S3)."""
    report = REPORTS[report_id]
    rows, day = [], report["start"]
    while day <= report["end"]:
        for kw, (campaign, text, match, _, (imp, clicks, spend, sales, orders)) in KEYWORDS.items():
            odd = day.toordinal() % 2
            rows.append({"date": day.isoformat(), "campaignId": campaign, "keywordId": kw, "keyword": text,
                         "matchType": match, "keywordBid": BIDS[kw]["bid"], "impressions": imp,
                         "clicks": _whole(clicks, odd), "cost": spend, "sales14d": sales, "purchases14d": _whole(orders, odd)})
        day += timedelta(days=1)
    return rows


@app.put("/sp/keywords")
async def update_keywords(request: Request):
    _auth(request)
    results = []
    for change in (await request.json())["keywords"]:
        kw = change["keywordId"]
        if kw not in BIDS:
            results.append({"keywordId": kw, "code": "NOT_FOUND"})
            continue
        if "bid" in change:
            BIDS[kw]["bid"] = change["bid"]
        if "state" in change:
            BIDS[kw]["state"] = change["state"]
        results.append({"keywordId": kw, "code": "SUCCESS"})
    return {"keywords": {"success": [r for r in results if r["code"] == "SUCCESS"],
                         "error": [r for r in results if r["code"] != "SUCCESS"]}}
