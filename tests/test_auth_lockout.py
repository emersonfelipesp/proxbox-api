"""Regression tests for credential-isolated authentication lockout."""

from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import os
import sqlite3
import threading
import time
from collections.abc import Generator
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine, select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api import auth
from proxbox_api.database import (
    ApiKey,
    AuthLockout,
    AuthLockoutReservation,
    AuthLockoutSchemaError,
    DatabaseConfigurationError,
    _migrate_auth_lockout_schema,
    configure_sqlite_engine,
)
from proxbox_api.services import auth_lockout as lockout_module
from proxbox_api.services.auth_lockout import (
    AuthLockoutPolicy,
    AuthLockoutService,
    FailureResult,
    LockoutConfigurationError,
    LockoutState,
    build_lockout_identity,
    get_auth_lockout_metrics,
    get_auth_lockout_prometheus_metrics,
    parse_trusted_proxy_cidrs,
    resolve_auth_source_context,
    transition_failed_attempt,
)

VALID_KEY = "valid-test-api-key-aaaaaaaaaaaaaaaaaaaaaaaa"
STALE_KEY = "stale-test-api-key-bbbbbbbbbbbbbbbbbbbbbbbbb"
CLIENT_IP = "10.0.0.42"


def _record_failures_in_process(
    database_path: str,
    worker_number: int,
    attempts: int,
    max_buckets: int,
) -> None:
    target_engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    configure_sqlite_engine(target_engine)
    lockout_module.initialize_auth_lockout_identity_key(None)
    policy = AuthLockoutPolicy(
        threshold=100,
        source_threshold=100_000,
        window_seconds=60,
        max_buckets=max_buckets,
    )
    source = resolve_auth_source_context(CLIENT_IP, None, ())
    try:
        for attempt in range(attempts):
            identity = build_lockout_identity(
                source,
                f"rotated-{worker_number}-{attempt}",
            )
            with Session(target_engine) as session:
                AuthLockoutService.record_failure(session, identity, policy, now=100.0)
    finally:
        target_engine.dispose()
        lockout_module.clear_runtime_auth_lockout_identity_key()


def _bootstrap_database_in_process(database_path: str) -> None:
    from proxbox_api import database as database_module

    database_module._legacy_default_database_candidates = tuple
    try:
        database_module.initialize_database_and_schema({"PROXBOX_DATABASE_PATH": database_path})
    finally:
        asyncio.run(database_module.dispose_database())


def _hold_write_transaction_in_process(
    database_path: str,
    ready: Any,
    release: Any,
) -> None:
    connection = sqlite3.connect(database_path, timeout=5.0, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("BEGIN IMMEDIATE")
        ready.set()
        assert release.wait(timeout=5)
        connection.rollback()
    finally:
        connection.close()


def _finalize_reservation_in_process(
    database_path: str,
    raw_key: str,
    reservation_token: str,
) -> bool:
    target_engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    configure_sqlite_engine(target_engine)
    lockout_module.initialize_auth_lockout_identity_key(None)
    state = LockoutState(0, 100.0, None, 100.0)
    reservation = FailureResult(
        credential=state,
        source=state,
        reservation_token=reservation_token,
    )
    identity = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        raw_key,
    )
    try:
        with Session(target_engine) as session:
            return (
                AuthLockoutService.finalize_verification(
                    session,
                    identity,
                    AuthLockoutPolicy(threshold=100, source_threshold=100),
                    reservation,
                    succeeded=False,
                    now=101.0,
                )
                is not None
            )
    finally:
        target_engine.dispose()
        lockout_module.clear_runtime_auth_lockout_identity_key()


def _reserve_distinct_identity_in_process(
    database_path: str,
    worker_number: int,
    max_global_in_flight: int,
) -> bool:
    target_engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    configure_sqlite_engine(target_engine)
    lockout_module.initialize_auth_lockout_identity_key(None)
    identity = build_lockout_identity(
        resolve_auth_source_context(f"10.0.1.{worker_number + 1}", None, ()),
        f"multiprocess-distinct-key-{worker_number}",
    )
    try:
        with Session(target_engine) as session:
            return (
                AuthLockoutService.reserve_verification(
                    session,
                    identity,
                    AuthLockoutPolicy(
                        threshold=100,
                        source_threshold=100,
                        max_in_flight=10,
                        max_global_in_flight=max_global_in_flight,
                    ),
                    now=100.0,
                )
                is not None
            )
    finally:
        target_engine.dispose()
        lockout_module.clear_runtime_auth_lockout_identity_key()


def _bootstrap_database_with_identity_key_in_process(
    database_path: str,
    key_path: str,
) -> str:
    from proxbox_api import database as database_module

    os.environ.pop("PROXBOX_AUTH_LOCKOUT_HMAC_KEY", None)
    os.environ["PROXBOX_AUTH_LOCKOUT_HMAC_KEY_FILE"] = key_path
    lockout_module.clear_runtime_auth_lockout_identity_key()
    database_module._legacy_default_database_candidates = tuple
    try:
        database_module.initialize_database_and_schema({"PROXBOX_DATABASE_PATH": database_path})
    except database_module.DatabaseConfigurationError:
        return "rejected"
    finally:
        asyncio.run(database_module.dispose_database())
    return "accepted"


def _build_identity_with_generated_key_in_process(key_path: str) -> str:
    os.environ.pop("PROXBOX_AUTH_LOCKOUT_HMAC_KEY", None)
    os.environ["PROXBOX_AUTH_LOCKOUT_HMAC_KEY_FILE"] = key_path
    lockout_module.clear_runtime_auth_lockout_identity_key()
    lockout_module.initialize_auth_lockout_identity_key(None)
    try:
        return build_lockout_identity(
            resolve_auth_source_context(CLIENT_IP, None, ()),
            STALE_KEY,
        ).bucket_id
    finally:
        lockout_module.clear_runtime_auth_lockout_identity_key()


@pytest.fixture(autouse=True)
def _pin_test_identity_key() -> Generator[None, None, None]:
    lockout_module.clear_runtime_auth_lockout_identity_key()
    lockout_module.initialize_auth_lockout_identity_key(None)
    yield
    lockout_module.clear_runtime_auth_lockout_identity_key()


@pytest.fixture
def stored_key(db_session: Session) -> str:
    ApiKey.store_key(db_session, VALID_KEY, label="auth-lockout-test")
    # store_key() refreshes the row after commit, opening a new SQLite read
    # transaction; release it before concurrency tests open other connections.
    db_session.rollback()
    return VALID_KEY


def test_valid_key_returns_authorized(db_session: Session, stored_key: str) -> None:
    ok, message = auth.check_auth_header_with_session(db_session, stored_key, CLIENT_IP)
    assert ok is True
    assert message is None


def test_no_keys_configured_does_not_create_lockout_bucket(db_session: Session) -> None:
    ok, message = auth.check_auth_header_with_session(db_session, VALID_KEY, CLIENT_IP)
    assert ok is False
    assert message is not None and "No API key configured" in message
    assert AuthLockoutService.list_rows(db_session) == []


