"""Tests for credential encryption (Fernet/AES-128-CBC via PROXBOX_ENCRYPTION_KEY)."""

from __future__ import annotations

import multiprocessing
import os
import stat
import threading

import pytest

import proxbox_api.credentials as creds_mod
from proxbox_api import plugin_key_authority
from proxbox_api.exception import ProxboxException
from proxbox_api.plugin_key_authority import AuthorizedPluginKey, PluginKeyBinding


def _recursive_settings_lookup_worker(queue: multiprocessing.Queue) -> None:
    """Prove that root acquisition does not construct a decrypting session."""
    import os

    import proxbox_api.credentials as child_creds
    from proxbox_api import settings_client
    from proxbox_api.app import netbox_session as netbox_session_mod

    child_creds.reset_encryption_cache()
    plugin_key_authority.invalidate_plugin_key_authority()
    settings_client.invalidate_settings_cache()
    os.environ.pop("PROXBOX_ENCRYPTION_KEY", None)

    calls: list[bool] = []

    def _raw_session_requiring_decryption() -> None:
        calls.append(True)
        child_creds.decrypt_value("stored-plaintext-token")
        return None

    netbox_session_mod.get_raw_netbox_session = _raw_session_requiring_decryption
    child_creds._resolve_local_key_file = lambda: ""
    queue.put((child_creds.is_encryption_enabled(), calls))


@pytest.fixture(autouse=True)
def reset_credential_globals(monkeypatch):
    """Reset source provenance and keep these tests away from operator files."""
    plugin_key_authority.invalidate_plugin_key_authority()
    creds_mod.reset_encryption_cache()
    monkeypatch.delenv("PROXBOX_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(creds_mod, "_resolve_local_key_file", lambda: "")
    yield
    plugin_key_authority.invalidate_plugin_key_authority()
    creds_mod.reset_encryption_cache()


# ---------------------------------------------------------------------------
# is_encryption_enabled
# ---------------------------------------------------------------------------


def test_is_encryption_disabled_with_no_key(monkeypatch):
    monkeypatch.delenv("PROXBOX_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(
        creds_mod,
        "get_fresh_plugin_key",
        lambda: None,
    )
    assert creds_mod.is_encryption_enabled() is False


def test_is_encryption_enabled_with_env_var(monkeypatch):
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "test-secret-key")
    assert creds_mod.is_encryption_enabled() is True


def test_is_encryption_enabled_with_fresh_plugin_key(monkeypatch):
    monkeypatch.delenv("PROXBOX_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(
        creds_mod,
        "get_fresh_plugin_key",
        lambda: AuthorizedPluginKey("from-plugin-runtime", PluginKeyBinding(7, "synthetic")),
    )
    assert creds_mod.is_encryption_enabled() is True


def test_env_var_takes_priority_over_settings_key(monkeypatch):
    """PROXBOX_ENCRYPTION_KEY env var must take priority over plugin settings."""
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "env-key")
    called = []
    monkeypatch.setattr(
        creds_mod,
        "get_fresh_plugin_key",
        lambda: called.append(True),
    )
    assert creds_mod.is_encryption_enabled() is True
    assert called == [], "env var should short-circuit plugin authority lookup"


def test_selected_key_source_does_not_acquire_key(monkeypatch):
    monkeypatch.setattr(
        creds_mod,
        "_get_encryption_key",
        lambda: pytest.fail("selected_key_source must not acquire key material"),
    )

    assert creds_mod.selected_key_source() is None


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
        creds_mod,
        "get_fresh_plugin_key",
        lambda: None,
    )
    result = creds_mod.encrypt_value("my-token")
    assert result == "my-token"
    assert not (result or "").startswith("enc:")


def test_decrypt_returns_ciphertext_when_disabled(monkeypatch):
    """When no key is configured, decrypt_value is a no-op (passthrough)."""
    monkeypatch.delenv("PROXBOX_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(
        creds_mod,
        "get_fresh_plugin_key",
        lambda: None,
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


def test_decrypt_wrong_key_raises_proxbox_exception(monkeypatch):
    """A ciphertext encrypted with key A cannot be decrypted with key B.

    decrypt_value must raise a ProxboxException so the caller surfaces the
    failure instead of silently producing 401s downstream.
    """
    import pytest

    from proxbox_api.exception import ProxboxException

    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "key-a")
    encrypted = creds_mod.encrypt_value("secret")

    creds_mod.reset_encryption_cache()

    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "key-b")

    with pytest.raises(ProxboxException, match="Credential decryption failed"):
        creds_mod.decrypt_value(encrypted)


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


