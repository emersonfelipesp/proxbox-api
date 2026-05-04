"""Credential encryption using Fernet (AES-128-CBC with HMAC).

Encryption is applied to sensitive fields stored in the SQLite database:
- NetBoxEndpoint.token (API token)
- NetBoxEndpoint.token_key (token key for v2)
- ProxmoxEndpoint.password
- ProxmoxEndpoint.token_value

The encryption key is derived from the PROXBOX_ENCRYPTION_KEY environment
variable. If not set, credentials are stored in plaintext (dev mode only).

WARNING: Running without encryption key is insecure and should never happen
in production. All credentials will be stored in plaintext in the database.
"""

from __future__ import annotations

import base64
import hashlib
import os
import threading
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger

if TYPE_CHECKING:
    pass

_ENCRYPTION_KEY: bytes | None = None
_FERNET: Fernet | None = None
_ENCRYPTION_WARNING_LOGGED: bool = False
_KEY_LOCK = threading.Lock()


def _allow_plaintext_credentials() -> bool:
    return os.environ.get("PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _resolve_raw_key() -> str:
    raw_key = os.environ.get("PROXBOX_ENCRYPTION_KEY", "").strip()
    if raw_key:
        return raw_key
    try:
        from proxbox_api.settings_client import get_settings

        settings = get_settings()
        return (settings.get("encryption_key") or "").strip()
    except Exception as exc:
        # Don't silently swallow — surface at WARNING so misconfigured settings
        # backends are visible. Caller still falls back to plaintext checks.
        logger.warning("Could not load encryption_key from plugin settings: %s", exc)
        return ""


def _get_encryption_key() -> bytes | None:
    """Get the encryption key from environment variable or ProxboxPluginSettings.

    The key is derived using SHA-256 to ensure it's exactly 32 bytes
    (Fernet requirement). Priority: env var > ProxboxPluginSettings > None.
    Returns None when no key is configured.
    """
    global _ENCRYPTION_KEY
    with _KEY_LOCK:
        if _ENCRYPTION_KEY is not None:
            return _ENCRYPTION_KEY
        raw_key = _resolve_raw_key()
        if not raw_key:
            return None
        _ENCRYPTION_KEY = hashlib.sha256(raw_key.encode()).digest()
        return _ENCRYPTION_KEY


def _get_fernet() -> Fernet | None:
    """Get or create the Fernet instance."""
    global _FERNET, _ENCRYPTION_WARNING_LOGGED
    with _KEY_LOCK:
        if _FERNET is not None:
            return _FERNET

    key = _get_encryption_key()
    if key is None:
        with _KEY_LOCK:
            if not _ENCRYPTION_WARNING_LOGGED:
                logger.critical(
                    "Credential encryption is DISABLED. "
                    "Set PROXBOX_ENCRYPTION_KEY to encrypt credentials at rest. "
                    "Without encryption, all credentials will be stored in plaintext."
                )
                _ENCRYPTION_WARNING_LOGGED = True
            _FERNET = None
        return None

    with _KEY_LOCK:
        if _FERNET is None:
            _FERNET = Fernet(base64.urlsafe_b64encode(key))
        return _FERNET


def assert_encryption_configured() -> None:
    """Refuse to start without an encryption key unless the operator explicitly opts in.

    Called once during application startup. If neither PROXBOX_ENCRYPTION_KEY nor
    plugin-settings ``encryption_key`` is set, the process aborts with a clear
    ProxboxException. Setting ``PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS=1`` opts into
    the legacy plaintext-storage path with a CRITICAL log; this path is only
    appropriate for development.
    """
    if _get_encryption_key() is not None:
        return
    if _allow_plaintext_credentials():
        logger.critical(
            "PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS is set: storing credentials in plaintext. "
            "Configure PROXBOX_ENCRYPTION_KEY before deploying to production."
        )
        return
    raise ProxboxException(
        message=(
            "Refusing to start without credential encryption. Set "
            "PROXBOX_ENCRYPTION_KEY (or the plugin settings 'encryption_key' field), "
            "or set PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS=1 to acknowledge insecure storage."
        ),
    )


def is_encryption_enabled() -> bool:
    """Check if credential encryption is enabled."""
    return _get_encryption_key() is not None


def encrypt_value(plaintext: str | None) -> str | None:
    """Encrypt a plaintext string.

    Returns None if encryption is disabled or input is None.
    Returns the encrypted value as a base64 string prefixed with 'enc:'.
    """
    if plaintext is None:
        return None

    fernet = _get_fernet()
    if fernet is None:
        return plaintext

    encrypted = fernet.encrypt(plaintext.encode())
    return f"enc:{base64.urlsafe_b64encode(encrypted).decode()}"


def decrypt_value(ciphertext: str | None) -> str | None:
    """Decrypt a ciphertext string.

    Returns None if encryption is disabled or input is None.
    Handles both encrypted ('enc:...') and plaintext values for
    backwards compatibility during migration.
    """
    if ciphertext is None:
        return None

    fernet = _get_fernet()
    if fernet is None:
        return ciphertext

    if not ciphertext.startswith("enc:"):
        return ciphertext

    try:
        encrypted = base64.urlsafe_b64decode(ciphertext[4:])
        decrypted = fernet.decrypt(encrypted)
        return decrypted.decode()
    except Exception as e:
        logger.error(
            "Decryption failed for a value (corrupted ciphertext or wrong PROXBOX_ENCRYPTION_KEY): %s",
            e,
        )
        raise ProxboxException(
            message=(
                "Credential decryption failed. The stored value is corrupted or "
                "PROXBOX_ENCRYPTION_KEY does not match the key used to encrypt it. "
                "Re-create the affected endpoint with the correct key."
            ),
            python_exception=str(e),
        )


def generate_encryption_key() -> str:
    """Generate a new random encryption key suitable for PROXBOX_ENCRYPTION_KEY."""
    return Fernet.generate_key().decode()
