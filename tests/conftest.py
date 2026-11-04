"""Test setup: a real PostgreSQL database (migrated with Alembic) and the mock Amazon app in-process."""
import os

from cryptography.fernet import Fernet

# Settings are read once, so the environment is prepared before the app is imported.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5432/seller_ops_test")
os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ["AMAZON_REPORT_POLL_SECONDS"] = "0"
os.environ["LWA_URL"] = os.environ["SP_API_URL"] = os.environ["ADS_API_URL"] = "http://mock-amazon"

import httpx  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

import mock_amazon.app as mock  # noqa: E402
from app.db import get_session  # noqa: E402
from app.deps import get_http  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, Tenant  # noqa: E402
from app.security import encrypt, hash_api_key, new_api_key  # noqa: E402

DB_URL = os.environ["DATABASE_URL"]
ROOT = os.path.dirname(os.path.dirname(__file__))


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    """Fresh schema built by the real migrations, so the tests also prove the migrations work."""
    import asyncio

    async def reset():
        engine = create_async_engine(DB_URL, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
        await engine.dispose()

    asyncio.run(reset())
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "alembic"))
    cfg.attributes["database_url"] = DB_URL
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


@pytest.fixture
async def session_factory():
    engine = create_async_engine(DB_URL, poolclass=NullPool)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def reset_mock_amazon():
    mock.BIDS.clear()
    mock.BIDS.update({kw: {"bid": v[3], "state": "ENABLED"} for kw, v in mock.KEYWORDS.items()})
    mock.REPORTS.clear()
    yield


def amazon_http() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=mock.app), base_url="http://mock-amazon")


@pytest.fixture
async def api(session_factory):
    async def _session():
        async with session_factory() as s:
            yield s

    async def _http():
        async with amazon_http() as http:
            yield http

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_http] = _http
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest.fixture
def make_tenant(session_factory):
    async def _make(name="Demo Brand", refresh_token="Atzr|demo", marketplaces=("ES", "DE", "FR", "IT", "UK")):
        key = new_api_key()
        async with session_factory() as s:
            s.add(Tenant(name=name, api_key_hash=hash_api_key(key), seller_id="A2DEMO", ads_profile_id="123",
                         refresh_token_enc=encrypt(refresh_token), marketplaces=list(marketplaces)))
            await s.commit()
        return {"X-API-Key": key}
    return _make
