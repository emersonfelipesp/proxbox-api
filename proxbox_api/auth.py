"""Database-backed API-key authentication with credential-isolated lockout."""

from __future__ import annotations

from contextlib import (
    AbstractAsyncContextManager,
    AbstractContextManager,
    asynccontextmanager,
    contextmanager,
)
from typing import Callable

from sqlmodel import Session
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api import database
from proxbox_api.database import ApiKey, get_session
from proxbox_api.services.auth_lockout import (
    AuthLockoutPolicy,
    AuthLockoutService,
    AuthSourceContext,
    LockoutCapacityError,
    LockoutIdentity,
    build_lockout_identity,
    resolve_auth_source_context,
)

# Public compatibility constants retain the defaults; runtime policy comes from
# validated process configuration rather than these values.
_LOCKOUT_DURATION = 300
_MAX_FAILED_ATTEMPTS = 5

_LOCKED_MESSAGE = "Too many failed authentication attempts. Please try again later."
_CAPACITY_MESSAGE = "Authentication verification capacity is temporarily exhausted."
_NO_KEYS_MESSAGE = (
    "No API key configured. Register a key via POST /auth/register-key or use an existing key."
)


def _source_context(source: AuthSourceContext | str) -> AuthSourceContext:
    if isinstance(source, AuthSourceContext):
        return source
    return resolve_auth_source_context(source, None, ())


def _identity(source: AuthSourceContext | str, api_key: str | None) -> LockoutIdentity:
    return build_lockout_identity(_source_context(source), api_key)


def _policy(policy: AuthLockoutPolicy | None) -> AuthLockoutPolicy:
    return policy or AuthLockoutPolicy.from_env()


async def is_locked_out_async(
    session: AsyncSession,
    source: AuthSourceContext | str,
    api_key: str | None = None,
    policy: AuthLockoutPolicy | None = None,
) -> bool:
    return await AuthLockoutService.is_locked_async(session, _identity(source, api_key))


async def record_failed_attempt_async(
    session: AsyncSession,
    source: AuthSourceContext | str,
    api_key: str | None = None,
    policy: AuthLockoutPolicy | None = None,
) -> None:
    await AuthLockoutService.record_failure_async(
        session,
        _identity(source, api_key),
        _policy(policy),
    )


async def clear_failed_attempts_async(
    session: AsyncSession,
    source: AuthSourceContext | str,
    api_key: str | None = None,
) -> None:
    await AuthLockoutService.clear_async(session, _identity(source, api_key))


async def _get_attempt_count_async(
    session: AsyncSession,
    source: AuthSourceContext | str,
    api_key: str | None = None,
) -> int:
    row = await AuthLockoutService.get_async(session, _identity(source, api_key))
    return row.attempts if row else 0


async def check_auth_header_with_session_async(  # noqa: C901
    session: AsyncSession,
    api_key: str | None,
    client_source: AuthSourceContext | str,
    policy: AuthLockoutPolicy | None = None,
) -> tuple[bool, str | None]:
    """Validate an API key with the shared async lockout state service."""

    resolved_policy = _policy(policy)
    identity = _identity(client_source, api_key)
    if await AuthLockoutService.is_locked_async(session, identity):
        await session.rollback()
        return False, _LOCKED_MESSAGE

    if not await ApiKey.has_any_key_async(session):
        await session.rollback()
        return False, _NO_KEYS_MESSAGE

    if not api_key:
        await session.rollback()
        try:
            state = await AuthLockoutService.record_failure_async(
                session, identity, resolved_policy
            )
        except LockoutCapacityError:
            return False, _CAPACITY_MESSAGE
        remaining = state.remaining_attempts(resolved_policy)
        if remaining:
            return False, f"API key required. {remaining} attempts remaining."
        return False, "API key required."

    await session.rollback()
    reservation = await AuthLockoutService.reserve_verification_async(
        session, identity, resolved_policy
    )
    if reservation is None:
        if await AuthLockoutService.is_locked_async(session, identity):
            await session.rollback()
            return False, _LOCKED_MESSAGE
        await session.rollback()
        return False, _CAPACITY_MESSAGE

    verified = await ApiKey.verify_any_async(session, api_key)
    await session.rollback()
    finalized = await AuthLockoutService.finalize_verification_async(
        session,
        identity,
        resolved_policy,
        reservation,
        succeeded=verified,
    )
    if finalized is None:
        return False, _CAPACITY_MESSAGE
    if not verified:
        remaining = finalized.remaining_attempts(resolved_policy)
        if remaining:
            return False, f"Invalid API key. {remaining} attempts remaining."
        return False, "Invalid API key."

    return True, None


