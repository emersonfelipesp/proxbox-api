"""Authentication utilities with database-backed API key storage.

All API keys are stored in the database using bcrypt hashing.
The first key can be registered via /auth/register-key (auth-exempt bootstrap).
Subsequent key management requires authentication via /auth/keys endpoints.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Session

from proxbox_api.database import ApiKey, AuthLockout, async_session_factory, engine, get_session

_LOCKOUT_DURATION = 300
_MAX_FAILED_ATTEMPTS = 5


async def is_locked_out_async(session: AsyncSession, ip: str) -> bool:
    return await AuthLockout.is_locked_out_async(
        session, ip, _MAX_FAILED_ATTEMPTS, _LOCKOUT_DURATION
    )


async def record_failed_attempt_async(session: AsyncSession, ip: str) -> None:
    await AuthLockout.record_failed_attempt_async(session, ip)


async def clear_failed_attempts_async(session: AsyncSession, ip: str) -> None:
    await AuthLockout.clear_failed_attempts_async(session, ip)


async def _get_attempt_count_async(session: AsyncSession, ip: str) -> int:
    lockout = await session.get(AuthLockout, ip)
    if lockout:
        return lockout.attempts
    return 0


async def check_auth_header_with_session_async(
    session: AsyncSession, api_key: str | None, client_ip: str
) -> tuple[bool, str | None]:
    """Validate API key using the provided async session.

    Returns (authorized, error_message) tuple.
    """
    if await is_locked_out_async(session, client_ip):
        return False, "Too many failed authentication attempts. Please try again later."

    if not await ApiKey.has_any_key_async(session):
        return False, (
            "No API key configured. "
            "Register a key via POST /auth/register-key or use an existing key."
        )

    if not api_key:
        await record_failed_attempt_async(session, client_ip)
        remaining = _MAX_FAILED_ATTEMPTS - await _get_attempt_count_async(session, client_ip)
        if remaining > 0:
            return False, f"API key required. {remaining} attempts remaining."
        return False, "API key required."

    if not await ApiKey.verify_any_async(session, api_key):
        await record_failed_attempt_async(session, client_ip)
        remaining = _MAX_FAILED_ATTEMPTS - await _get_attempt_count_async(session, client_ip)
        if remaining > 0:
            return False, f"Invalid API key. {remaining} attempts remaining."
        return False, "Invalid API key."

    await clear_failed_attempts_async(session, client_ip)
    return True, None


async def check_auth_header_async(api_key: str | None, client_ip: str) -> tuple[bool, str | None]:
    """Validate API key from database (using async engine).

    Returns (authorized, error_message) tuple.
    """
    async with async_session_factory() as session:
        return await check_auth_header_with_session_async(session, api_key, client_ip)


# Sync versions for backward compatibility during migration

_LOCKOUT_DURATION = 300
_MAX_FAILED_ATTEMPTS = 5


def is_locked_out(session: Session, ip: str) -> bool:
    return AuthLockout.is_locked_out(session, ip, _MAX_FAILED_ATTEMPTS, _LOCKOUT_DURATION)


def record_failed_attempt(session: Session, ip: str) -> None:
    AuthLockout.record_failed_attempt(session, ip)


def clear_failed_attempts(session: Session, ip: str) -> None:
    AuthLockout.clear_failed_attempts(session, ip)


def _get_attempt_count(session: Session, ip: str) -> int:
    lockout = session.get(AuthLockout, ip)
    if lockout:
        return lockout.attempts
    return 0


def check_auth_header_with_session(
    session: Session, api_key: str | None, client_ip: str
) -> tuple[bool, str | None]:
    """Validate API key using the provided session.

    Returns (authorized, error_message) tuple.
    """
    if is_locked_out(session, client_ip):
        return False, "Too many failed authentication attempts. Please try again later."

    if not ApiKey.has_any_key(session):
        return False, (
            "No API key configured. "
            "Register a key via POST /auth/register-key or use an existing key."
        )

    if not api_key:
        record_failed_attempt(session, client_ip)
        remaining = _MAX_FAILED_ATTEMPTS - _get_attempt_count(session, client_ip)
        if remaining > 0:
            return False, f"API key required. {remaining} attempts remaining."
        return False, "API key required."

    if not ApiKey.verify_any(session, api_key):
        record_failed_attempt(session, client_ip)
        remaining = _MAX_FAILED_ATTEMPTS - _get_attempt_count(session, client_ip)
        if remaining > 0:
            return False, f"Invalid API key. {remaining} attempts remaining."
        return False, "Invalid API key."

    clear_failed_attempts(session, client_ip)
    return True, None


def check_auth_header(api_key: str | None, client_ip: str) -> tuple[bool, str | None]:
    """Validate API key from database (using default engine).

    Returns (authorized, error_message) tuple.
    """
    with Session(engine) as session:
        return check_auth_header_with_session(session, api_key, client_ip)


def get_session_factory(app) -> Callable[[], Session]:
    """Get the session factory respecting dependency overrides.

    Returns a context manager that yields a Session.
    """
    if hasattr(app, "dependency_overrides") and get_session in app.dependency_overrides:
        original = app.dependency_overrides[get_session]
        return contextmanager(original)
    return contextmanager(get_session)


def get_async_session_factory(app) -> Callable[[], AsyncSession]:
    """Get the async session factory respecting dependency overrides.

    Returns an async context manager that yields an AsyncSession.
    """

    from proxbox_api.database import get_async_session

    if hasattr(app, "dependency_overrides") and get_async_session in app.dependency_overrides:
        return app.dependency_overrides[get_async_session]
    return get_async_session
