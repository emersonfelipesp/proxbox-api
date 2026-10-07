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
from proxbox_api.database import (
    CephDashboardEndpoint,
    CephExternalCluster,
    NetBoxEndpoint,
    PBSEndpoint,
    PDMEndpoint,
    PrometheusSource,
    ProxmoxEndpoint,
    get_session,
)

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


# Every table whose secret columns are encrypted with the shared key (``encrypt_value`` in
# ``proxbox_api.database``). Replacing or deleting the key while any of these still holds
# ciphertext would make those credentials undecryptable.
_ENCRYPTED_COLUMNS: tuple[tuple[type, tuple[str, ...]], ...] = (
    (NetBoxEndpoint, ("token", "token_key")),
    (ProxmoxEndpoint, ("password", "token_value")),
    (PBSEndpoint, ("token_secret",)),
    (PDMEndpoint, ("token_secret",)),
    (PrometheusSource, ("bearer_token",)),
    (CephDashboardEndpoint, ("password", "token")),
    (CephExternalCluster, ("rgw_access_key", "rgw_secret_key")),
)


def _has_encrypted_values(session: Session) -> bool:
    """Return True if any stored credential value is already ciphertext."""
    for model, columns in _ENCRYPTED_COLUMNS:
        for row in session.exec(select(model)).all():
            for column in columns:
                value = getattr(row, column, None)
                if isinstance(value, str) and value.startswith("enc:"):
                    return True
    return False


def _reject_if_encrypted_values(session: Session) -> None:
    """Raise 409 when stored ``enc:`` credentials depend on the current key."""
    if _has_encrypted_values(session):
        raise HTTPException(
            status_code=409,
            detail=(
                "Encrypted credentials still exist in the database. Remove or rotate "
                "them before replacing or clearing the encryption key."
            ),
        )


@router.get("/encryption/status", response_model=EncryptionStatus)
async def get_encryption_status() -> EncryptionStatus:
    """Report whether a credential encryption key is configured and where it came from."""
    return _build_status()


@router.post("/encryption/key", response_model=EncryptionStatus)
async def set_encryption_key(
    payload: EncryptionKeyRequest,
    session: Annotated[Session, Depends(get_session)],
) -> EncryptionStatus:
    """Persist a caller-supplied encryption key to the local key file.

    Returns 409 if any encrypted credential exists, since replacing the key
    would strand those ciphertexts.
    """
    _reject_if_encrypted_values(session)
    set_local_encryption_key(payload.key)
    return _build_status()


@router.post("/encryption/generate", response_model=EncryptionKeyResponse)
async def generate_and_set_encryption_key(
    session: Annotated[Session, Depends(get_session)],
) -> EncryptionKeyResponse:
    """Generate a fresh Fernet key, persist it locally, and return it once.

    Returns 409 if any encrypted credential exists.
    """
    _reject_if_encrypted_values(session)
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
    _reject_if_encrypted_values(session)
    clear_local_encryption_key()
    return _build_status()