def test_same_ip_stale_key_cannot_lock_out_valid_key(
    db_session: Session,
    stored_key: str,
) -> None:
    policy = AuthLockoutPolicy(threshold=2, window_seconds=60)

    for _ in range(policy.threshold):
        ok, _ = auth.check_auth_header_with_session(
            db_session,
            STALE_KEY,
            CLIENT_IP,
            policy,
        )
        assert ok is False

    stale_ok, stale_message = auth.check_auth_header_with_session(
        db_session,
        STALE_KEY,
        CLIENT_IP,
        policy,
    )
    valid_ok, valid_message = auth.check_auth_header_with_session(
        db_session,
        stored_key,
        CLIENT_IP,
        policy,
    )

    assert stale_ok is False
    assert stale_message is not None and "Too many failed" in stale_message
    assert valid_ok is True
    assert valid_message is None
    stale_identity = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    )
    assert AuthLockoutService.is_locked(db_session, stale_identity)


def test_same_ip_stale_and_valid_keys_remain_isolated_during_race(
    db_engine,
    stored_key: str,
) -> None:
    policy = AuthLockoutPolicy(threshold=2, window_seconds=60)

    def authenticate(api_key: str) -> bool:
        with Session(db_engine) as session:
            authorized, _ = auth.check_auth_header_with_session(
                session,
                api_key,
                CLIENT_IP,
                policy,
            )
            return authorized

    submitted_keys = [STALE_KEY, stored_key] * 4
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(authenticate, submitted_keys))

    valid_results = [result for key, result in zip(submitted_keys, results) if key == stored_key]
    stale_results = [result for key, result in zip(submitted_keys, results) if key == STALE_KEY]
    # Speculative verification never consumes the durable failure budget.
    assert valid_results == [True] * 4
    assert stale_results == [False] * 4
    assert authenticate(stored_key) is True


def test_concurrent_requests_reserve_budget_before_bcrypt(
    db_engine,
    stored_key: str,
    monkeypatch,
) -> None:
    policy = AuthLockoutPolicy(
        threshold=1,
        source_threshold=100,
        window_seconds=60,
        max_in_flight=1,
    )
    entered = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    verification_calls = 0

    def slow_invalid_verification(session: Session, provided_key: str) -> bool:  # noqa: ARG001
        nonlocal verification_calls
        with counter_lock:
            verification_calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return False

    monkeypatch.setattr(ApiKey, "verify_any", staticmethod(slow_invalid_verification))

    def authenticate() -> tuple[bool, str | None]:
        with Session(db_engine) as session:
            return auth.check_auth_header_with_session(
                session,
                stored_key,
                CLIENT_IP,
                policy,
            )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(authenticate) for _ in range(8)]
        assert entered.wait(timeout=5)
        time.sleep(0.1)
        release.set()
        results = [future.result(timeout=5) for future in futures]

    assert verification_calls == 1
    assert sum("verification capacity" in (message or "") for _, message in results) == 7
    assert all("Too many failed" not in (message or "") for _, message in results)


def test_more_than_failure_threshold_concurrent_valid_requests_never_lock(
    db_engine,
    stored_key: str,
    monkeypatch,
) -> None:
    policy = AuthLockoutPolicy(
        threshold=2,
        source_threshold=3,
        window_seconds=60,
        max_in_flight=16,
    )
    entered = threading.Barrier(9)
    release = threading.Event()

    def slow_valid_verification(session: Session, provided_key: str) -> bool:  # noqa: ARG001
        entered.wait(timeout=5)
        assert release.wait(timeout=5)
        return True

    monkeypatch.setattr(ApiKey, "verify_any", staticmethod(slow_valid_verification))

    def authenticate() -> tuple[bool, str | None]:
        with Session(db_engine) as session:
            return auth.check_auth_header_with_session(
                session,
                stored_key,
                CLIENT_IP,
                policy,
            )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(authenticate) for _ in range(8)]
        entered.wait(timeout=5)
        release.set()
        results = [future.result(timeout=5) for future in futures]

    assert results == [(True, None)] * 8
    with Session(db_engine) as session:
        assert AuthLockoutService.list_rows(session) == []


async def test_async_valid_burst_above_failure_threshold_never_locks(
    db_engine,
    stored_key: str,
    monkeypatch,
) -> None:
    policy = AuthLockoutPolicy(
        threshold=2,
        source_threshold=3,
        window_seconds=60,
        max_in_flight=16,
    )
    db_engine.dispose()
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    target = create_async_engine(async_url, connect_args={"check_same_thread": False})
    configure_sqlite_engine(target.sync_engine)
    sessions = async_sessionmaker(target, class_=AsyncSession, expire_on_commit=False)
    entered = 0
    entered_lock = asyncio.Lock()
    all_entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_valid_verification(session, provided_key: str) -> bool:  # noqa: ANN001, ARG001
        nonlocal entered
        async with entered_lock:
            entered += 1
            if entered == 8:
                all_entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(ApiKey, "verify_any_async", staticmethod(slow_valid_verification))

    async def authenticate() -> tuple[bool, str | None]:
        async with sessions() as session:
            return await auth.check_auth_header_with_session_async(
                session,
                stored_key,
                CLIENT_IP,
                policy,
            )

    try:
        tasks = [asyncio.create_task(authenticate()) for _ in range(8)]
        await asyncio.wait_for(all_entered.wait(), timeout=5)
        release.set()
        assert await asyncio.gather(*tasks) == [(True, None)] * 8
        async with sessions() as session:
            assert (await session.exec(select(AuthLockout))).all() == []
    finally:
        await target.dispose()


def test_http_valid_burst_above_failure_threshold_never_returns_lockout(
    db_engine,
    stored_key: str,
    monkeypatch,
) -> None:
    from fastapi import FastAPI

    from proxbox_api.app.factory import APIKeyAuthMiddleware
    from proxbox_api.database import get_session

    policy = AuthLockoutPolicy(
        threshold=2,
        source_threshold=3,
        window_seconds=60,
        max_in_flight=16,
    )
    entered = threading.Barrier(9)
    release = threading.Event()

    def slow_valid_verification(session: Session, provided_key: str) -> bool:  # noqa: ARG001
        entered.wait(timeout=5)
        assert release.wait(timeout=5)
        return True

    monkeypatch.setattr(ApiKey, "verify_any", staticmethod(slow_valid_verification))
    application = FastAPI()

    @application.get("/protected")
    def protected() -> dict[str, bool]:
        return {"ok": True}

    application.add_middleware(APIKeyAuthMiddleware, policy=policy)

    def override_get_session():
        with Session(db_engine) as session:
            yield session

    application.dependency_overrides[get_session] = override_get_session
    with TestClient(application) as client:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [
                executor.submit(
                    client.get,
                    "/protected",
                    headers={"X-Proxbox-API-Key": stored_key},
                )
                for _ in range(8)
            ]
            entered.wait(timeout=5)
            release.set()
            responses = [future.result(timeout=5) for future in futures]

    assert [response.status_code for response in responses] == [200] * 8
    assert all("Retry-After" not in response.headers for response in responses)


