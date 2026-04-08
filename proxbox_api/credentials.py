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
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet

from proxbox_api.logger import logger

if TYPE_CHECKING:
    pass

_ENCRYPTION_KEY: bytes | None = None
_FERNET: Fernet | None = None
_ENCRYPTION_WARNING_LOGGED: bool = False


def _get_encryption_key() -> bytes | None:
    """Get the encryption key from environment variable or ProxboxPluginSettings.

    The key is derived using SHA-256 to ensure it's exactly 32 bytes
    (Fernet requirement). Priority: env var > ProxboxPluginSettings > None.
    Returns None which signals that encryption should be skipped (dev mode).
    """
    global _ENCRYPTION_KEY
    if _ENCRYPTION_KEY is not None:
        return _ENCRYPTION_KEY

    # Check environment variable first (highest priority)
    raw_key = os.environ.get("PROXBOX_ENCRYPTION_KEY", "").strip()

    # If no env var, try to get from ProxboxPluginSettings
    if not raw_key:
        try:
            from proxbox_api.settings_client import get_settings
            settings = get_settings()
            raw_key = settings.get("encryption_key", "").strip()
        except Exception:
            # If fetching settings fails, continue with empty key
            pass

    if not raw_key:
        _ENCRYPTION_KEY = None
        return None

    _ENCRYPTION_KEY = hashlib.sha256(raw_key.encode()).digest()
    return _ENCRYPTION_KEY


def _get_fernet() -> Fernet | None:
    """Get or create the Fernet instance."""
    global _FERNET, _ENCRYPTION_WARNING_LOGGED
    if _FERNET is not None:
        return _FERNET

    key = _get_encryption_key()
    if key is None:
        if not _ENCRYPTION_WARNING_LOGGED:
            logger.warning(
                "CRITICAL: Credential encryption is DISABLED. "
                "Set PROXBOX_ENCRYPTION_KEY to encrypt credentials at rest. "
                "Without encryption, all credentials will be stored in plaintext."
            )
            _ENCRYPTION_WARNING_LOGGED = True
        _FERNET = None
        return None

    _FERNET = Fernet(base64.urlsafe_b64encode(key))
    return _FERNET


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
        logger.warning("Decryption failed for a value (may be corrupted or using wrong key): %s", e)
        return ciphertext


def generate_encryption_key() -> str:
    """Generate a new random encryption key suitable for PROXBOX_ENCRYPTION_KEY."""
    return Fernet.generate_key().decode()
