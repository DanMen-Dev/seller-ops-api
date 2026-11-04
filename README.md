# Seller Ops API

Backend for Amazon sellers who run several brands across European marketplaces: it syncs data from Amazon every morning and answers three daily questions.

- **What do I need to reorder?** Days of stock left per SKU, with an alert before it runs out and a suggested reorder quantity.
- **How much do I really make per unit, per country?** Price without VAT, Amazon referral and FBA fees, product cost converted between currencies.
- **Which ad bids should change?** Data-driven PPC rules that raise, lower or pause Sponsored Products keywords, applied on Amazon with an audit trail.

FastAPI, async SQLAlchemy 2.0, PostgreSQL 16, Alembic, httpx, Pydantic v2, pytest, Docker, GitHub Actions.

It runs end to end without an Amazon seller account: `mock_amazon/` is a stand-in for Amazon's APIs that mimics the shape of the real responses (Login with Amazon, FBA Inventory, Listings, Product Fees, Sales, Advertising API v3 reports) and can answer `429 Too Many Requests` on purpose to exercise the retries.

## Architecture

```mermaid
flowchart LR
    S[Scheduler<br/>every morning] --> C[sync-all CLI]
    U[Seller / dashboard] -->|X-API-Key| A[FastAPI]
    A --> P[(PostgreSQL)]
    C --> AM[Amazon client<br/>LWA token · retries · rate limits]
    A -->|sync now / apply bids| AM
    AM --> SP[SP-API<br/>inventory · listings · fees · sales]
    AM --> ADS[Advertising API<br/>async reports · keyword bids]
    C --> P
```

## Example

```bash
$ curl -s localhost:8000/v1/stock/alerts -H "X-API-Key: $KEY"
```
```json
[
  {"sku": "RESIST-BANDS", "fulfillable": 0,  "inbound": 120, "avg_daily_units": 2.0, "days_of_cover": 60.0, "status": "out_of_stock", "reorder_qty": 0},
  {"sku": "YOGA-MAT-BLU", "fulfillable": 40, "inbound": 0,   "avg_daily_units": 6.8, "days_of_cover": 5.9,  "status": "reorder_now",  "reorder_qty": 355},
  {"sku": "FOAM-ROLLER",  "fulfillable": 40, "inbound": 10,  "avg_daily_units": 2.0, "days_of_cover": 25.0, "status": "reorder_soon", "reorder_qty": 66}
]
```

```bash
$ curl -s "localhost:8000/v1/ppc/recommendations?target_acos=0.30" -H "X-API-Key: $KEY"
```
```json
[
  {"keyword_text": "yoga mat non slip", "rule": "ACoS well below target with orders", "action": "increase_bid", "old_bid": "0.80", "new_bid": "0.88", "acos": 0.1, "clicks": 84, "orders": 14},
  {"keyword_text": "yoga mat", "rule": "ACoS well above target", "action": "decrease_bid", "old_bid": "1.10", "new_bid": "0.94", "acos": 0.5333, "clicks": 56, "orders": 7},
  {"keyword_text": "cheap water bottle", "rule": "no orders after enough clicks", "action": "pause", "old_bid": "0.60", "new_bid": null, "acos": null, "clicks": 28, "orders": 0}
]
```

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| POST | `/v1/sync` | Pulls the last 30 days from Amazon now (the nightly job does the same for every tenant) |
| GET | `/v1/sync/runs` | Last sync runs with status, row counts and error |
| GET | `/v1/stock/alerts` | Days of cover and reorder quantity, most urgent first (`only_alerts=false` lists every SKU) |
| GET | `/v1/margins` | Unit economics per SKU and marketplace (`marketplace=ES` to filter) |
| GET | `/v1/ppc/recommendations` | Bid changes the rules propose for a target ACoS, without touching Amazon |
| POST | `/v1/ppc/apply` | Applies them on Amazon and records each change (`dry_run=true` by default) |
| GET / PUT | `/v1/products`, `/v1/products/{sku}` | Product cost and supplier lead time, which Amazon does not know |

Interactive documentation at `http://localhost:8000/docs`.

## The business rules

**Stock.** `days_of_cover = (fulfillable + inbound) / average units sold per day` over the last 30 days, all marketplaces together (FBA stock is pooled across the EU). If the cover is shorter than the supplier lead time it is `reorder_now`; shorter than lead time plus 7 safety days, `reorder_soon`. The suggested quantity covers lead time, safety days and 30 days of sales after the shipment arrives.

**Margins.** On European marketplaces the price shown on Amazon includes VAT, and the referral fee is charged on that VAT-inclusive price:

```
net price = price / (1 + VAT)            ES 21 %, DE 19 %, FR 20 %, IT 22 %, UK 20 %
profit    = net price − referral fee − FBA fee − product cost (in the marketplace currency)
margin %  = profit / net price
```

