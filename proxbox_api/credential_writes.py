"""Durable generation fencing for encrypted credential writes."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlmodel import Session, select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.credentials import (
    EncryptionKeyUseConflict,
    LocalKeyFileIdentity,
    encrypt_value,
    local_key_file_identity,
)
from proxbox_api.database import CredentialEncryptionKeyState
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

_GENERATION_CONFLICT = "The credential encryption key changed during this write. Retry the request."
_GENERATION_UNAVAILABLE = (
    "Credential encryption coordination is temporarily unavailable. Retry the request."
)


@dataclass(frozen=True, slots=True)
class CredentialWritePermit:
    """Database generation and local-key identity captured before ORM mutation."""

    generation: int
    local_file_identity: LocalKeyFileIdentity | None

    def encrypt(
        self,
        plaintext: str | None,
        *,
        plugin_conflict_detail: str | None = None,
    ) -> str | None:
        """Encrypt with the key identity captured for this database generation."""
        try:
            return encrypt_value(
                plaintext,
                expected_local_file_identity=self.local_file_identity,
                require_independent_source=plugin_conflict_detail is not None,
            )
        except EncryptionKeyUseConflict as exc:
            detail = (
                plugin_conflict_detail
                if exc.reason == "plugin_source" and plugin_conflict_detail is not None
                else _GENERATION_CONFLICT
            )
            raise HTTPException(status_code=409, detail=detail) from None


def _no_autoflush(session: Session | AsyncSession):
    return getattr(session, "no_autoflush", nullcontext())


async def prepare_credential_write(session: Session | AsyncSession) -> CredentialWritePermit:
    """Read the durable key generation before encrypting any credential value."""
    try:
        with _no_autoflush(session):
            state = await _maybe_await(session.get(CredentialEncryptionKeyState, 1))
            if state is None:
                # Normal startup creates the singleton. This compatibility path
                # supports direct library/test databases created with metadata only.
                state = CredentialEncryptionKeyState()
                session.add(state)
                flush = getattr(session, "flush", None)
                if flush is not None:
                    await _maybe_await(flush([state]))
                result = await _maybe_await(
                    session.exec(
                        select(CredentialEncryptionKeyState).where(
                            CredentialEncryptionKeyState.id == 1
                        )
                    )
                )
                state = result.one()
            if state.replacement_pending:
                raise HTTPException(status_code=409, detail=_GENERATION_CONFLICT)
            return CredentialWritePermit(state.generation, local_key_file_identity())
    except HTTPException:
        raise
    except SQLAlchemyError:
        await _maybe_await(session.rollback())
        raise HTTPException(
            status_code=503,
            detail=_GENERATION_UNAVAILABLE,
            headers={"Retry-After": "1"},
        ) from None


async def commit_credential_write(
    session: Session | AsyncSession, permit: CredentialWritePermit
) -> None:
    """Fence and commit ciphertext in one transaction, or return retryable 409."""
    try:
        statement = text(
            "UPDATE credential_encryption_key_state "
            "SET generation = generation "
            "WHERE id = 1 AND generation = :generation "
            "AND replacement_pending = 0"
        ).bindparams(generation=permit.generation)
        result = await _maybe_await(session.exec(statement))
        if result.rowcount != 1:
            await _maybe_await(session.rollback())
            raise HTTPException(status_code=409, detail=_GENERATION_CONFLICT)
        await _maybe_await(session.commit())
    except HTTPException:
        raise
    except OperationalError:
        await _maybe_await(session.rollback())
        raise HTTPException(status_code=409, detail=_GENERATION_CONFLICT) from None
