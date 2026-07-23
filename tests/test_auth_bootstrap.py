"""Fail-closed API-key bootstrap and key-rotation invariants."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Session, select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.database import (
    ApiKey,
    ApiKeyBootstrapClaim,
    ApiKeyBootstrapConflict,
    _migrate_api_key_bootstrap_claim,
)
from proxbox_api.routes import auth as auth_routes

_FIRST_KEY = "first-bootstrap-key-aaaaaaaaaaaaaaaaaaaaaaaa"
_SECOND_KEY = "second-bootstrap-key-bbbbbbbbbbbbbbbbbbbbbbb"


def _async_factory(db_engine):
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    async_engine = create_async_engine(
        async_url,
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    return async_engine, async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


def test_inactive_key_never_reopens_public_bootstrap(test_client, db_session: Session) -> None:
    key = ApiKey.store_key(db_session, _FIRST_KEY, label="inactive-only")
    key.is_active = False
    db_session.add(key)
    db_session.commit()

    status = test_client.get("/auth/bootstrap-status")
    registration = test_client.post(
        "/auth/register-key",
        json={"api_key": _SECOND_KEY, "label": "hostile-rebootstrap"},
    )

    assert status.status_code == 200
    assert status.json() == {"needs_bootstrap": False, "has_db_keys": True}
    assert registration.status_code == 409
    assert registration.json() == {"detail": "An API key is already configured."}
    assert db_session.exec(select(ApiKey)).all() == [key]


@pytest.mark.asyncio
async def test_database_claim_allows_exactly_one_concurrent_first_key(db_engine) -> None:
    async_engine, factory = _async_factory(db_engine)

    async def register(candidate: str) -> str:
        async with factory() as session:
            try:
                await ApiKey.bootstrap_first_key_async(session, candidate, label="race")
            except ApiKeyBootstrapConflict:
                return "conflict"
            return "created"

    try:
        results = await asyncio.gather(
            register(_FIRST_KEY),
            register(_SECOND_KEY),
        )
        assert sorted(results) == ["conflict", "created"]
        async with factory() as session:
            assert len((await session.exec(select(ApiKey))).all()) == 1
            claims = (await session.exec(select(ApiKeyBootstrapClaim))).all()
            assert len(claims) == 1
            assert claims[0].id == 1
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_failed_first_key_transaction_rolls_back_claim_and_key(db_engine) -> None:
    async_engine, factory = _async_factory(db_engine)
    table = ApiKey.__tablename__
    trigger = "reject_bootstrap_key_for_test"
    async with async_engine.begin() as connection:
        await connection.execute(
            text(
                f"CREATE TRIGGER {trigger} BEFORE INSERT ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'forced bootstrap failure'); END"
            )
        )
    try:
        async with factory() as session:
            with pytest.raises(ApiKeyBootstrapConflict):
                await ApiKey.bootstrap_first_key_async(session, _FIRST_KEY, label="rollback")
        async with factory() as session:
            assert (await session.exec(select(ApiKey))).all() == []
            assert (await session.exec(select(ApiKeyBootstrapClaim))).all() == []
    finally:
        async with async_engine.begin() as connection:
            await connection.execute(text(f"DROP TRIGGER IF EXISTS {trigger}"))
        await async_engine.dispose()


def test_existing_key_migration_backfills_durable_claim(db_engine) -> None:
    with Session(db_engine) as session:
        ApiKey.store_key(session, _FIRST_KEY, label="legacy")
        assert session.get(ApiKeyBootstrapClaim, 1) is None

    _migrate_api_key_bootstrap_claim(db_engine)
    _migrate_api_key_bootstrap_claim(db_engine)

    with Session(db_engine) as session:
        claims = session.exec(select(ApiKeyBootstrapClaim)).all()
        assert len(claims) == 1
        assert claims[0].id == 1


def test_final_active_key_cannot_be_deactivated_or_deleted(
    auth_test_client,
    db_session: Session,
) -> None:
    key = db_session.exec(select(ApiKey)).one()

    deactivated = auth_test_client.post(f"/auth/keys/{key.id}/deactivate")
    deleted = auth_test_client.delete(f"/auth/keys/{key.id}")

    for response in (deactivated, deleted):
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "last_active_api_key_required"
    db_session.refresh(key)
    assert key.is_active is True


@pytest.mark.asyncio
async def test_concurrent_deactivation_preserves_one_active_key(db_engine) -> None:
    with Session(db_engine) as session:
        first = ApiKey.store_key(session, _FIRST_KEY, label="first")
        second = ApiKey.store_key(session, _SECOND_KEY, label="second")
        first_id = int(first.id or 0)
        second_id = int(second.id or 0)

    async_engine, factory = _async_factory(db_engine)

    async def deactivate(key_id: int) -> str:
        async with factory() as session:
            try:
                await auth_routes._deactivate_key_safely(session, key_id)
            except auth_routes.HTTPException as exc:
                assert exc.status_code == 409
                return "conflict"
            return "deactivated"

    try:
        results = await asyncio.gather(deactivate(first_id), deactivate(second_id))
        assert sorted(results) == ["conflict", "deactivated"]
        async with factory() as session:
            active = (
                await session.exec(select(ApiKey).where(ApiKey.is_active == True))  # noqa: E712
            ).all()
            assert len(active) == 1
    finally:
        await async_engine.dispose()


def test_rotation_allows_old_key_deactivation_and_deletion(
    auth_test_client,
    db_session: Session,
) -> None:
    old_key = db_session.exec(select(ApiKey)).one()
    new_key = ApiKey.store_key(db_session, _SECOND_KEY, label="replacement")

    deactivated = auth_test_client.post(f"/auth/keys/{old_key.id}/deactivate")
    auth_test_client.headers["X-Proxbox-API-Key"] = _SECOND_KEY
    deleted = auth_test_client.delete(f"/auth/keys/{old_key.id}")

    assert deactivated.status_code == 200
    assert deleted.status_code == 204
    db_session.refresh(new_key)
    assert new_key.is_active is True
