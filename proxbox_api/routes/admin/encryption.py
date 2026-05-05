"""Runtime management of the credential encryption key.

These endpoints let an operator inspect the encryption status and configure /
rotate the local encryption key without restarting the service or setting
``PROXBOX_ENCRYPTION_KEY``. They complement the netbox-proxbox plugin settings
path (``ProxboxPluginSettings.encryption_key``).

Resolution order (already enforced in ``proxbox_api.credentials``):

    env var > plugin settings > local key file > none

Writes from these endpoints persist to the local key file and survive restarts.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from proxbox_api.credentials import (
    KeySource,
    clear_local_encryption_key,
    generate_encryption_key,
    get_encryption_source,
    is_encryption_enabled,
    set_local_encryption_key,
)
from proxbox_api.database import NetBoxEndpoint, ProxmoxEndpoint, get_session

router = APIRouter()


class EncryptionStatus(BaseModel):
    configured: bool
    source: KeySource | None = None


class EncryptionKeyRequest(BaseModel):
    key: str = Field(..., min_length=1, description="Raw encryption key value to persist locally.")


class EncryptionKeyResponse(EncryptionStatus):
    key: str | None = None


def _build_status() -> EncryptionStatus:
    return EncryptionStatus(configured=is_encryption_enabled(), source=get_encryption_source())


def _has_encrypted_values(session: Session) -> bool:
    """Return True if any stored credential value is already ciphertext."""
    netbox_rows = session.exec(select(NetBoxEndpoint)).all()
    for row in netbox_rows:
        for value in (row.token, row.token_key):
            if isinstance(value, str) and value.startswith("enc:"):
                return True
    proxmox_rows = session.exec(select(ProxmoxEndpoint)).all()
    for row in proxmox_rows:
        for value in (row.password, row.token_value):
            if isinstance(value, str) and value.startswith("enc:"):
                return True
    return False


@router.get("/encryption/status", response_model=EncryptionStatus)
async def get_encryption_status() -> EncryptionStatus:
    """Report whether a credential encryption key is configured and where it came from."""
    return _build_status()


@router.post("/encryption/key", response_model=EncryptionStatus)
async def set_encryption_key(payload: EncryptionKeyRequest) -> EncryptionStatus:
    """Persist a caller-supplied encryption key to the local key file."""
    set_local_encryption_key(payload.key)
    return _build_status()


@router.post("/encryption/generate", response_model=EncryptionKeyResponse)
async def generate_and_set_encryption_key() -> EncryptionKeyResponse:
    """Generate a fresh Fernet key, persist it locally, and return it once."""
    new_key = generate_encryption_key()
    set_local_encryption_key(new_key)
    status = _build_status()
    return EncryptionKeyResponse(configured=status.configured, source=status.source, key=new_key)


@router.delete("/encryption/key", response_model=EncryptionStatus)
async def delete_encryption_key(
    session: Annotated[Session, Depends(get_session)],
) -> EncryptionStatus:
    """Remove the locally persisted encryption key.

    Returns 409 if any encrypted (``enc:``-prefixed) credential value remains in
    the database, since dropping the key would strand those ciphertexts.
    """
    if _has_encrypted_values(session):
        raise HTTPException(
            status_code=409,
            detail=(
                "Encrypted credentials still exist in the database. Remove or rotate "
                "them before clearing the encryption key."
            ),
        )
    clear_local_encryption_key()
    return _build_status()
