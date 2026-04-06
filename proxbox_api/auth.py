"""Authentication utilities with database-backed API key storage.

All API keys are stored in the database using bcrypt hashing.
The first key can be registered via /auth/register-key (auth-exempt bootstrap).
Subsequent key management requires authentication via /auth/keys endpoints.
"""

from __future__ import annotations

from sqlmodel import Session

from proxbox_api.database import ApiKey, AuthLockout, engine

_LOCKOUT_DURATION = 300
_MAX_FAILED_ATTEMPTS = 5


def is_locked_out(ip: str) -> bool:
    with Session(engine) as session:
        return AuthLockout.is_locked_out(session, ip, _MAX_FAILED_ATTEMPTS, _LOCKOUT_DURATION)


def record_failed_attempt(ip: str) -> None:
    with Session(engine) as session:
        AuthLockout.record_failed_attempt(session, ip)


def clear_failed_attempts(ip: str) -> None:
    with Session(engine) as session:
        AuthLockout.clear_failed_attempts(session, ip)


def _get_attempt_count(ip: str) -> int:
    with Session(engine) as session:
        lockout = session.get(AuthLockout, ip)
        if lockout:
            return lockout.attempts
    return 0


def check_auth_header(api_key: str | None, client_ip: str) -> tuple[bool, str | None]:
    """Validate API key from database.

    Returns (authorized, error_message) tuple.
    """
    if is_locked_out(client_ip):
        return False, "Too many failed authentication attempts. Please try again later."

    with Session(engine) as session:
        if not ApiKey.has_any_key(session):
            return False, (
                "No API key configured. "
                "Register a key via POST /auth/register-key or use an existing key."
            )

        if not api_key:
            record_failed_attempt(client_ip)
            remaining = _MAX_FAILED_ATTEMPTS - _get_attempt_count(client_ip)
            if remaining > 0:
                return False, f"API key required. {remaining} attempts remaining."
            return False, "API key required."

        if not ApiKey.verify_any(session, api_key):
            record_failed_attempt(client_ip)
            remaining = _MAX_FAILED_ATTEMPTS - _get_attempt_count(client_ip)
            if remaining > 0:
                return False, f"Invalid API key. {remaining} attempts remaining."
            return False, "Invalid API key."

    clear_failed_attempts(client_ip)
    return True, None
