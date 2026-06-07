"""Deny-by-default credential-write sink gate (SEC-005, redesigned).

When no encryption key is configured, ``encrypt_value`` (the single sink every
``set_encrypted_*`` model method funnels through) refuses to persist a non-empty
secret in plaintext unless ``PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS`` is explicitly
set. Unlike a startup gate this never aborts the process — it only blocks the
specific credential write, so reads and the rest of the service keep working.
"""

from __future__ import annotations

import pytest

import proxbox_api.credentials as creds_mod
from proxbox_api.database import NetBoxEndpoint, ProxmoxEndpoint
from proxbox_api.exception import ProxboxException


@pytest.fixture(autouse=True)
def reset_credential_globals():
    """Clear the module-level key/Fernet cache before and after each test."""
    creds_mod._ENCRYPTION_KEY = None
    creds_mod._FERNET = None
    creds_mod._ENCRYPTION_WARNING_LOGGED = False
    yield
    creds_mod._ENCRYPTION_KEY = None
    creds_mod._FERNET = None
    creds_mod._ENCRYPTION_WARNING_LOGGED = False


@pytest.fixture
def no_encryption_key(monkeypatch):
    """Force 'no key configured' deterministically (env, plugin settings, file)."""
    monkeypatch.delenv("PROXBOX_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(creds_mod, "_resolve_raw_key_with_source", lambda: ("", None), raising=True)


@pytest.fixture
def with_encryption_key(monkeypatch):
    """Force a resolvable key so encryption is active."""
    monkeypatch.setattr(
        creds_mod,
        "_resolve_raw_key_with_source",
        lambda: ("unit-test-secret-key", "env"),
        raising=True,
    )


def _deny_plaintext(monkeypatch):
    monkeypatch.delenv("PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS", raising=False)


def _allow_plaintext(monkeypatch):
    monkeypatch.setenv("PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS", "1")


# ---------------------------------------------------------------------------
# encrypt_value chokepoint
# ---------------------------------------------------------------------------


def test_no_key_no_optin_refuses_secret(monkeypatch, no_encryption_key):
    """A real secret cannot be stored in plaintext without an explicit opt-in."""
    _deny_plaintext(monkeypatch)
    assert creds_mod.is_encryption_enabled() is False
    with pytest.raises(ProxboxException):
        creds_mod.encrypt_value("super-secret-token")


def test_no_key_with_optin_stores_plaintext(monkeypatch, no_encryption_key):
    """The explicit opt-in keeps the legacy plaintext behavior working."""
    _allow_plaintext(monkeypatch)
    assert creds_mod.encrypt_value("super-secret-token") == "super-secret-token"


def test_no_key_none_and_empty_never_raise(monkeypatch, no_encryption_key):
    """None/empty are not secrets — they must pass even with no key and no opt-in."""
    _deny_plaintext(monkeypatch)
    assert creds_mod.encrypt_value(None) is None
    assert creds_mod.encrypt_value("") == ""


def test_with_key_encrypts_regardless_of_optin(monkeypatch, with_encryption_key):
    """When a key resolves, the value is encrypted and the guard never fires."""
    _deny_plaintext(monkeypatch)
    out = creds_mod.encrypt_value("super-secret-token")
    assert out is not None and out.startswith("enc:")
    assert creds_mod.decrypt_value(out) == "super-secret-token"


# ---------------------------------------------------------------------------
# end-to-end through the set_encrypted_* model sinks
# ---------------------------------------------------------------------------


def test_netbox_endpoint_token_sink_refused(monkeypatch, no_encryption_key):
    """NetBoxEndpoint.set_encrypted_token funnels through the guarded sink."""
    _deny_plaintext(monkeypatch)
    endpoint = NetBoxEndpoint.model_construct()
    with pytest.raises(ProxboxException):
        endpoint.set_encrypted_token("api-token-secret")


def test_proxmox_endpoint_password_sink_refused(monkeypatch, no_encryption_key):
    """ProxmoxEndpoint.set_encrypted_password is guarded too."""
    _deny_plaintext(monkeypatch)
    endpoint = ProxmoxEndpoint.model_construct()
    with pytest.raises(ProxboxException):
        endpoint.set_encrypted_password("proxmox-password")
