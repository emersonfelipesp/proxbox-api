"""Bootstrap API key registration endpoint."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from proxbox_api.database import ApiKey, DatabaseSessionDep

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterKeyRequest(BaseModel):
    api_key: str
    label: str = ""


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
