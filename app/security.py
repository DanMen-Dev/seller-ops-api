"""API-key authentication (one key per tenant) and encryption of Amazon credentials."""
import hashlib
import secrets
from typing import Annotated

from cryptography.fernet import Fernet
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import Tenant


def new_api_key() -> str:
    return "so_" + secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    # API keys are long random strings, so a fast hash is enough (unlike passwords, they cannot be guessed).
    return hashlib.sha256(api_key.encode()).hexdigest()


def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise RuntimeError("ENCRYPTION_KEY is not set")
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


async def get_tenant(
    session: Annotated[AsyncSession, Depends(get_session)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> Tenant:
    """Resolves the tenant from the X-API-Key header. Every endpoint filters its queries by this tenant."""
    if x_api_key:
        tenant = await session.scalar(select(Tenant).where(Tenant.api_key_hash == hash_api_key(x_api_key)))
        if tenant:
            return tenant
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid X-API-Key")
