"""Command line.

    python -m app.cli create-tenant --name "Demo Brand"   prints the tenant's API key (shown once)
    python -m app.cli sync-all                            nightly job: syncs every tenant

sync-all is what a scheduler runs every morning (cron, a Kubernetes CronJob, EventBridge...).
One tenant failing does not stop the others; the exit code is 1 if any failed, so the scheduler can alert.
"""
import argparse
import asyncio
import json
import sys

import httpx
from sqlalchemy import select

from app.db import SessionLocal
from app.deps import build_client
from app.models import Tenant
from app.security import encrypt, hash_api_key, new_api_key
from app.services.sync import sync_tenant


async def create_tenant(name: str, seller_id: str, profile_id: str, refresh_token: str, marketplaces: list[str]) -> None:
    api_key = new_api_key()
    async with SessionLocal() as session:
        tenant = Tenant(name=name, api_key_hash=hash_api_key(api_key), seller_id=seller_id,
                        ads_profile_id=profile_id, refresh_token_enc=encrypt(refresh_token), marketplaces=marketplaces)
        session.add(tenant)
        await session.commit()
        print(f"Tenant {tenant.id} created. API key (store it now, it is not saved): {api_key}")


async def sync_all() -> int:
    async with SessionLocal() as session:
        tenants = (await session.scalars(select(Tenant).order_by(Tenant.id))).all()
    failures = 0
    async with httpx.AsyncClient(timeout=30) as http:
        for tenant in tenants:
            async with SessionLocal() as session:
                run = await sync_tenant(session, tenant, build_client(tenant, http))
            print(json.dumps({"tenant": tenant.id, "status": run.status, "stats": run.stats, "error": run.error}))
            failures += run.status != "ok"
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    t = sub.add_parser("create-tenant")
    t.add_argument("--name", required=True)
    t.add_argument("--seller-id", default="A2DEMOSELLER")
    t.add_argument("--profile-id", default="1234567890")
    t.add_argument("--refresh-token", default="Atzr|demo-refresh-token")
    t.add_argument("--marketplaces", default="ES,DE,FR,IT")
    sub.add_parser("sync-all")
    args = parser.parse_args()

    if args.command == "create-tenant":
        asyncio.run(create_tenant(args.name, args.seller_id, args.profile_id, args.refresh_token,
                                  args.marketplaces.split(",")))
    else:
        sys.exit(asyncio.run(sync_all()))


if __name__ == "__main__":
    main()
