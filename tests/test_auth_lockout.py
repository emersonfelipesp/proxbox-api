"""Regression tests for credential-isolated authentication lockout."""

from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import os
import threading
import time
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
    AuthLockoutSchemaError,
    _migrate_auth_lockout_schema,
    configure_sqlite_engine,
)
from proxbox_api.services import auth_lockout as lockout_module
from proxbox_api.services.auth_lockout import (
    AuthLockoutPolicy,
    AuthLockoutService,
    LockoutCapacityError,
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


def _migrate_lockout_in_process(database_path: str) -> None:
    target_engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    configure_sqlite_engine(target_engine)
    try:
        _migrate_auth_lockout_schema(target_engine)
    finally:
        target_engine.dispose()


def _bootstrap_database_in_process(database_path: str) -> None:
    from proxbox_api import database as database_module

    database_module._legacy_default_database_candidates = tuple
    try:
        database_module.initialize_database_and_schema({"PROXBOX_DATABASE_PATH": database_path})
    finally:
        asyncio.run(database_module.dispose_database())


def _build_identity_with_generated_key_in_process(key_path: str) -> str:
    os.environ.pop("PROXBOX_AUTH_LOCKOUT_HMAC_KEY", None)
    os.environ["PROXBOX_AUTH_LOCKOUT_HMAC_KEY_FILE"] = key_path
    lockout_module._identity_hmac_key.cache_clear()
    return build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    ).bucket_id


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
    # Admission is deliberately bounded before bcrypt. Some simultaneous valid
    # requests can receive a transient lockout response, but stale-key failures
    # never poison the valid credential bucket and a retry succeeds.
    assert any(valid_results)
    assert stale_results == [False] * 4
    assert authenticate(stored_key) is True


def test_concurrent_requests_reserve_budget_before_bcrypt(
    db_engine,
    stored_key: str,
    monkeypatch,
) -> None:
    policy = AuthLockoutPolicy(threshold=1, source_threshold=100, window_seconds=60)
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
    assert sum("Too many failed" in (message or "") for _, message in results) == 7


def test_stale_finalizer_cannot_release_a_newer_window_reservation(db_session: Session) -> None:
    policy = AuthLockoutPolicy(threshold=1, source_threshold=10, window_seconds=1)
    identity = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    )

    first = AuthLockoutService.reserve_verification(db_session, identity, policy, now=100.0)
    assert first is not None
    assert AuthLockoutService.reserve_verification(db_session, identity, policy, now=101.0) is None
    with pytest.raises(LockoutCapacityError, match="still in progress"):
        AuthLockoutService.record_failure(db_session, identity, policy, now=101.0)

    AuthLockoutService.finalize_verification(
        db_session,
        identity,
        policy,
        first,
        succeeded=True,
        now=101.1,
    )
    second = AuthLockoutService.reserve_verification(db_session, identity, policy, now=101.1)
    assert second is not None

    # A duplicated/stale finalizer is token-scoped and cannot decrement the
    # reservation admitted in the newer generation.
    AuthLockoutService.finalize_verification(
        db_session,
        identity,
        policy,
        first,
        succeeded=True,
        now=101.2,
    )
    row = AuthLockoutService.get(db_session, identity)
    assert row is not None
    assert row.in_flight == 1
    assert row.reservation_tokens == second.reservation_token
    assert AuthLockoutService.reserve_verification(db_session, identity, policy, now=101.2) is None


def test_orphaned_reservation_expires_and_stale_token_cannot_release_recovery(
    db_engine,
) -> None:
    policy = AuthLockoutPolicy(threshold=1, source_threshold=10, window_seconds=1)
    identity = build_lockout_identity(
        resolve_auth_source_context(CLIENT_IP, None, ()),
        STALE_KEY,
    )
    with Session(db_engine) as session:
        abandoned = AuthLockoutService.reserve_verification(session, identity, policy, now=100.0)
        assert abandoned is not None

    with Session(db_engine) as restarted_session:
        assert (
            AuthLockoutService.reserve_verification(restarted_session, identity, policy, now=159.9)
            is None
        )
        recovered = AuthLockoutService.reserve_verification(
            restarted_session, identity, policy, now=160.0
        )
        assert recovered is not None

        AuthLockoutService.finalize_verification(
            restarted_session,
            identity,
            policy,
            abandoned,
            succeeded=True,
            now=160.1,
        )
        row = AuthLockoutService.get(restarted_session, identity)
        assert row is not None
        assert row.in_flight == 1
        assert row.reservation_tokens == recovered.reservation_token


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


def test_bucket_row_cap_retains_current_source_budget(db_session: Session) -> None:
    policy = AuthLockoutPolicy(
        threshold=100,
        source_threshold=100,
        window_seconds=60,
        max_buckets=5,
    )
    source = resolve_auth_source_context(CLIENT_IP, None, ())
    final_identity = None
    for index in range(20):
        final_identity = build_lockout_identity(source, f"rotated-key-{index}")
        AuthLockoutService.record_failure(db_session, final_identity, policy, now=100.0)

    assert final_identity is not None
    rows = AuthLockoutService.list_rows(db_session)
    assert len(rows) == policy.max_buckets
    source_row = AuthLockoutService.get_source(db_session, final_identity)
    assert source_row is not None
    assert source_row.attempts == 20


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
    assert AuthLockoutService.record_failure(db_session, second, policy, now=100.0) is not None

    assert AuthLockoutService.is_locked(db_session, first, now=100.0)
    assert len(AuthLockoutService.list_rows(db_session)) == policy.max_buckets


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
    lockout_module._identity_hmac_key.cache_clear()
    try:
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
        lockout_module._identity_hmac_key.cache_clear()


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
        "proxbox_auth_active_lockouts": 1,
        "proxbox_auth_active_source_lockouts": 0,
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
            executor.submit(_migrate_lockout_in_process, str(database_path)) for _ in range(4)
        ]
        for future in futures:
            future.result(timeout=20)

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
            executor.submit(_migrate_lockout_in_process, str(database_path)) for _ in range(4)
        ]
        for future in futures:
            future.result(timeout=20)

    verified = create_engine(f"sqlite:///{database_path}")
    try:
        inspector = inspect(verified)
        assert inspector.has_table("auth_lockout_buckets")
        assert inspector.has_table("auth_lockout_metrics")
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
                "attempts INTEGER NOT NULL, in_flight INTEGER NOT NULL, "
                "reservation_tokens VARCHAR NOT NULL, "
                "reservation_expires_at FLOAT, "
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
    max_buckets = 8

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
            assert len(rows) == max_buckets
            assert session.exec(select(func.count()).select_from(AuthLockout)).one() == max_buckets
            identity = build_lockout_identity(
                resolve_auth_source_context(CLIENT_IP, None, ()),
                "not-presented",
            )
            source_row = AuthLockoutService.get_source(session, identity)
            assert source_row is not None
            assert source_row.attempts == workers * attempts_per_worker
            metrics = get_auth_lockout_metrics(session, now=100.0)
            assert metrics["proxbox_auth_failures_total"] == workers * attempts_per_worker
    finally:
        verified.dispose()
