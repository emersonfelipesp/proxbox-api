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
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from cryptography.fernet import Fernet

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger

if TYPE_CHECKING:
    pass

_ENCRYPTION_KEY: bytes | None = None
_FERNET: Fernet | None = None
_ENCRYPTION_WARNING_LOGGED: bool = False
_KEY_LOCK = threading.Lock()

KeySource = Literal["env", "plugin", "local"]

_DEFAULT_KEY_FILE = Path(__file__).resolve().parent.parent / "data" / "encryption.key"


def _allow_plaintext_credentials() -> bool:
    return os.environ.get("PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS", "").lower() in (
        "1",
        "true",
        "yes",
    )


def _local_key_file_path() -> Path:
    override = os.environ.get("PROXBOX_ENCRYPTION_KEY_FILE", "").strip()
    return Path(override) if override else _DEFAULT_KEY_FILE


def _resolve_local_key_file() -> str:
    path = _local_key_file_path()
    try:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("Could not read local encryption key file %s: %s", path, exc)
        return ""


def _resolve_raw_key_with_source() -> tuple[str, KeySource | None]:
    raw_key = os.environ.get("PROXBOX_ENCRYPTION_KEY", "").strip()
    if raw_key:
        return raw_key, "env"
    try:
        from proxbox_api.settings_client import get_settings

        settings = get_settings()
        plugin_key = (settings.get("encryption_key") or "").strip()
        if plugin_key:
            return plugin_key, "plugin"
    except Exception as exc:
        # Don't silently swallow — surface at WARNING so misconfigured settings
        # backends are visible. Caller still falls back to local + plaintext checks.
        logger.warning("Could not load encryption_key from plugin settings: %s", exc)

    local_key = _resolve_local_key_file()
    if local_key:
        return local_key, "local"
    return "", None


def _resolve_raw_key() -> str:
    return _resolve_raw_key_with_source()[0]


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
    """Log encryption status during application startup.

    Startup is no longer aborted when no key is configured: the operator can set
    one later via ``PROXBOX_ENCRYPTION_KEY``, ``ProxboxPluginSettings.encryption_key``,
    or the ``/admin/encryption/*`` endpoints. Without a key, credentials are stored
    in plaintext and a CRITICAL log is emitted on first encryption attempt.
    """
    if _get_encryption_key() is not None:
        return
    logger.critical(
        "Credential encryption is DISABLED. Configure PROXBOX_ENCRYPTION_KEY, the "
        "ProxboxPluginSettings 'encryption_key' field, or POST /admin/encryption/key "
        "before storing sensitive credentials in production."
    )


def is_encryption_enabled() -> bool:
    """Check if credential encryption is enabled."""
    return _get_encryption_key() is not None


def get_encryption_source() -> KeySource | None:
    """Return where the active encryption key came from, or None if unset."""
    return _resolve_raw_key_with_source()[1]


def reset_encryption_cache() -> None:
    """Reset the in-process key + Fernet cache so the next call re-resolves."""
    global _ENCRYPTION_KEY, _FERNET, _ENCRYPTION_WARNING_LOGGED
    with _KEY_LOCK:
        _ENCRYPTION_KEY = None
        _FERNET = None
        _ENCRYPTION_WARNING_LOGGED = False


def set_local_encryption_key(value: str) -> Path:
    """Persist ``value`` as the local encryption key (mode 0600) and reset the cache.

    Returns the absolute path of the key file written.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        raise ProxboxException(message="Encryption key value must be a non-empty string.")

    path = _local_key_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, cleaned.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
    except OSError as exc:
        raise ProxboxException(
            message=f"Could not write local encryption key file {path}: {exc}",
            python_exception=str(exc),
        ) from exc

    reset_encryption_cache()
    try:
        from proxbox_api.settings_client import invalidate_settings_cache

        invalidate_settings_cache()
    except Exception:  # noqa: BLE001
        pass
    return path


def clear_local_encryption_key() -> bool:
    """Remove the local key file (if present) and reset the cache. Returns True if removed."""
    path = _local_key_file_path()
    removed = False
    try:
        if path.exists():
            path.unlink()
            removed = True
    except OSError as exc:
        raise ProxboxException(
            message=f"Could not delete local encryption key file {path}: {exc}",
            python_exception=str(exc),
        ) from exc

    reset_encryption_cache()
    try:
        from proxbox_api.settings_client import invalidate_settings_cache

        invalidate_settings_cache()
    except Exception:  # noqa: BLE001
        pass
    return removed


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