def test_local_key_replacement_invalidates_another_worker_cache(monkeypatch, tmp_path):
    key_file = tmp_path / "encryption.key"
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY_FILE", str(key_file))
    monkeypatch.setattr(
        creds_mod,
        "_resolve_local_key_file",
        lambda: key_file.read_text(encoding="utf-8").strip() if key_file.exists() else "",
    )
    creds_mod.set_local_encryption_key("synthetic-retired-local-key")
    old_ciphertext = creds_mod.encrypt_value("synthetic-old-credential")
    cached_state = (
        creds_mod._ENCRYPTION_KEY,
        creds_mod._FERNET,
        creds_mod._FERNET_KEY,
        creds_mod._KEY_SOURCE,
        creds_mod._PLUGIN_KEY_BINDING,
        creds_mod._LOCAL_KEY_SIGNATURE,
    )

    creds_mod.set_local_encryption_key("synthetic-current-local-key")
    (
        creds_mod._ENCRYPTION_KEY,
        creds_mod._FERNET,
        creds_mod._FERNET_KEY,
        creds_mod._KEY_SOURCE,
        creds_mod._PLUGIN_KEY_BINDING,
        creds_mod._LOCAL_KEY_SIGNATURE,
    ) = cached_state

    new_ciphertext = creds_mod.encrypt_value("synthetic-new-credential")

    assert creds_mod._KEY_SOURCE == "local"
    assert creds_mod._ENCRYPTION_KEY != cached_state[0]
    creds_mod.reset_encryption_cache()
    assert creds_mod.decrypt_value(new_ciphertext) == "synthetic-new-credential"
    with pytest.raises(creds_mod.ProxboxException, match="Credential decryption failed"):
        creds_mod.decrypt_value(old_ciphertext)


def test_delayed_local_reader_cannot_publish_replaced_key(monkeypatch, tmp_path):
    key_file = tmp_path / "encryption.key"
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY_FILE", str(key_file))
    creds_mod.set_local_encryption_key("synthetic-retired-local-key")
    creds_mod.reset_encryption_cache()
    original_resolve = creds_mod._resolve_stable_local_key
    resolved_old = threading.Event()
    permit_publication = threading.Event()
    first_resolution = True

    def delayed_resolution():
        nonlocal first_resolution
        resolved = original_resolve()
        if first_resolution:
            first_resolution = False
            resolved_old.set()
            assert permit_publication.wait(timeout=2)
        return resolved

    monkeypatch.setattr(creds_mod, "_resolve_stable_local_key", delayed_resolution)
    result: list[str | BaseException | None] = []

    def encrypt_after_delay() -> None:
        try:
            result.append(creds_mod.encrypt_value("synthetic-current-credential"))
        except BaseException as exc:  # pragma: no cover - asserted below
            result.append(exc)

    reader = threading.Thread(target=encrypt_after_delay)
    reader.start()
    assert resolved_old.wait(timeout=2)
    replacement = tmp_path / "replacement.key"
    replacement.write_text("synthetic-current-local-key", encoding="utf-8")
    os.replace(replacement, key_file)
    permit_publication.set()
    reader.join(timeout=2)

    assert not reader.is_alive()
    assert len(result) == 1 and isinstance(result[0], str)
    creds_mod.reset_encryption_cache()
    assert creds_mod.decrypt_value(result[0]) == "synthetic-current-credential"


def test_local_key_publication_fsyncs_file_then_rename_then_directory(monkeypatch, tmp_path):
    key_file = tmp_path / "encryption.key"
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY_FILE", str(key_file))
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(descriptor: int) -> None:
        events.append(
            "directory-fsync" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file-fsync"
        )
        real_fsync(descriptor)

    def recording_replace(source, destination) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "replace", recording_replace)

    creds_mod.set_local_encryption_key("synthetic-durable-local-key")

    assert events == ["file-fsync", "replace", "directory-fsync"]


def test_local_key_delete_retries_directory_fsync_after_unlink(monkeypatch, tmp_path):
    key_file = tmp_path / "encryption.key"
    key_file.write_text("synthetic-delete-key", encoding="utf-8")
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY_FILE", str(key_file))
    calls = 0

    def fail_once(_path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic directory fsync failure")

    monkeypatch.setattr(creds_mod, "fsync_directory", fail_once)

    with pytest.raises(ProxboxException, match="Could not delete local encryption key file"):
        creds_mod.clear_local_encryption_key()

    assert not key_file.exists()
    assert creds_mod.clear_local_encryption_key() is False
    assert calls == 2


def test_stable_keyed_fingerprint_is_stable_and_domain_separated(monkeypatch):
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "binding-root-key")

    first = creds_mod.stable_keyed_fingerprint(b"endpoint-schema", purpose="ceph-v2")
    creds_mod.reset_encryption_cache()
    same = creds_mod.stable_keyed_fingerprint(b"endpoint-schema", purpose="ceph-v2")
    different_payload = creds_mod.stable_keyed_fingerprint(b"retargeted", purpose="ceph-v2")
    different_purpose = creds_mod.stable_keyed_fingerprint(
        b"endpoint-schema",
        purpose="other-authority",
    )

    assert first == same
    assert len(first) == 64
    assert first not in {different_payload, different_purpose}


def test_stable_keyed_fingerprint_fails_closed_without_server_key(monkeypatch):
    from proxbox_api.exception import ProxboxException

    monkeypatch.setattr(creds_mod, "get_fresh_plugin_key", lambda: None)

    with pytest.raises(ProxboxException, match="Credential encryption must be configured"):
        creds_mod.stable_keyed_fingerprint(b"endpoint-schema", purpose="ceph-v2")


def test_key_lookup_does_not_bootstrap_a_decrypting_session():
    """Missing plugin authority must not construct a recursive session factory."""
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    process = context.Process(
        target=_recursive_settings_lookup_worker,
        args=(queue,),
        daemon=True,
    )

    process.start()
    process.join(timeout=3)

    if process.is_alive():
        process.terminate()
        process.join(timeout=3)
        pytest.fail("encryption key lookup deadlocked during recursive settings lookup")

    assert process.exitcode == 0
    assert queue.get(timeout=1) == (False, [])
