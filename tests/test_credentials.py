"""Tests for credential encryption (Fernet/AES-128-CBC via PROXBOX_ENCRYPTION_KEY)."""

from __future__ import annotations

import pytest

import proxbox_api.credentials as creds_mod


@pytest.fixture(autouse=True)
def reset_credential_globals():
    """Reset module-level cache globals before and after every test.

    credentials.py caches the derived key and Fernet instance in module
    globals so key derivation only happens once per process. Tests that
    exercise different key scenarios must clear these between runs.
    """
    creds_mod._ENCRYPTION_KEY = None
    creds_mod._FERNET = None
    creds_mod._ENCRYPTION_WARNING_LOGGED = False
    yield
    creds_mod._ENCRYPTION_KEY = None
    creds_mod._FERNET = None
    creds_mod._ENCRYPTION_WARNING_LOGGED = False


# ---------------------------------------------------------------------------
# is_encryption_enabled
# ---------------------------------------------------------------------------


def test_is_encryption_disabled_with_no_key(monkeypatch):
    monkeypatch.delenv("PROXBOX_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(
        "proxbox_api.credentials.get_settings",
        lambda: {"encryption_key": ""},
        raising=False,
    )
    assert creds_mod.is_encryption_enabled() is False


def test_is_encryption_enabled_with_env_var(monkeypatch):
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "test-secret-key")
    assert creds_mod.is_encryption_enabled() is True


def test_is_encryption_enabled_with_settings_key(monkeypatch):
    monkeypatch.delenv("PROXBOX_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(
        "proxbox_api.settings_client.get_settings",
        lambda: {"encryption_key": "from-plugin-settings"},
        raising=False,
    )
    assert creds_mod.is_encryption_enabled() is True


def test_env_var_takes_priority_over_settings_key(monkeypatch):
    """PROXBOX_ENCRYPTION_KEY env var must take priority over plugin settings."""
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "env-key")
    called = []
    monkeypatch.setattr(
        "proxbox_api.settings_client.get_settings",
        lambda: called.append(True) or {"encryption_key": "settings-key"},
        raising=False,
    )
    assert creds_mod.is_encryption_enabled() is True
    # settings_client should not have been consulted
    assert called == [], "env var should short-circuit settings lookup"


# ---------------------------------------------------------------------------
# encrypt_value / decrypt_value round-trip
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_round_trip(monkeypatch):
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "round-trip-key")
    plaintext = "super-secret-password"

    encrypted = creds_mod.encrypt_value(plaintext)
    assert encrypted is not None
    assert encrypted.startswith("enc:"), "encrypted value must carry enc: prefix"
    assert encrypted != plaintext

    decrypted = creds_mod.decrypt_value(encrypted)
    assert decrypted == plaintext


def test_encrypt_none_returns_none(monkeypatch):
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "any-key")
    assert creds_mod.encrypt_value(None) is None


def test_decrypt_none_returns_none(monkeypatch):
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "any-key")
    assert creds_mod.decrypt_value(None) is None


def test_encrypt_returns_plaintext_when_disabled(monkeypatch):
    """When no key is configured, encrypt_value is a no-op."""
    monkeypatch.delenv("PROXBOX_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(
        "proxbox_api.settings_client.get_settings",
        lambda: {"encryption_key": ""},
        raising=False,
    )
    result = creds_mod.encrypt_value("my-token")
    assert result == "my-token"
    assert not (result or "").startswith("enc:")


def test_decrypt_returns_ciphertext_when_disabled(monkeypatch):
    """When no key is configured, decrypt_value is a no-op (passthrough)."""
    monkeypatch.delenv("PROXBOX_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(
        "proxbox_api.settings_client.get_settings",
        lambda: {"encryption_key": ""},
        raising=False,
    )
    result = creds_mod.decrypt_value("enc:some-ciphertext")
    assert result == "enc:some-ciphertext"


# ---------------------------------------------------------------------------
# decrypt_value backwards-compatibility path
# ---------------------------------------------------------------------------


def test_decrypt_plaintext_value_without_enc_prefix(monkeypatch):
    """Values stored before encryption was enabled have no enc: prefix.

    decrypt_value must return them as-is for backwards compatibility.
    """
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "new-key")
    # Old plaintext value — no enc: prefix
    assert creds_mod.decrypt_value("plaintext-token") == "plaintext-token"


# ---------------------------------------------------------------------------
# decrypt_value error handling
# ---------------------------------------------------------------------------


def test_decrypt_wrong_key_logs_warning_and_returns_ciphertext(monkeypatch):
    """A ciphertext encrypted with key A cannot be decrypted with key B.

    decrypt_value must not raise; it logs a warning and returns the raw value.
    """
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "key-a")
    encrypted = creds_mod.encrypt_value("secret")

    # Reset globals so next call uses key-b
    creds_mod._ENCRYPTION_KEY = None
    creds_mod._FERNET = None

    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "key-b")

    warnings: list[str] = []
    monkeypatch.setattr(creds_mod.logger, "warning", lambda msg, *a: warnings.append(str(msg)))

    result = creds_mod.decrypt_value(encrypted)

    assert result == encrypted  # raw ciphertext returned unchanged, not raised
    assert any("Decryption failed" in w for w in warnings)


# ---------------------------------------------------------------------------
# generate_encryption_key
# ---------------------------------------------------------------------------


def test_generate_encryption_key_returns_valid_fernet_key():
    from cryptography.fernet import Fernet

    key = creds_mod.generate_encryption_key()
    assert isinstance(key, str)
    # Fernet.generate_key() produces URL-safe base64; verify it is usable
    Fernet(key.encode())  # raises if invalid


def test_generate_encryption_key_is_unique():
    keys = {creds_mod.generate_encryption_key() for _ in range(5)}
    assert len(keys) == 5, "generated keys must be unique"


# ---------------------------------------------------------------------------
# _get_encryption_key caching behaviour
# ---------------------------------------------------------------------------


def test_encryption_key_is_cached_after_first_call(monkeypatch):
    """_get_encryption_key must cache the derived key in the module global."""
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "cached-key")
    first = creds_mod._get_encryption_key()
    # Changing the env var after the first call must not affect the cached result
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "different-key")
    second = creds_mod._get_encryption_key()
    assert first is second