def test_http_verification_capacity_returns_503_without_recording_failure(
    db_engine,
    stored_key: str,
    monkeypatch,
) -> None:
    from fastapi import FastAPI

    from proxbox_api.app.factory import APIKeyAuthMiddleware
    from proxbox_api.database import get_session

    policy = AuthLockoutPolicy(
        threshold=1,
        source_threshold=1,
        window_seconds=60,
        max_in_flight=1,
    )
    entered = threading.Event()
    release = threading.Event()

    def slow_valid_verification(session: Session, provided_key: str) -> bool:  # noqa: ARG001
        entered.set()
        assert release.wait(timeout=5)
        return True

    monkeypatch.setattr(ApiKey, "verify_any", staticmethod(slow_valid_verification))
    application = FastAPI()

    @application.get("/protected")
    def protected() -> dict[str, bool]:
        return {"ok": True}

    application.add_middleware(APIKeyAuthMiddleware, policy=policy)

    def override_get_session():
        with Session(db_engine) as session:
            yield session

    application.dependency_overrides[get_session] = override_get_session
    with TestClient(application) as client:
        with ThreadPoolExecutor(max_workers=2) as executor:
            admitted = executor.submit(
                client.get,
                "/protected",
                headers={"X-Proxbox-API-Key": stored_key},
            )
            assert entered.wait(timeout=5)
            rejected = client.get(
                "/protected",
                headers={"X-Proxbox-API-Key": stored_key},
            )
            release.set()
            admitted_response = admitted.result(timeout=5)

    assert admitted_response.status_code == 200
    assert rejected.status_code == 503
    assert rejected.headers["Retry-After"] == "1"
    assert rejected.json() == {
        "detail": "Authentication verification capacity is temporarily exhausted."
    }
    with Session(db_engine) as session:
        assert AuthLockoutService.list_rows(session) == []
        assert get_auth_lockout_metrics(session)["proxbox_auth_capacity_rejections_total"] == 1


def test_websocket_valid_burst_above_failure_threshold_never_closes_as_locked(
    db_engine,
    stored_key: str,
    monkeypatch,
) -> None:
    from fastapi import FastAPI

    from proxbox_api.app import websockets

    policy = AuthLockoutPolicy(
        threshold=2,
        source_threshold=3,
        window_seconds=60,
        max_in_flight=16,
    )
    entered = threading.Barrier(9)
    release = threading.Event()

    def slow_valid_verification(session: Session, provided_key: str) -> bool:  # noqa: ARG001
        entered.wait(timeout=5)
        assert release.wait(timeout=5)
        return True

    def check_with_test_database(api_key, source):  # noqa: ANN001
        with Session(db_engine) as session:
            return auth.check_auth_header_with_session(session, api_key, source, policy)

    monkeypatch.setattr(ApiKey, "verify_any", staticmethod(slow_valid_verification))
    monkeypatch.setattr(websockets, "check_auth_header", check_with_test_database)
    application = FastAPI()
    application.include_router(websockets.websocket_router)

    def connect(client: TestClient) -> str:
        with client.websocket_connect("/") as socket:
            socket.send_json({"api_key": stored_key})
            return socket.receive_text()

    with TestClient(application) as client:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(connect, client) for _ in range(8)]
            entered.wait(timeout=5)
            release.set()
            messages = [future.result(timeout=5) for future in futures]

    assert messages == ["Message: 1"] * 8


@pytest.mark.parametrize(
    "payload",
    [[], 7, {"api_key": []}, {"api_key": {}}, {"api_key": 7}],
)
def test_websocket_non_string_api_key_uses_normal_auth_close(
    monkeypatch,
    payload: object,
) -> None:
    from fastapi import FastAPI

    from proxbox_api.app import websockets

    monkeypatch.setattr(
        websockets,
        "check_auth_header",
        lambda api_key, source: (False, "Invalid API key"),
    )
    application = FastAPI()
    application.include_router(websockets.websocket_router)

    with TestClient(application) as client:
        with client.websocket_connect("/") as socket:
            socket.send_json(payload)
            close = socket.receive()

    assert close["type"] == "websocket.close"
    assert close["code"] == 4001


def test_duplicate_finalizer_cannot_consume_a_newer_token(db_session: Session) -> None:
    policy = AuthLockoutPolicy(
        threshold=1,
        source_threshold=10,
        window_seconds=1,
        max_in_flight=1,
    )
    identity = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    )

    first = AuthLockoutService.reserve_verification(db_session, identity, policy, now=100.0)
    assert first is not None
    assert AuthLockoutService.reserve_verification(db_session, identity, policy, now=101.0) is None
    first_result = AuthLockoutService.finalize_verification(
        db_session,
        identity,
        policy,
        first,
        succeeded=False,
        now=101.1,
    )
    assert first_result is not None and first_result.credential.attempts == 1

    second = AuthLockoutService.reserve_verification(db_session, identity, policy, now=102.1)
    assert second is not None

    duplicate = AuthLockoutService.finalize_verification(
        db_session,
        identity,
        policy,
        first,
        succeeded=False,
        now=102.2,
    )
    assert duplicate is None
    assert db_session.get(AuthLockoutReservation, second.reservation_token) is not None
    row = AuthLockoutService.get(db_session, identity)
    assert row is not None and row.attempts == 1


def test_multiprocess_finalizers_consume_a_reservation_exactly_once(db_engine) -> None:
    policy = AuthLockoutPolicy(threshold=100, source_threshold=100)
    identity = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    )
    with Session(db_engine) as session:
        reservation = AuthLockoutService.reserve_verification(
            session,
            identity,
            policy,
            now=100.0,
        )
        assert reservation is not None

    database_path = str(db_engine.url.database)
    db_engine.dispose()
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as executor:
        futures = [
            executor.submit(
                _finalize_reservation_in_process,
                database_path,
                STALE_KEY,
                reservation.reservation_token,
            )
            for _ in range(4)
        ]
        results = [future.result(timeout=30) for future in futures]

    assert results.count(True) == 1
    assert results.count(False) == 3
    verified = create_engine(f"sqlite:///{database_path}")
    try:
        with Session(verified) as session:
            row = AuthLockoutService.get(session, identity)
            assert row is not None and row.attempts == 1
            metrics = get_auth_lockout_metrics(session, now=101.0)
            assert metrics["proxbox_auth_failures_total"] == 1
            assert metrics["proxbox_auth_verifications_in_flight"] == 0
    finally:
        verified.dispose()


def test_individual_orphan_expiry_does_not_extend_or_lose_late_failure(
    db_engine,
) -> None:
    policy = AuthLockoutPolicy(
        threshold=1,
        source_threshold=10,
        window_seconds=1,
        max_in_flight=2,
    )
    identity = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    )
    with Session(db_engine) as session:
        abandoned = AuthLockoutService.reserve_verification(session, identity, policy, now=100.0)
        assert abandoned is not None

    with Session(db_engine) as restarted_session:
        live = AuthLockoutService.reserve_verification(
            restarted_session, identity, policy, now=159.0
        )
        assert live is not None
        assert (
            AuthLockoutService.reserve_verification(restarted_session, identity, policy, now=159.9)
            is None
        )
        recovered = AuthLockoutService.reserve_verification(
            restarted_session, identity, policy, now=160.0
        )
        assert recovered is not None

        late = AuthLockoutService.finalize_verification(
            restarted_session,
            identity,
            policy,
            abandoned,
            succeeded=False,
            now=220.0,
        )
        assert late is not None and late.credential.attempts == 1
        live_result = AuthLockoutService.finalize_verification(
            restarted_session,
            identity,
            policy,
            live,
            succeeded=False,
            now=220.1,
        )
        assert live_result is not None and live_result.credential.attempts == 2
        row = AuthLockoutService.get(restarted_session, identity)
        assert row is not None and row.attempts == 2
        assert (
            restarted_session.get(AuthLockoutReservation, recovered.reservation_token) is not None
        )


