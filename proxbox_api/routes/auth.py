"""API key registration and management endpoints.

All keys are stored in the database with bcrypt hashing.
The first key registration is auth-exempt (bootstrap).
Subsequent operations require authentication.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from proxbox_api.database import ApiKey, AsyncDatabaseSessionDep

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterKeyRequest(BaseModel):
    api_key: str
    label: str = ""


class ApiKeyResponse(BaseModel):
    id: int
    label: str
    is_active: bool
    created_at: float


class CreateKeyResponse(ApiKeyResponse):
    raw_key: str


class ApiKeyListResponse(BaseModel):
    keys: list[ApiKeyResponse]


class BootstrapStatusResponse(BaseModel):
    needs_bootstrap: bool
    has_db_keys: bool


@router.get("/bootstrap-status", response_model=BootstrapStatusResponse)
async def get_bootstrap_status(session: AsyncDatabaseSessionDep):
    """Check if initial bootstrap is needed (no API keys exist)."""
    has_db_keys = await ApiKey.has_any_key_async(session)
    return BootstrapStatusResponse(
        needs_bootstrap=not has_db_keys,
        has_db_keys=has_db_keys,
    )


@router.post("/register-key", status_code=201)
async def register_key(body: RegisterKeyRequest, session: AsyncDatabaseSessionDep):
    """One-time bootstrap to register the first API key.

    Only succeeds when no API keys exist in the database.
    Returns 409 if a key already exists.
    """
    if len(body.api_key) < 32:
        raise HTTPException(status_code=400, detail="API key must be at least 32 characters.")

    if await ApiKey.has_any_key_async(session):
        raise HTTPException(status_code=409, detail="An API key is already configured.")

    await ApiKey.store_key_async(session, body.api_key, label=body.label)
    return {"detail": "API key registered."}


@router.post("/keys", status_code=201, response_model=CreateKeyResponse)
async def create_key(session: AsyncDatabaseSessionDep):
    """Create a new API key (requires existing authentication).

    Generates a random 64-character API key and returns it.
    The key is stored hashed - this is the only time the raw key is visible.
    """
    raw_key = secrets.token_urlsafe(48)
    obj = await ApiKey.store_key_async(session, raw_key, label="")

    return CreateKeyResponse(
        id=obj.id,
        label=obj.label,
        is_active=obj.is_active,
        created_at=obj.created_at,
        raw_key=raw_key,
    )


@router.get("/keys", response_model=ApiKeyListResponse)
async def list_keys(session: AsyncDatabaseSessionDep):
    """List all configured API keys (key values are not returned for security)."""
    result = await session.exec(select(ApiKey).order_by(ApiKey.created_at.desc()))
    keys = result.all()
    return ApiKeyListResponse(
        keys=[
            ApiKeyResponse(
                id=k.id,
                label=k.label,
                is_active=k.is_active,
                created_at=k.created_at,
            )
            for k in keys
        ],
    )


@router.delete("/keys/{key_id}", status_code=204)
async def delete_key(key_id: int, session: AsyncDatabaseSessionDep):
    """Delete an API key by ID."""
    key = await session.get(ApiKey, key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API key not found.")
    await session.delete(key)
    await session.commit()
    return None


@router.post("/keys/{key_id}/deactivate", response_model=ApiKeyResponse)
async def deactivate_key(key_id: int, session: AsyncDatabaseSessionDep):
    """Deactivate an API key (keeps it in DB but marks as inactive)."""
    key = await session.get(ApiKey, key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API key not found.")
    key.is_active = False
    session.add(key)
    await session.commit()
    await session.refresh(key)
    return ApiKeyResponse(
        id=key.id,
        label=key.label,
        is_active=key.is_active,
        created_at=key.created_at,
    )


@router.post("/keys/{key_id}/activate", response_model=ApiKeyResponse)
async def activate_key(key_id: int, session: AsyncDatabaseSessionDep):
    """Re-activate a deactivated API key."""
    key = await session.get(ApiKey, key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API key not found.")
    key.is_active = True
    session.add(key)
    await session.commit()
    await session.refresh(key)
    return ApiKeyResponse(
        id=key.id,
        label=key.label,
        is_active=key.is_active,
        created_at=key.created_at,
    )
