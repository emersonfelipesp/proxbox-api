"""Credential encryption using Fernet (AES-128-CBC with HMAC).

Encryption is applied to sensitive fields stored in the SQLite database:
- NetBoxEndpoint.token (API token)
- NetBoxEndpoint.token_key (token key for v2)
- ProxmoxEndpoint.password
- ProxmoxEndpoint.token_value

The encryption key is derived from the PROXBOX_ENCRYPTION_KEY environment
variable. If no key is configured, credential writes are refused at the write
sink (``encrypt_value``) unless plaintext storage is explicitly opted in via
PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS — a deny-by-default guard that prevents
silently persisting secrets in plaintext.

WARNING: Running without an encryption key is insecure and should never happen
in production. Setting PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS stores all
credentials in plaintext in the database and must only be used for dev/tests.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from cryptography.fernet import Fernet

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.plugin_key_authority import (
    PluginKeyAuthorityError,
    PluginKeyBinding,
    get_fresh_plugin_key,
    reset_plugin_key_selection,
)

if TYPE_CHECKING:
    pass

KeySource = Literal["env", "plugin", "local"]

_ENCRYPTION_KEY: bytes | None = None
_FERNET: Fernet | None = None
_FERNET_KEY: bytes | None = None
_KEY_SOURCE: KeySource | None = None
_PLUGIN_KEY_BINDING: PluginKeyBinding | None = None
_KEY_EPOCH = 0
_ENCRYPTION_WARNING_LOGGED: bool = False
_KEY_LOCK = threading.Lock()
_PROCESS_SERVICE_KEY = secrets.token_bytes(32)

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


@dataclass(frozen=True, slots=True, repr=False)
class _ResolvedKey:
    raw_key: str = field(repr=False)
    source: KeySource | None
    binding: PluginKeyBinding | None = None


def _resolve_initial_key() -> _ResolvedKey:
    raw_key = os.environ.get("PROXBOX_ENCRYPTION_KEY", "").strip()
    if raw_key:
        return _ResolvedKey(raw_key, "env")
    try:
        material = get_fresh_plugin_key()
    except PluginKeyAuthorityError:
        # Initial selection may use an independently configured operator key.
        local_key = _resolve_local_key_file()
        if local_key:
            return _ResolvedKey(local_key, "local")
        raise
    if material is not None:
        return _ResolvedKey(material.raw_key, "plugin", material.binding)
    local_key = _resolve_local_key_file()
    if local_key:
        return _ResolvedKey(local_key, "local")
    return _ResolvedKey("", None)


def _publish_current_key_locked(resolved: _ResolvedKey) -> bytes | None:
    """The caller owns the key lock and has verified source-generation identity."""
    global _ENCRYPTION_KEY, _KEY_SOURCE, _PLUGIN_KEY_BINDING, _FERNET, _FERNET_KEY
    if _ENCRYPTION_KEY is not None and _KEY_SOURCE != "plugin":
        return _ENCRYPTION_KEY
    if not resolved.raw_key:
        return None
    derived = hashlib.sha256(resolved.raw_key.encode()).digest()
    if _ENCRYPTION_KEY != derived:
        _FERNET = None
        _FERNET_KEY = None
    _ENCRYPTION_KEY = derived
    _KEY_SOURCE = resolved.source
    _PLUGIN_KEY_BINDING = resolved.binding
    return _ENCRYPTION_KEY


def _key_publication_conflicts(resolved: _ResolvedKey, *, epoch: int) -> bool:
    if epoch != _KEY_EPOCH:
        return True
    return _KEY_SOURCE == "plugin" and (
        resolved.source != "plugin" or resolved.binding != _PLUGIN_KEY_BINDING
    )


def _publish_key_material(resolved: _ResolvedKey, *, epoch: int) -> bytes | None:
    """Never log, request authority, or publish a retired result under the key lock."""
    with _KEY_LOCK:
        if not _key_publication_conflicts(resolved, epoch=epoch):
            return _publish_current_key_locked(resolved)
    raise PluginKeyAuthorityError()


def _get_encryption_key() -> bytes | None:
    """Operator keys cache independently; every plugin-root use authorizes afresh."""
    with _KEY_LOCK:
        source, binding, epoch = _KEY_SOURCE, _PLUGIN_KEY_BINDING, _KEY_EPOCH
        if _ENCRYPTION_KEY is not None and source != "plugin":
            return _ENCRYPTION_KEY
    if source == "plugin":
        if binding is None:
            raise PluginKeyAuthorityError()
        material = get_fresh_plugin_key(expected_binding=binding)
        if material is None:
            raise PluginKeyAuthorityError()
        resolved = _ResolvedKey(material.raw_key, "plugin", material.binding)
    else:
        resolved = _resolve_initial_key()
    return _publish_key_material(resolved, epoch=epoch)


def _get_fernet() -> Fernet | None:
    """Authorize the key before considering a cached Fernet instance."""
    global _FERNET, _FERNET_KEY, _ENCRYPTION_WARNING_LOGGED
    key = _get_encryption_key()
    if key is None:
        with _KEY_LOCK:
            warn = not _ENCRYPTION_WARNING_LOGGED
            _ENCRYPTION_WARNING_LOGGED = True
            _FERNET = None
        if warn:
            logger.critical(
                "Credential encryption is DISABLED. "
                "Set PROXBOX_ENCRYPTION_KEY to encrypt credentials at rest. "
                "Credential writes are refused unless the lab-only plaintext opt-in is set."
            )
        return None

    with _KEY_LOCK:
        if _ENCRYPTION_KEY == key:
            if _FERNET is None or _FERNET_KEY != key:
                _FERNET = Fernet(base64.urlsafe_b64encode(key))
                _FERNET_KEY = key
            return _FERNET
    raise PluginKeyAuthorityError()


def assert_encryption_configured() -> None:
    """Log encryption status during application startup.

    Startup is no longer aborted when no key is configured: the operator can set
    one later via ``PROXBOX_ENCRYPTION_KEY``, ``ProxboxPluginSettings.encryption_key``,
    or the ``/admin/encryption/*`` endpoints. Without a source, nonempty credential
    writes are refused unless lab-only plaintext storage is explicitly enabled.
    A denied or retired plugin source raises instead of becoming a no-key state.
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


def stable_keyed_fingerprint(payload: bytes, *, purpose: str) -> str:
    """Return a stable, purpose-separated HMAC without exposing server key material.

    Safety-sensitive durable bindings use the already configured credential
    encryption key as their server-held root. Rotating that key intentionally
    invalidates outstanding bindings. Callers must fail closed when encryption
    is not configured; an unkeyed digest is not a substitute.
    """

    key = _get_encryption_key()
    if key is None:
        raise ProxboxException(
            message=(
                "Credential encryption must be configured before creating durable safety bindings."
            )
        )
    context_key = hmac.new(key, purpose.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(context_key, payload, hashlib.sha256).hexdigest()


def derive_service_signing_key(context: str) -> bytes:
    """Derive a purpose-bound HMAC key without exposing credential key material.

    Production deployments inherit the configured credential-encryption key so
    signed plans remain valid across workers. Development instances without a
    configured key use one process-local seed; their short-lived plans are
    intentionally invalidated by a restart instead of being signed by a public
    or hard-coded fallback.
    """

    root_key = _get_encryption_key() or _PROCESS_SERVICE_KEY
    return hmac.new(root_key, f"proxbox-service:{context}".encode(), hashlib.sha256).digest()


def get_encryption_source() -> KeySource | None:
    """Report selected provenance, not an unrelated fresh source resolution."""
    with _KEY_LOCK:
        source = _KEY_SOURCE
    if source is not None:
        return source
    _get_encryption_key()
    with _KEY_LOCK:
        return _KEY_SOURCE


def retire_plugin_key_material(endpoint_id: int | None = None) -> None:
    """Release derived material without turning retired plugin authority into fallback."""
    global _ENCRYPTION_KEY, _FERNET, _FERNET_KEY, _KEY_EPOCH
    with _KEY_LOCK:
        # Also reject an initial acquisition that has not published its source yet.
        _KEY_EPOCH += 1
        if _KEY_SOURCE != "plugin" or _PLUGIN_KEY_BINDING is None:
            return
        if endpoint_id is not None and _PLUGIN_KEY_BINDING.endpoint_id != endpoint_id:
            return
        _ENCRYPTION_KEY = None
        _FERNET = None
        _FERNET_KEY = None


def reset_encryption_cache() -> None:
    """Reset the in-process key + Fernet cache so the next call re-resolves."""
    global _ENCRYPTION_KEY, _FERNET, _FERNET_KEY, _ENCRYPTION_WARNING_LOGGED
    global _KEY_SOURCE, _PLUGIN_KEY_BINDING, _KEY_EPOCH
    with _KEY_LOCK:
        _ENCRYPTION_KEY = None
        _FERNET = None
        _FERNET_KEY = None
        _KEY_SOURCE = None
        _PLUGIN_KEY_BINDING = None
        _KEY_EPOCH += 1
        _ENCRYPTION_WARNING_LOGGED = False
    reset_plugin_key_selection()


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


def _require_credential_storage_allowed() -> None:
    """Refuse to persist a secret in plaintext unless plaintext storage is opted in.

    Deny-by-default guard for the credential-write sink: when no encryption key
    resolves and ``PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS`` is not set, raise instead
    of silently storing the secret as plaintext. Unlike a startup gate, this never
    aborts the process — it only blocks the specific write, so reads and the rest
    of the service keep working while encryption is unconfigured.
    """
    if is_encryption_enabled() or _allow_plaintext_credentials():
        return
    raise ProxboxException(
        message=(
            "Credential encryption is not configured, so this secret cannot be stored. "
            "Set PROXBOX_ENCRYPTION_KEY, configure the ProxboxPluginSettings "
            "'encryption_key' field, create a local key via POST /admin/encryption/key, "
            "or set PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS=1 to explicitly allow plaintext "
            "storage."
        ),
    )


def encrypt_value(plaintext: str | None) -> str | None:
    """Encrypt a plaintext string.

    Returns None if input is None. When encryption is disabled, a non-empty secret
    is only returned as plaintext if plaintext storage is explicitly opted in via
    ``PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS``; otherwise this raises ``ProxboxException``
    rather than persisting the secret unencrypted (deny-by-default at the write sink).
    Returns the encrypted value as a base64 string prefixed with 'enc:'.
    """
    if plaintext is None:
        return None

    fernet = _get_fernet()
    if fernet is None:
        if plaintext:
            _require_credential_storage_allowed()
        return plaintext

    encrypted = fernet.encrypt(plaintext.encode())
    return f"enc:{base64.urlsafe_b64encode(encrypted).decode()}"


def decrypt_value(ciphertext: str | None) -> str | None:
    """Decrypt a ciphertext string.

    Returns None for absent input. A standalone instance without a configured
    source preserves its historical passthrough behavior. A selected plugin
    source requires fresh authorization even for legacy plaintext values.
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