A missing product cost is reported as missing, never guessed; a negative profit is flagged.

**PPC.** Rules are data (Pydantic models), evaluated in order, first match wins:

| Rule | Conditions (last 14 days) | Action |
| --- | --- | --- |
| No orders after enough clicks | clicks ≥ 25 and orders = 0 | pause |
| ACoS well above target | ACoS > target × 1.3 and clicks ≥ 15 | bid −15 % |
| ACoS well below target | ACoS < target × 0.7 and orders ≥ 3 | bid +10 % |

Bids stay between 0.10 and 5.00. A keyword changed in the last 7 days is skipped, because its stats still reflect the old bid; without that cooldown, running the rules twice would compound the change.

## Design decisions

- **Async where the time goes.** A sync is many slow HTTP calls: about 80 for the 5-SKU demo (5 SKUs × 5 marketplaces × 3 calls, plus inventory pages and a report), thousands for a real catalog. They run concurrently with `asyncio.gather`, while a semaphore caps parallel calls per seller so the client never floods Amazon. Database writes happen afterwards in a single session, because an `AsyncSession` must not be shared between concurrent tasks.
- **Amazon is slow and strict, so the client is defensive.** It caches the LWA access token and refreshes it before it expires (or once on a 401), retries 429 and 5xx using `Retry-After` when Amazon sends it and exponential backoff with jitter otherwise, and never retries 4xx errors that would fail again. Advertising reports are asynchronous: the client creates the report, polls until it is ready and downloads it, with the same retries.
- **Idempotent sync.** Every write is an `INSERT ... ON CONFLICT DO UPDATE`, so a sync can be re-run after a failure without duplicating rows. A failed sync rolls back entirely (the data stays as of the last good sync) and is recorded in `sync_runs` with its error.
- **Tenant isolation.** Each seller authenticates with its own API key, stored only as a SHA-256 hash. Every table carries `tenant_id` and every query filters by it; a SKU belonging to another tenant answers 404, the same as a SKU that does not exist. Tests check this explicitly.
- **Credentials encrypted at rest.** Each seller's Amazon refresh token is encrypted with Fernet (AES + HMAC). In production the key would live in a secrets manager or be replaced by KMS envelope encryption.
- **Postgres doing Postgres things.** Unique constraints that start with `tenant_id` double as its index (no redundant single-column indexes), `DISTINCT ON` picks each keyword's current bid, `JSONB` holds sync stats, `ARRAY` holds each tenant's marketplaces. The schema is built by Alembic migrations, and the test suite builds its database from those same migrations.
- **Pure business logic.** Stock, margin and PPC rules are plain functions with no database or HTTP, so they are tested directly and could move to a worker unchanged.
- **Safe by default.** Applying bids is a dry run unless `dry_run=false` is passed explicitly, and every applied change lands in `bid_changes`.

## Run it

With Docker:

```bash
cp .env.example .env    # then set ENCRYPTION_KEY (command inside the file)
docker compose up --build
docker compose exec api python -m app.cli create-tenant --name "Demo Brand" --marketplaces ES,DE,FR,IT,UK
# copy the API key it prints, then:
export KEY=so_...
curl -s -X POST localhost:8000/v1/sync -H "X-API-Key: $KEY"
curl -s localhost:8000/v1/stock/alerts -H "X-API-Key: $KEY"
```

Without Docker (Python 3.12 and a local PostgreSQL):

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env    # set ENCRYPTION_KEY and DATABASE_URL
alembic upgrade head
uvicorn mock_amazon.app:app --port 8001 &
uvicorn app.main:app --port 8000
```

Nightly job (cron, a Kubernetes CronJob or any scheduler): `python -m app.cli sync-all`. One tenant failing does not stop the others, and the exit code is 1 if any failed so the scheduler can alert.

## Tests

```bash
createdb seller_ops_test
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/seller_ops_test pytest -q
```

35 tests against a real PostgreSQL: business rules, the Amazon client (token caching and refresh, retries on 429/503, giving up, pagination, report polling) and the API end to end (sync, throttling, failed sync, idempotency, stock alerts, margins, PPC apply with audit and cooldown, tenant isolation, validation). CI runs them on every push with a PostgreSQL service container.

## What production would add

- Real Amazon authorization flow (OAuth consent from Seller Central) and the SP-API restricted data token where personal data is involved.
- Sync in a task queue (Celery or ARQ) instead of inside the request, with per-operation rate limits taken from Amazon's documented quotas.
- Ad spend attributed to SKUs (advertised products report) to compute margin after advertising.
- PPC rules stored per tenant and editable from a dashboard; keyword state tracked so paused keywords are not re-evaluated.
- Daily exchange rates from the ECB instead of static ones; alerts by email or Slack; row-level security in PostgreSQL as a second isolation layer.