def test_global_verification_capacity_bounds_distinct_identities(db_session: Session) -> None:
    policy = AuthLockoutPolicy(
        threshold=100,
        source_threshold=100,
        max_in_flight=10,
        max_global_in_flight=2,
    )
    identities = [
        build_lockout_identity(
            resolve_auth_source_context(f"10.0.0.{index}", None, ()),
            f"distinct-stale-key-{index}",
        )
        for index in range(1, 4)
    ]

    first = AuthLockoutService.reserve_verification(db_session, identities[0], policy, now=100.0)
    second = AuthLockoutService.reserve_verification(db_session, identities[1], policy, now=100.0)
    assert first is not None
    assert second is not None
    assert (
        AuthLockoutService.reserve_verification(db_session, identities[2], policy, now=100.0)
        is None
    )
    assert (
        get_auth_lockout_metrics(db_session, now=100.0)["proxbox_auth_capacity_rejections_total"]
        == 1
    )

    assert (
        AuthLockoutService.finalize_verification(
            db_session,
            identities[0],
            policy,
            first,
            succeeded=True,
            now=101.0,
        )
        is not None
    )
    assert (
        AuthLockoutService.reserve_verification(db_session, identities[2], policy, now=101.0)
        is not None
    )


def test_global_verification_capacity_is_atomic_across_processes(db_engine) -> None:
    database_path = str(db_engine.url.database)
    db_engine.dispose()
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as executor:
        results = list(
            executor.map(
                _reserve_distinct_identity_in_process,
                [database_path] * 4,
                range(4),
                [2] * 4,
            )
        )

    assert results.count(True) == 2
    assert results.count(False) == 2
    verified = create_engine(f"sqlite:///{database_path}")
    try:
        with Session(verified) as session:
            metrics = get_auth_lockout_metrics(session, now=100.0)
            assert metrics["proxbox_auth_verifications_in_flight"] == 2
            assert metrics["proxbox_auth_capacity_rejections_total"] == 2
    finally:
        verified.dispose()


def test_reservations_beyond_finalization_horizon_are_compacted(
    db_session: Session,
) -> None:
    policy = AuthLockoutPolicy(threshold=100, source_threshold=100)
    identity = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    )
    abandoned = AuthLockoutService.reserve_verification(db_session, identity, policy, now=100.0)
    assert abandoned is not None

    replacement = AuthLockoutService.reserve_verification(
        db_session,
        identity,
        policy,
        now=4_000.0,
    )
    assert replacement is not None
    assert db_session.get(AuthLockoutReservation, abandoned.reservation_token) is None
    metrics = get_auth_lockout_metrics(db_session, now=4_000.0)
    assert metrics["proxbox_auth_orphan_compactions_total"] == 1
    assert metrics["proxbox_auth_expired_orphan_reservations"] == 0
    assert (
        AuthLockoutService.finalize_verification(
            db_session,
            identity,
            policy,
            abandoned,
            succeeded=False,
            now=4_000.1,
        )
        is None
    )


def test_missing_and_presented_credentials_have_distinct_buckets(
    db_session: Session,
    stored_key: str,
) -> None:
    policy = AuthLockoutPolicy(threshold=2, window_seconds=60)
    auth.check_auth_header_with_session(db_session, None, CLIENT_IP, policy)
    auth.check_auth_header_with_session(db_session, STALE_KEY, CLIENT_IP, policy)

    rows = AuthLockoutService.list_rows(db_session)
    credential_rows = [row for row in rows if row.bucket_type == "credential"]
    source_rows = [row for row in rows if row.bucket_type == "source"]
    assert len(credential_rows) == 2
    assert len(source_rows) == 1
    assert len({row.bucket_id for row in rows}) == 3
    assert all(stored_key not in repr(row) for row in rows)
    assert all(STALE_KEY not in repr(row) for row in rows)


@pytest.mark.parametrize(
    ("threshold", "window_seconds"),
    [(1, 1), (100, 86_400)],
)
def test_policy_accepts_documented_boundaries(threshold: int, window_seconds: int) -> None:
    assert AuthLockoutPolicy(threshold=threshold, window_seconds=window_seconds) == (
        AuthLockoutPolicy.from_env(
            {
                "PROXBOX_AUTH_LOCKOUT_THRESHOLD": str(threshold),
                "PROXBOX_AUTH_LOCKOUT_WINDOW_SECONDS": str(window_seconds),
            }
        )
    )


@pytest.mark.parametrize(
    "values",
    [
        {"PROXBOX_AUTH_LOCKOUT_THRESHOLD": "0"},
        {"PROXBOX_AUTH_LOCKOUT_THRESHOLD": "101"},
        {"PROXBOX_AUTH_LOCKOUT_THRESHOLD": "five"},
        {"PROXBOX_AUTH_LOCKOUT_WINDOW_SECONDS": "0"},
        {"PROXBOX_AUTH_LOCKOUT_WINDOW_SECONDS": "86401"},
        {"PROXBOX_AUTH_LOCKOUT_WINDOW_SECONDS": "1.5"},
        {"PROXBOX_AUTH_LOCKOUT_SOURCE_THRESHOLD": "0"},
        {"PROXBOX_AUTH_LOCKOUT_SOURCE_THRESHOLD": "100001"},
        {"PROXBOX_AUTH_LOCKOUT_MAX_BUCKETS": "1"},
        {"PROXBOX_AUTH_LOCKOUT_MAX_BUCKETS": "1000001"},
        {"PROXBOX_AUTH_LOCKOUT_MAX_IN_FLIGHT": "0"},
        {"PROXBOX_AUTH_LOCKOUT_MAX_IN_FLIGHT": "1025"},
        {"PROXBOX_AUTH_LOCKOUT_MAX_GLOBAL_IN_FLIGHT": "0"},
        {"PROXBOX_AUTH_LOCKOUT_MAX_GLOBAL_IN_FLIGHT": "4097"},
    ],
)
def test_policy_rejects_invalid_configuration(values: dict[str, str]) -> None:
    with pytest.raises(LockoutConfigurationError):
        AuthLockoutPolicy.from_env(values)


def test_fixed_window_transition_resets_at_exact_boundary() -> None:
    policy = AuthLockoutPolicy(threshold=2, window_seconds=10)
    first = transition_failed_attempt(None, policy, now=100.0)
    locked = transition_failed_attempt(first, policy, now=109.999)
    reset = transition_failed_attempt(locked, policy, now=110.0)

    assert locked.is_locked(109.999)
    assert not locked.is_locked(110.0)
    assert reset == LockoutState(
        attempts=1,
        window_started_at=110.0,
        locked_until=None,
        updated_at=110.0,
    )