async def check_auth_header_async(
    api_key: str | None,
    client_source: AuthSourceContext | str,
    policy: AuthLockoutPolicy | None = None,
) -> tuple[bool, str | None]:
    """Validate an API key using the default async database engine."""
    async with database.get_async_sessionmaker()() as session:
        return await check_auth_header_with_session_async(session, api_key, client_source, policy)


def is_locked_out(
    session: Session,
    source: AuthSourceContext | str,
    api_key: str | None = None,
    policy: AuthLockoutPolicy | None = None,
) -> bool:
    return AuthLockoutService.is_locked(session, _identity(source, api_key))


def record_failed_attempt(
    session: Session,
    source: AuthSourceContext | str,
    api_key: str | None = None,
    policy: AuthLockoutPolicy | None = None,
) -> None:
    AuthLockoutService.record_failure(session, _identity(source, api_key), _policy(policy))


def clear_failed_attempts(
    session: Session,
    source: AuthSourceContext | str,
    api_key: str | None = None,
) -> None:
    AuthLockoutService.clear(session, _identity(source, api_key))


def _get_attempt_count(
    session: Session,
    source: AuthSourceContext | str,
    api_key: str | None = None,
) -> int:
    row = AuthLockoutService.get(session, _identity(source, api_key))
    return row.attempts if row else 0


def check_auth_header_with_session(  # noqa: C901
    session: Session,
    api_key: str | None,
    client_source: AuthSourceContext | str,
    policy: AuthLockoutPolicy | None = None,
) -> tuple[bool, str | None]:
    """Validate an API key with the shared synchronous lockout state service."""

    resolved_policy = _policy(policy)
    identity = _identity(client_source, api_key)
    if AuthLockoutService.is_locked(session, identity):
        session.rollback()
        return False, _LOCKED_MESSAGE

    if not ApiKey.has_any_key(session):
        session.rollback()
        return False, _NO_KEYS_MESSAGE

    if not api_key:
        session.rollback()
        try:
            state = AuthLockoutService.record_failure(session, identity, resolved_policy)
        except LockoutCapacityError:
            return False, _CAPACITY_MESSAGE
        remaining = state.remaining_attempts(resolved_policy)
        if remaining:
            return False, f"API key required. {remaining} attempts remaining."
        return False, "API key required."

    session.rollback()
    reservation = AuthLockoutService.reserve_verification(session, identity, resolved_policy)
    if reservation is None:
        if AuthLockoutService.is_locked(session, identity):
            session.rollback()
            return False, _LOCKED_MESSAGE
        session.rollback()
        return False, _CAPACITY_MESSAGE

    verified = ApiKey.verify_any(session, api_key)
    session.rollback()
    finalized = AuthLockoutService.finalize_verification(
        session,
        identity,
        resolved_policy,
        reservation,
        succeeded=verified,
    )
    if finalized is None:
        return False, _CAPACITY_MESSAGE
    if not verified:
        remaining = finalized.remaining_attempts(resolved_policy)
        if remaining:
            return False, f"Invalid API key. {remaining} attempts remaining."
        return False, "Invalid API key."

    return True, None


def check_auth_header(
    api_key: str | None,
    client_source: AuthSourceContext | str,
    policy: AuthLockoutPolicy | None = None,
) -> tuple[bool, str | None]:
    """Validate an API key using the default synchronous database engine."""
    with Session(database.get_engine()) as session:
        return check_auth_header_with_session(session, api_key, client_source, policy)


def get_session_factory(app) -> Callable[[], AbstractContextManager[Session]]:
    """Get the session factory while respecting FastAPI dependency overrides."""

    if hasattr(app, "dependency_overrides") and get_session in app.dependency_overrides:
        original = app.dependency_overrides[get_session]
        return contextmanager(original)
    return contextmanager(get_session)


def get_async_session_factory(app) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """Get the async session factory while respecting dependency overrides."""

    from proxbox_api.database import get_async_session

    if hasattr(app, "dependency_overrides") and get_async_session in app.dependency_overrides:
        original = app.dependency_overrides[get_async_session]
        return asynccontextmanager(original)
    return asynccontextmanager(get_async_session)
