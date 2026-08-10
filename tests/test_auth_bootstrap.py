"""Fail-closed API-key bootstrap and key-rotation invariants."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Session, select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api import auth
from proxbox_api.database import (
    ApiKey,
    ApiKeyActiveLimitError,
    ApiKeyBootstrapClaim,
    ApiKeyBootstrapConflict,
    _migrate_api_key_bootstrap_claim,
)
from proxbox_api.routes import auth as auth_routes
from proxbox_api.services.auth_lockout import AuthLockoutPolicy

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


def test_create_and_reactivate_reject_active_key_cap_crossing(
    auth_test_client,
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROXBOX_AUTH_MAX_ACTIVE_KEYS", "2")

    final_permitted = auth_test_client.post("/auth/keys")
    assert final_permitted.status_code == 201
    final_permitted_body = final_permitted.json()
    authorized, message = auth.check_auth_header_with_session(
        db_session,
        final_permitted_body["raw_key"],
        "192.0.2.45",
        AuthLockoutPolicy(max_active_keys=2),
    )
    assert authorized is True
    assert message is None

    active = db_session.exec(
        select(ApiKey).where(ApiKey.is_active == True).order_by(ApiKey.id)  # noqa: E712
    ).all()
    inactive = ApiKey(
        label="inactive-cap-test",
        key_hash=active[0].key_hash,
        is_active=False,
    )
    db_session.add(inactive)
    db_session.commit()
    db_session.refresh(inactive)

    rejected_create = auth_test_client.post("/auth/keys")
    rejected_reactivation = auth_test_client.post(f"/auth/keys/{inactive.id}/activate")

    for response in (rejected_create, rejected_reactivation):
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["code"] == "active_api_key_limit_reached"
        assert "2" in detail["message"]
        assert "deactivate" in detail["message"].lower()
    assert (
        len(
            db_session.exec(select(ApiKey).where(ApiKey.is_active == True)).all()  # noqa: E712
        )
        == 2
    )
    db_session.refresh(inactive)
    assert inactive.is_active is False


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


@pytest.mark.asyncio
async def test_concurrent_key_creation_cannot_cross_active_cap(db_engine) -> None:
    with Session(db_engine) as session:
        ApiKey.store_key(session, _FIRST_KEY, label="first")

    async_engine, factory = _async_factory(db_engine)

    async def create(candidate: str) -> str:
        async with factory() as session:
            try:
                await ApiKey.store_key_async(
                    session,
                    candidate,
                    label="concurrent",
                    max_active_keys=2,
                )
            except ApiKeyActiveLimitError:
                return "conflict"
            return "created"

    try:
        results = await asyncio.gather(create(_SECOND_KEY), create(f"{_SECOND_KEY}-other"))
        assert sorted(results) == ["conflict", "created"]
        async with factory() as session:
            active = (
                await session.exec(select(ApiKey).where(ApiKey.is_active == True))  # noqa: E712
            ).all()
            assert len(active) == 2
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