def test_atomic_database_transition_resets_at_exact_boundary(db_session: Session) -> None:
    policy = AuthLockoutPolicy(threshold=2, window_seconds=10)
    identity = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    )
    first = AuthLockoutService.record_failure(db_session, identity, policy, now=100.0)
    reset = AuthLockoutService.record_failure(db_session, identity, policy, now=110.0)

    assert first.attempts == 1
    assert reset.attempts == 1
    assert reset.credential.window_started_at == 110.0
    assert reset.credential.locked_until is None


def test_failure_write_prunes_inactive_expired_buckets(db_session: Session) -> None:
    policy = AuthLockoutPolicy(threshold=5, window_seconds=10)
    expired_identity = build_lockout_identity(
        resolve_auth_source_context("10.0.0.10", None, ()),
        STALE_KEY,
    )
    current_identity = build_lockout_identity(
        resolve_auth_source_context("10.0.0.11", None, ()),
        STALE_KEY,
    )
    AuthLockoutService.record_failure(db_session, expired_identity, policy, now=100.0)
    AuthLockoutService.record_failure(db_session, current_identity, policy, now=111.0)

    assert AuthLockoutService.get(db_session, expired_identity) is None
    assert AuthLockoutService.get(db_session, current_identity) is not None


def test_rotating_credentials_exhaust_durable_source_budget(db_session: Session) -> None:
    policy = AuthLockoutPolicy(
        threshold=5,
        source_threshold=3,
        window_seconds=60,
        max_buckets=20,
    )
    source = resolve_auth_source_context(CLIENT_IP, None, ())
    identities = [build_lockout_identity(source, f"rotated-key-{index}") for index in range(4)]

    for identity in identities[:3]:
        result = AuthLockoutService.record_failure(db_session, identity, policy, now=100.0)
        assert result.credential.attempts == 1

    assert AuthLockoutService.is_locked(db_session, identities[3], now=100.0)
    source_row = AuthLockoutService.get_source(db_session, identities[3])
    assert source_row is not None
    assert source_row.attempts == 3
    assert source_row.locked_until == 160.0


def test_rotating_invalid_keys_block_bcrypt_for_valid_key(
    db_session: Session,
    stored_key: str,
) -> None:
    policy = AuthLockoutPolicy(
        threshold=5,
        source_threshold=3,
        window_seconds=60,
        max_buckets=20,
    )
    for index in range(policy.source_threshold):
        authorized, _ = auth.check_auth_header_with_session(
            db_session,
            f"rotated-invalid-{index}",
            CLIENT_IP,
            policy,
        )
        assert authorized is False

    authorized, message = auth.check_auth_header_with_session(
        db_session,
        stored_key,
        CLIENT_IP,
        policy,
    )
    assert authorized is False
    assert message is not None and "Too many failed" in message


def test_partitioned_row_cap_preserves_valid_authentication_and_counts_saturation(
    db_session: Session,
    stored_key: str,
) -> None:
    policy = AuthLockoutPolicy(
        threshold=100,
        source_threshold=100,
        window_seconds=60,
        max_buckets=2,
    )
    first = build_lockout_identity(
        resolve_auth_source_context("10.0.0.10", None, ()),
        "first-invalid-key",
    )
    saturated = build_lockout_identity(
        resolve_auth_source_context("10.0.0.11", None, ()),
        "second-invalid-key",
    )
    AuthLockoutService.record_failure(db_session, first, policy, now=100.0)
    dropped = AuthLockoutService.record_failure(db_session, saturated, policy, now=100.0)

    rows = AuthLockoutService.list_rows(db_session)
    assert len(rows) == policy.max_buckets
    assert dropped.credential.attempts == 1
    assert AuthLockoutService.get(db_session, saturated) is None
    assert AuthLockoutService.get_source(db_session, saturated) is None
    assert (
        get_auth_lockout_metrics(db_session, now=100.0)["proxbox_auth_capacity_rejections_total"]
        == 1
    )

    authorized, message = auth.check_auth_header_with_session(
        db_session,
        stored_key,
        "10.0.0.11",
        policy,
    )
    assert authorized is True
    assert message is None


def test_bucket_cap_preserves_other_source_at_threshold_minus_one(
    db_session: Session,
) -> None:
    policy = AuthLockoutPolicy(
        threshold=5,
        source_threshold=3,
        window_seconds=60,
        max_buckets=4,
    )
    protected = build_lockout_identity(
        resolve_auth_source_context("10.0.0.10", None, ()),
        "protected-invalid-key",
    )
    for _ in range(policy.source_threshold - 1):
        AuthLockoutService.record_failure(db_session, protected, policy, now=100.0)

    filler = build_lockout_identity(
        resolve_auth_source_context("10.0.0.11", None, ()),
        "filler-invalid-key",
    )
    AuthLockoutService.record_failure(db_session, filler, policy, now=100.0)
    pressure = build_lockout_identity(
        resolve_auth_source_context("10.0.0.12", None, ()),
        "pressure-invalid-key",
    )
    AuthLockoutService.record_failure(db_session, pressure, policy, now=100.0)

    protected_source = AuthLockoutService.get_source(db_session, protected)
    assert protected_source is not None
    assert protected_source.attempts == policy.source_threshold - 1
    result = AuthLockoutService.record_failure(db_session, protected, policy, now=100.0)
    assert result.source.attempts == policy.source_threshold
    assert result.source.locked_until == 160.0


def test_bucket_cap_never_evicts_an_active_lockout(db_session: Session) -> None:
    policy = AuthLockoutPolicy(
        threshold=1,
        source_threshold=100,
        window_seconds=60,
        max_buckets=3,
    )
    first = build_lockout_identity(
        resolve_auth_source_context("10.0.0.1", None, ()),
        "first-invalid-key",
    )
    second = build_lockout_identity(
        resolve_auth_source_context("10.0.0.2", None, ()),
        "second-invalid-key",
    )

    assert AuthLockoutService.record_failure(db_session, first, policy, now=100.0) is not None
    AuthLockoutService.record_failure(db_session, second, policy, now=100.0)

    assert AuthLockoutService.is_locked(db_session, first, now=100.0)
    assert len(AuthLockoutService.list_rows(db_session)) == policy.max_buckets
    assert (
        get_auth_lockout_metrics(db_session, now=100.0)["proxbox_auth_capacity_rejections_total"]
        == 1
    )


def test_persisted_credential_identifier_is_not_an_unkeyed_hash() -> None:
    identity = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    )
    unkeyed = hashlib.sha256(
        b"proxbox-auth-credential-v1\0" + STALE_KEY.encode("utf-8")
    ).hexdigest()

    assert identity.credential_id != unkeyed[:12]


