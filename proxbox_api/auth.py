"""Authentication utilities with database-backed brute-force protection.

Uses bcrypt for secure API key hashing with salt and iterations.
"""

from __future__ import annotations

import os

import bcrypt
from sqlalchemy.orm import Session

from proxbox_api.database import AuthLockout, engine

_LOCKOUT_DURATION = 300
_MAX_FAILED_ATTEMPTS = 5


def _hash_api_key(raw_key: str) -> bytes:
    return bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=12))


def _verify_api_key_bcrypt(provided_key: str, stored_hash: bytes) -> bool:
    try:
        return bcrypt.checkpw(provided_key.encode(), stored_hash)
    except Exception:
        return False


def verify_api_key(provided_key: str | None) -> bool:
    raw_key = os.environ.get("PROXBOX_API_KEY", "").strip()
    if not raw_key:
        return False
    if not provided_key:
        return False
    stored_hash_bytes = os.environ.get("PROXBOX_API_KEY_HASH", "").strip()
    if stored_hash_bytes:
        try:
            return _verify_api_key_bcrypt(provided_key, stored_hash_bytes.encode())
        except Exception:
            return False
    stored_hash = bcrypt.hashpw(raw_key.encode(), bcrypt.gensalt(rounds=12))
    os.environ["PROXBOX_API_KEY_HASH"] = stored_hash.decode()
    return bcrypt.checkpw(provided_key.encode(), stored_hash)


def is_dev_mode() -> bool:
    return os.environ.get("PROXBOX_DEV_MODE", "false").lower() in ("true", "1", "yes")


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
    dev_mode = is_dev_mode()

    if is_locked_out(client_ip):
        return False, "Too many failed authentication attempts. Please try again later."

    raw_key = os.environ.get("PROXBOX_API_KEY", "").strip()

    if not raw_key:
        if dev_mode:
            return True, None
        return False, "API key not configured. Set PROXBOX_API_KEY environment variable."

    if not verify_api_key(api_key):
        record_failed_attempt(client_ip)
        remaining = _MAX_FAILED_ATTEMPTS - _get_attempt_count(client_ip)
        if remaining > 0:
            return False, f"Invalid API key. {remaining} attempts remaining."
        return False, "Invalid or missing API key."

    clear_failed_attempts(client_ip)
    return True, None
