"""Bootstrap API key registration and management endpoints."""

from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from proxbox_api.database import ApiKey, DatabaseSessionDep

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
    has_env_key: bool


class BootstrapStatusResponse(BaseModel):
    needs_bootstrap: bool
    has_env_key: bool
    has_db_keys: bool


@router.get("/bootstrap-status", response_model=BootstrapStatusResponse)
def get_bootstrap_status(session: DatabaseSessionDep):
    """Check if bootstrap is needed (no API keys configured).

    This endpoint is auth-exempt to allow initial setup.
    """
    env_key = os.environ.get("PROXBOX_API_KEY", "").strip()
    has_db_keys = ApiKey.has_any_key(session)
    return BootstrapStatusResponse(
        needs_bootstrap=not env_key and not has_db_keys,
        has_env_key=bool(env_key),
        has_db_keys=has_db_keys,
    )


@router.post("/register-key", status_code=201)
def register_key(body: RegisterKeyRequest, session: DatabaseSessionDep):
    """One-time key bootstrap.

    Only succeeds when no API key is configured (neither env var nor database).
    Subsequent calls return 409 so this endpoint cannot be used to rotate keys.
    """
    if len(body.api_key) < 32:
        raise HTTPException(status_code=400, detail="API key must be at least 32 characters.")
    env_key = os.environ.get("PROXBOX_API_KEY", "").strip()
    if env_key or ApiKey.has_any_key(session):
        raise HTTPException(status_code=409, detail="An API key is already configured.")
    ApiKey.store_key(session, body.api_key, label=body.label)
    return {"detail": "API key registered."}


@router.post("/keys", status_code=201, response_model=CreateKeyResponse)
def create_key(session: DatabaseSessionDep):
    """Create a new API key.

    Generates a random 64-character API key and returns it.
    The key is stored hashed - this is the only time the raw key is visible.
    Requires authentication via existing API key or env var.
    """
    raw_key = secrets.token_urlsafe(48)
    obj = ApiKey.store_key(session, raw_key, label="")

    return CreateKeyResponse(
        id=obj.id,
        label=obj.label,
        is_active=obj.is_active,
        created_at=obj.created_at,
        raw_key=raw_key,
    )


@router.get("/keys", response_model=ApiKeyListResponse)
def list_keys(session: DatabaseSessionDep):
    """List all configured API keys (key values are not returned for security)."""
    keys = session.exec(select(ApiKey).order_by(ApiKey.created_at.desc())).all()
    env_key = os.environ.get("PROXBOX_API_KEY", "").strip()
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
        has_env_key=bool(env_key),
    )


@router.delete("/keys/{key_id}", status_code=204)
def delete_key(key_id: int, session: DatabaseSessionDep):
    """Delete an API key by ID."""
    key = session.get(ApiKey, key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API key not found.")
    session.delete(key)
    session.commit()
    return None


@router.post("/keys/{key_id}/deactivate", response_model=ApiKeyResponse)
def deactivate_key(key_id: int, session: DatabaseSessionDep):
    """Deactivate an API key (keeps it in DB but marks as inactive)."""
    key = session.get(ApiKey, key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API key not found.")
    key.is_active = False
    session.add(key)
    session.commit()
    session.refresh(key)
    return ApiKeyResponse(
        id=key.id,
        label=key.label,
        is_active=key.is_active,
        created_at=key.created_at,
    )


@router.post("/keys/{key_id}/activate", response_model=ApiKeyResponse)
def activate_key(key_id: int, session: DatabaseSessionDep):
    """Re-activate a deactivated API key."""
    key = session.get(ApiKey, key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API key not found.")
    key.is_active = True
    session.add(key)
    session.commit()
    session.refresh(key)
    return ApiKeyResponse(
        id=key.id,
        label=key.label,
        is_active=key.is_active,
        created_at=key.created_at,
    )


# Alias for backward compatibility
router_tags = ["auth"]