def test_identity_key_file_is_created_private_and_stable(tmp_path, monkeypatch) -> None:
    key_path = tmp_path / "lockout-identity.key"
    monkeypatch.delenv("PROXBOX_AUTH_LOCKOUT_HMAC_KEY", raising=False)
    monkeypatch.setenv("PROXBOX_AUTH_LOCKOUT_HMAC_KEY_FILE", str(key_path))
    lockout_module.clear_runtime_auth_lockout_identity_key()
    try:
        lockout_module.initialize_auth_lockout_identity_key(None)
        first = build_lockout_identity(
            resolve_auth_source_context(CLIENT_IP, None, ()),
            STALE_KEY,
        )
        assert key_path.is_file()
        assert key_path.stat().st_mode & 0o077 == 0

        lockout_module._identity_hmac_key.cache_clear()
        second = build_lockout_identity(
            resolve_auth_source_context(CLIENT_IP, None, ()),
            STALE_KEY,
        )
        assert first == second
    finally:
        lockout_module.clear_runtime_auth_lockout_identity_key()


def test_identity_key_file_creation_is_safe_across_processes(tmp_path) -> None:
    key_path = tmp_path / "multiprocess-lockout-identity.key"
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as executor:
        identities = list(
            executor.map(
                _build_identity_with_generated_key_in_process,
                [str(key_path)] * 4,
            )
        )

    assert len(set(identities)) == 1
    assert key_path.stat().st_mode & 0o077 == 0


def test_identity_key_file_symlink_is_refused(tmp_path, monkeypatch) -> None:
    target = tmp_path / "real-identity.key"
    target.write_text("private-key-material-" + "x" * 32, encoding="utf-8")
    target.chmod(0o600)
    key_path = tmp_path / "linked-identity.key"
    key_path.symlink_to(target)
    monkeypatch.delenv("PROXBOX_AUTH_LOCKOUT_HMAC_KEY", raising=False)
    monkeypatch.setenv("PROXBOX_AUTH_LOCKOUT_HMAC_KEY_FILE", str(key_path))
    lockout_module.clear_runtime_auth_lockout_identity_key()

    with pytest.raises(LockoutConfigurationError, match="unable to read"):
        lockout_module.initialize_auth_lockout_identity_key(None)


def test_database_binding_rejects_different_key_worker_generation(
    db_engine,
    tmp_path,
    monkeypatch,
) -> None:
    first_key = tmp_path / "identity-a.key"
    second_key = tmp_path / "identity-b.key"
    first_key.write_text("a" * 48, encoding="utf-8")
    second_key.write_text("b" * 48, encoding="utf-8")
    first_key.chmod(0o600)
    second_key.chmod(0o600)
    monkeypatch.delenv("PROXBOX_AUTH_LOCKOUT_HMAC_KEY", raising=False)
    monkeypatch.setenv("PROXBOX_AUTH_LOCKOUT_HMAC_KEY_FILE", str(first_key))
    lockout_module.clear_runtime_auth_lockout_identity_key()
    _migrate_auth_lockout_schema(db_engine)
    database_path = str(db_engine.url.database)
    db_engine.dispose()

    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=2, mp_context=context) as executor:
        results = list(
            executor.map(
                _bootstrap_database_with_identity_key_in_process,
                (database_path, database_path),
                (str(first_key), str(second_key)),
            )
        )

    assert sorted(results) == ["accepted", "rejected"]


def test_bound_database_does_not_regenerate_a_lost_identity_key(
    db_engine,
    tmp_path,
    monkeypatch,
) -> None:
    key_path = tmp_path / "bound-identity.key"
    key_path.write_text("stable-identity-key-material-" + "x" * 32, encoding="utf-8")
    key_path.chmod(0o600)
    monkeypatch.delenv("PROXBOX_AUTH_LOCKOUT_HMAC_KEY", raising=False)
    monkeypatch.setenv("PROXBOX_AUTH_LOCKOUT_HMAC_KEY_FILE", str(key_path))
    lockout_module.clear_runtime_auth_lockout_identity_key()
    _migrate_auth_lockout_schema(db_engine)

    key_path.unlink()
    lockout_module.clear_runtime_auth_lockout_identity_key()
    with pytest.raises(ValueError, match="does not match the database binding"):
        _migrate_auth_lockout_schema(db_engine)

    assert not key_path.exists()


@pytest.mark.parametrize("state_kind", ["bucket", "reservation"])
def test_missing_identity_binding_rejects_existing_opaque_state(
    db_engine,
    monkeypatch,
    state_kind: str,
) -> None:
    monkeypatch.setenv("PROXBOX_AUTH_LOCKOUT_HMAC_KEY", "a" * 48)
    lockout_module.clear_runtime_auth_lockout_identity_key()
    _migrate_auth_lockout_schema(db_engine)
    identity = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    )
    policy = AuthLockoutPolicy(threshold=100, source_threshold=100)
    with Session(db_engine) as session:
        if state_kind == "bucket":
            AuthLockoutService.record_failure(session, identity, policy, now=100.0)
        else:
            assert (
                AuthLockoutService.reserve_verification(session, identity, policy, now=100.0)
                is not None
            )
        session.exec(text("DELETE FROM auth_lockout_identity_key_binding"))
        session.commit()

    monkeypatch.setenv("PROXBOX_AUTH_LOCKOUT_HMAC_KEY", "b" * 48)
    lockout_module.clear_runtime_auth_lockout_identity_key()
    with pytest.raises(DatabaseConfigurationError, match="binding is missing"):
        _migrate_auth_lockout_schema(db_engine)


@pytest.mark.parametrize("mutation", ["delete", "replace"])
def test_runtime_identity_key_stays_pinned_after_validated_sidecar_mutation(
    db_engine,
    tmp_path,
    monkeypatch,
    mutation: str,
) -> None:
    key_path = tmp_path / "runtime-bound-identity.key"
    key_path.write_text("runtime-bound-key-material-" + "x" * 32, encoding="utf-8")
    key_path.chmod(0o600)
    monkeypatch.delenv("PROXBOX_AUTH_LOCKOUT_HMAC_KEY", raising=False)
    monkeypatch.setenv("PROXBOX_AUTH_LOCKOUT_HMAC_KEY_FILE", str(key_path))
    lockout_module.clear_runtime_auth_lockout_identity_key()
    _migrate_auth_lockout_schema(db_engine)
    first = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    )

    if mutation == "delete":
        key_path.unlink()
    else:
        key_path.write_text("replacement-key-material-" + "y" * 32, encoding="utf-8")
        key_path.chmod(0o600)
    lockout_module._identity_hmac_key.cache_clear()

    second = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    )
    assert second == first


def test_forwarded_source_requires_explicit_trusted_cidr() -> None:
    trusted = parse_trusted_proxy_cidrs("10.0.0.0/8,2001:db8::/32")

    untrusted = resolve_auth_source_context("192.0.2.10", "198.51.100.5", trusted)
    trusted_forward = resolve_auth_source_context(
        "10.0.0.2",
        "198.51.100.5, 10.0.0.3",
        trusted,
    )
    loopback_default = resolve_auth_source_context("127.0.0.1", "198.51.100.7", trusted)
    malformed = resolve_auth_source_context("10.0.0.2", "not-an-ip", trusted)

    assert untrusted.source_ip == "192.0.2.10"
    assert untrusted.trust_context == "direct"
    assert trusted_forward.source_ip == "198.51.100.5"
    assert trusted_forward.trust_context == "trusted-forwarded"
    assert loopback_default.source_ip == "127.0.0.1"
    assert loopback_default.trust_context == "direct"
    assert malformed.source_ip == "10.0.0.2"
    assert malformed.trust_context == "trusted-peer-invalid-forwarding"


