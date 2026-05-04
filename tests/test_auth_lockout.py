"""Tests for proxbox_api.auth lockout state machine.

Exercises the sync helpers (`check_auth_header_with_session` plus the
`AuthLockout` plumbing) end-to-end against an isolated SQLite test database.
The async path uses the same `AuthLockout` model, so the sync coverage
locks in the shared state machine.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session

from proxbox_api import auth
from proxbox_api.database import ApiKey, AuthLockout

VALID_KEY = "valid-test-api-key-aaaaaaaaaaaaaaaaaaaaaaaa"
WRONG_KEY = "wrong-test-api-key-bbbbbbbbbbbbbbbbbbbbbbbbb"
CLIENT_IP = "10.0.0.42"


@pytest.fixture
def stored_key(db_session: Session) -> str:
    ApiKey.store_key(db_session, VALID_KEY, label="auth-lockout-test")
    return VALID_KEY


def test_valid_key_returns_authorized(db_session: Session, stored_key: str) -> None:
    ok, message = auth.check_auth_header_with_session(db_session, stored_key, CLIENT_IP)
    assert ok is True
    assert message is None


def test_missing_key_returns_remaining_attempts(db_session: Session, stored_key: str) -> None:
    ok, message = auth.check_auth_header_with_session(db_session, None, CLIENT_IP)
    assert ok is False
    assert message is not None and "attempts remaining" in message


def test_no_keys_configured_returns_specific_error(db_session: Session) -> None:
    ok, message = auth.check_auth_header_with_session(db_session, VALID_KEY, CLIENT_IP)
    assert ok is False
    assert message is not None and "No API key configured" in message


def test_failed_attempts_count_up_to_max(db_session: Session, stored_key: str) -> None:
    for _ in range(auth._MAX_FAILED_ATTEMPTS):
        ok, _ = auth.check_auth_header_with_session(db_session, WRONG_KEY, CLIENT_IP)
        assert ok is False
    # After hitting the cap the IP is locked out, so even a valid key fails.
    ok, message = auth.check_auth_header_with_session(db_session, VALID_KEY, CLIENT_IP)
    assert ok is False
    assert message is not None and "Too many failed authentication attempts" in message


def test_successful_auth_clears_failed_attempts(db_session: Session, stored_key: str) -> None:
    # Burn a few attempts but stay below the cap.
    for _ in range(auth._MAX_FAILED_ATTEMPTS - 1):
        auth.check_auth_header_with_session(db_session, WRONG_KEY, CLIENT_IP)
    assert auth._get_attempt_count(db_session, CLIENT_IP) > 0

    ok, _ = auth.check_auth_header_with_session(db_session, stored_key, CLIENT_IP)
    assert ok is True
    assert auth._get_attempt_count(db_session, CLIENT_IP) == 0


def test_lockout_expires_after_duration(
    db_session: Session, stored_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Give the failed-attempt loop room to finish (bcrypt verification is slow)
    # before the lockout window starts ticking.
    monkeypatch.setattr(auth, "_LOCKOUT_DURATION", 30)
    for _ in range(auth._MAX_FAILED_ATTEMPTS):
        auth.check_auth_header_with_session(db_session, WRONG_KEY, CLIENT_IP)
    assert auth.is_locked_out(db_session, CLIENT_IP) is True

    monkeypatch.setattr(auth, "_LOCKOUT_DURATION", 0)
    assert auth.is_locked_out(db_session, CLIENT_IP) is False


def test_clear_failed_attempts_resets_lockout(db_session: Session, stored_key: str) -> None:
    for _ in range(auth._MAX_FAILED_ATTEMPTS):
        auth.check_auth_header_with_session(db_session, WRONG_KEY, CLIENT_IP)
    assert auth.is_locked_out(db_session, CLIENT_IP) is True

    auth.clear_failed_attempts(db_session, CLIENT_IP)
    assert auth._get_attempt_count(db_session, CLIENT_IP) == 0
    assert auth.is_locked_out(db_session, CLIENT_IP) is False


async def test_async_check_auth_header_returns_authorized_for_valid_key(
    db_engine, stored_key: str
) -> None:
    """The async path shares the same AuthLockout model; a happy-path call
    against a fresh async session must succeed for the same stored key."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlmodel.ext.asyncio.session import AsyncSession

    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    async_engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            ok, message = await auth.check_auth_header_with_session_async(
                session, stored_key, "10.0.0.43"
            )
        assert ok is True
        assert message is None
    finally:
        await async_engine.dispose()


def test_attempt_counter_persists_across_sessions(db_engine, stored_key: str) -> None:
    """Failed attempts on one session must be visible from a fresh session."""
    with Session(db_engine) as session:
        auth.check_auth_header_with_session(session, WRONG_KEY, CLIENT_IP)
    with Session(db_engine) as session:
        lockout = session.get(AuthLockout, CLIENT_IP)
        assert lockout is not None
        assert lockout.attempts >= 1