def test_trusted_cidr_parser_rejects_partial_or_empty_configuration() -> None:
    with pytest.raises(LockoutConfigurationError):
        parse_trusted_proxy_cidrs("10.0.0.0/8,not-a-network")
    with pytest.raises(LockoutConfigurationError):
        parse_trusted_proxy_cidrs("10.0.0.0/8,")


def test_atomic_sync_failure_updates_do_not_lose_attempts(db_engine) -> None:
    policy = AuthLockoutPolicy(threshold=100, window_seconds=60)
    identity = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    )

    def record_once() -> None:
        with Session(db_engine) as session:
            AuthLockoutService.record_failure(session, identity, policy, now=100.0)

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(record_once) for _ in range(24)]
        for future in futures:
            future.result()

    with Session(db_engine) as session:
        row = AuthLockoutService.get(session, identity)
        assert row is not None
        assert row.attempts == 24


async def test_sync_and_async_auth_share_one_atomic_bucket(db_engine) -> None:
    policy = AuthLockoutPolicy(threshold=10, window_seconds=60)
    identity = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    )
    with Session(db_engine) as session:
        ApiKey.store_key(session, VALID_KEY, label="async-shared-state")
        session.rollback()
        auth.check_auth_header_with_session(session, STALE_KEY, CLIENT_IP, policy)

    db_engine.dispose()
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    async_engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    configure_sqlite_engine(async_engine.sync_engine)
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            ok, _ = await auth.check_auth_header_with_session_async(
                session,
                STALE_KEY,
                CLIENT_IP,
                policy,
            )
            assert ok is False
            row = await AuthLockoutService.get_async(session, identity)
            assert row is not None
            assert row.attempts == 2
    finally:
        await async_engine.dispose()


async def test_atomic_async_failure_race_counts_every_attempt(db_engine) -> None:
    policy = AuthLockoutPolicy(threshold=100, window_seconds=60)
    identity = build_lockout_identity(
        resolve_auth_source_context("10.0.0.99", None, ()),
        STALE_KEY,
    )
    db_engine.dispose()
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    async_engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    configure_sqlite_engine(async_engine.sync_engine)
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def record_once() -> None:
        async with factory() as session:
            await AuthLockoutService.record_failure_async(session, identity, policy, now=100.0)

    try:
        await asyncio.gather(*(record_once() for _ in range(16)))
        async with factory() as session:
            row = await AuthLockoutService.get_async(session, identity)
            assert row is not None
            assert row.attempts == 16
    finally:
        await async_engine.dispose()


def test_metrics_are_aggregate_and_secret_safe(db_session: Session) -> None:
    policy = AuthLockoutPolicy(threshold=1, window_seconds=60)
    identity = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    )
    AuthLockoutService.record_failure(db_session, identity, policy, now=100.0)

    metrics = get_auth_lockout_metrics(db_session, now=100.0)
    prometheus = get_auth_lockout_prometheus_metrics(db_session, now=100.0)

    assert metrics == {
        "proxbox_auth_failures_total": 1,
        "proxbox_auth_lockouts_total": 1,
        "proxbox_auth_source_lockouts_total": 0,
        "proxbox_auth_recoveries_total": 0,
        "proxbox_auth_capacity_rejections_total": 0,
        "proxbox_auth_orphan_compactions_total": 0,
        "proxbox_auth_active_lockouts": 1,
        "proxbox_auth_active_source_lockouts": 0,
        "proxbox_auth_bucket_rows": 2,
        "proxbox_auth_verifications_in_flight": 0,
        "proxbox_auth_expired_orphan_reservations": 0,
    }
    assert "proxbox_auth_lockouts_total 1" in prometheus
    assert STALE_KEY not in prometheus
    assert identity.bucket_id not in prometheus
    assert "source=" not in prometheus


def test_aggregate_metrics_survive_engine_recreation(db_engine) -> None:
    policy = AuthLockoutPolicy(threshold=1, window_seconds=60)
    identity = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    )
    with Session(db_engine) as session:
        AuthLockoutService.record_failure(session, identity, policy, now=100.0)
    database_path = Path(db_engine.url.database)
    reopened = create_engine(f"sqlite:///{database_path}")
    try:
        with Session(reopened) as session:
            assert get_auth_lockout_metrics(session, now=100.0)["proxbox_auth_failures_total"] == 1
    finally:
        reopened.dispose()


async def test_async_sqlite_connections_receive_production_pragmas(tmp_path) -> None:
    target = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'async-pragmas.db'}")
    configure_sqlite_engine(target.sync_engine)
    try:
        async with target.connect() as connection:
            journal_mode = (await connection.execute(text("PRAGMA journal_mode"))).scalar_one()
            busy_timeout = (await connection.execute(text("PRAGMA busy_timeout"))).scalar_one()
        assert journal_mode == "wal"
        assert busy_timeout == 5000
    finally:
        await target.dispose()


def test_sync_runtime_waits_for_concurrent_process_write_transaction(db_engine) -> None:
    database_path = str(db_engine.url.database)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_write_transaction_in_process,
        args=(database_path, ready, release),
    )
    holder.start()
    try:
        assert ready.wait(timeout=5)
        timer = threading.Timer(0.2, release.set)
        timer.start()
        identity = build_lockout_identity(
            resolve_auth_source_context(CLIENT_IP, None, ()),
            STALE_KEY,
        )
        started = time.monotonic()
        with Session(db_engine) as session:
            result = AuthLockoutService.record_failure(
                session,
                identity,
                AuthLockoutPolicy(),
            )
        elapsed = time.monotonic() - started
        timer.join(timeout=2)
        assert result.credential.attempts == 1
        assert 0.15 <= elapsed < 5
    finally:
        release.set()
        holder.join(timeout=5)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)
    assert holder.exitcode == 0


async def test_async_runtime_waits_for_concurrent_process_write_transaction(db_engine) -> None:
    database_path = str(db_engine.url.database)
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_write_transaction_in_process,
        args=(database_path, ready, release),
    )
    holder.start()
    async_engine = create_async_engine(
        str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///"),
        connect_args={"check_same_thread": False},
    )
    configure_sqlite_engine(async_engine.sync_engine)
    sessions = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        assert ready.wait(timeout=5)
        timer = threading.Timer(0.2, release.set)
        timer.start()
        identity = build_lockout_identity(
            resolve_auth_source_context(CLIENT_IP, None, ()),
            STALE_KEY,
        )
        started = time.monotonic()
        async with sessions() as session:
            result = await AuthLockoutService.record_failure_async(
                session,
                identity,
                AuthLockoutPolicy(),
            )
        elapsed = time.monotonic() - started
        timer.join(timeout=2)
        assert result.credential.attempts == 1
        assert 0.15 <= elapsed < 5
    finally:
        release.set()
        holder.join(timeout=5)
        if holder.is_alive():
            holder.terminate()
            holder.join(timeout=5)
        await async_engine.dispose()
    assert holder.exitcode == 0


def test_legacy_ip_only_schema_remains_rollback_compatible_and_is_not_imported(
    tmp_path,
) -> None:
    target_engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with target_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE authlockout ("
                "ip_address VARCHAR PRIMARY KEY, attempts INTEGER NOT NULL, "
                "first_attempt_time FLOAT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO authlockout (ip_address, attempts, first_attempt_time) "
                "VALUES ('127.0.0.1', 5, 100.0)"
            )
        )

    _migrate_auth_lockout_schema(target_engine)

    inspector = inspect(target_engine)
    columns = {column["name"] for column in inspector.get_columns("authlockout")}
    assert columns == {"ip_address", "attempts", "first_attempt_time"}
    assert inspector.has_table("auth_lockout_buckets")
    with target_engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM authlockout")).scalar_one() == 1
        assert (
            connection.execute(text("SELECT COUNT(*) FROM auth_lockout_buckets")).scalar_one() == 0
        )


def test_legacy_migration_is_safe_across_processes(tmp_path) -> None:
    database_path = tmp_path / "multiprocess-legacy.db"
    target_engine = create_engine(f"sqlite:///{database_path}")
    configure_sqlite_engine(target_engine)
    with target_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE authlockout ("
                "ip_address VARCHAR PRIMARY KEY, attempts INTEGER NOT NULL, "
                "first_attempt_time FLOAT NOT NULL)"
            )
        )
    target_engine.dispose()

    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as executor:
        futures = [
            executor.submit(_bootstrap_database_in_process, str(database_path)) for _ in range(4)
        ]
        for future in futures:
            future.result(timeout=30)

    verified = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(verified)
        columns = {column["name"] for column in inspector.get_columns("authlockout")}
        assert columns == {"ip_address", "attempts", "first_attempt_time"}
        assert inspector.has_table("auth_lockout_buckets")
    finally:
        verified.dispose()


def test_new_lockout_schema_initialization_is_safe_across_processes(tmp_path) -> None:
    database_path = tmp_path / "multiprocess-new.db"
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as executor:
        futures = [
            executor.submit(_bootstrap_database_in_process, str(database_path)) for _ in range(4)
        ]
        for future in futures:
            future.result(timeout=30)

    verified = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(verified)
        assert inspector.has_table("auth_lockout_buckets")
        assert inspector.has_table("auth_lockout_reservations")
        assert inspector.has_table("auth_lockout_metrics")
        assert inspector.has_table("auth_lockout_identity_key_binding")
    finally:
        verified.dispose()


@pytest.mark.parametrize("with_legacy_table", [False, True])
def test_complete_database_bootstrap_is_safe_across_processes(
    tmp_path,
    with_legacy_table: bool,
) -> None:
    database_path = tmp_path / f"multiprocess-bootstrap-{with_legacy_table}.db"
    if with_legacy_table:
        target_engine = create_engine(f"sqlite:///{database_path}")
        with target_engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE authlockout ("
                    "ip_address VARCHAR PRIMARY KEY, attempts INTEGER NOT NULL, "
                    "first_attempt_time FLOAT NOT NULL)"
                )
            )
        target_engine.dispose()

    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as executor:
        futures = [
            executor.submit(_bootstrap_database_in_process, str(database_path)) for _ in range(4)
        ]
        for future in futures:
            future.result(timeout=30)

    verified = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(verified)
        assert inspector.has_table("apikey")
        assert inspector.has_table("auth_lockout_buckets")
        assert inspector.has_table("auth_lockout_reservations")
        assert inspector.has_table("auth_lockout_metrics")
        assert inspector.has_table("authlockout") is with_legacy_table
    finally:
        verified.dispose()


def test_lockout_migration_rejects_malformed_primary_key(tmp_path) -> None:
    target_engine = create_engine(f"sqlite:///{tmp_path / 'malformed-buckets.db'}")
    with target_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE auth_lockout_buckets ("
                "bucket_id VARCHAR NOT NULL, bucket_type VARCHAR NOT NULL, "
                "source_context VARCHAR NOT NULL, credential_id VARCHAR NOT NULL, "
                "attempts INTEGER NOT NULL, "
                "window_started_at FLOAT NOT NULL, locked_until FLOAT, "
                "updated_at FLOAT NOT NULL)"
            )
        )

    with pytest.raises(RuntimeError, match="primary key"):
        _migrate_auth_lockout_schema(target_engine)


def test_lockout_migration_rejects_partial_metrics_schema(tmp_path) -> None:
    target_engine = create_engine(f"sqlite:///{tmp_path / 'malformed-metrics.db'}")
    cast(Any, AuthLockout).__table__.create(target_engine)
    with target_engine.begin() as connection:
        connection.execute(text("CREATE TABLE auth_lockout_metrics (id INTEGER PRIMARY KEY)"))

    with pytest.raises(RuntimeError, match="auth_lockout_metrics columns"):
        _migrate_auth_lockout_schema(target_engine)


def test_lifespan_propagates_auth_schema_incompatibility(monkeypatch) -> None:
    from proxbox_api.app import bootstrap, factory

    def fail_schema_bootstrap() -> None:
        raise AuthLockoutSchemaError("incompatible auth_lockout_buckets primary key")

    monkeypatch.setattr(
        bootstrap,
        "initialize_database_and_schema",
        fail_schema_bootstrap,
    )
    with pytest.raises(AuthLockoutSchemaError, match="primary key"):
        with TestClient(factory.create_app()):
            pass


def test_multiprocess_failures_are_exact_and_bounded(db_engine) -> None:
    database_path = str(db_engine.url.database)
    db_engine.dispose()
    context = multiprocessing.get_context("spawn")
    workers = 4
    attempts_per_worker = 6
    max_buckets = workers * attempts_per_worker + 1

    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as executor:
        futures = [
            executor.submit(
                _record_failures_in_process,
                database_path,
                worker,
                attempts_per_worker,
                max_buckets,
            )
            for worker in range(workers)
        ]
        for future in futures:
            future.result(timeout=30)

    verified = create_engine(f"sqlite:///{database_path}")
    try:
        with Session(verified) as session:
            rows = AuthLockoutService.list_rows(session)
            expected_rows = max_buckets // 2 + 1
            assert len(rows) == expected_rows
            assert (
                session.exec(select(func.count()).select_from(AuthLockout)).one() == expected_rows
            )
            identity = build_lockout_identity(
                resolve_auth_source_context(CLIENT_IP, None, ()),
                "not-presented",
            )
            source_row = AuthLockoutService.get_source(session, identity)
            assert source_row is not None
            assert source_row.attempts == workers * attempts_per_worker
            metrics = get_auth_lockout_metrics(session, now=100.0)
            assert metrics["proxbox_auth_failures_total"] == workers * attempts_per_worker
            assert metrics["proxbox_auth_capacity_rejections_total"] == (
                workers * attempts_per_worker - max_buckets // 2
            )
    finally:
        verified.dispose()
