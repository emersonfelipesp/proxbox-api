"""Synthetic tests for fresh, generation-bound plugin root authorization."""

from __future__ import annotations

import asyncio
import io
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from netbox_sdk.config import Config

from proxbox_api import credentials, settings_client
from proxbox_api import plugin_key_authority as authority

_BASE_URL = "https://authority.example.test"
_RUNTIME_URL = _BASE_URL + "/api/plugins/proxbox/settings/runtime/"
_ROOT = "synthetic-plugin-root-canary"
_TOKEN = "synthetic-service-token-canary"


class _Response(io.BytesIO):
    def __init__(self, state: SimpleNamespace) -> None:
        super().__init__(state.body)
        self.status = state.status
        self.headers = {"Content-Encoding": state.encoding}
        self.url = state.url

    def geturl(self) -> str:
        return self.url


def _register(endpoint_id: int = 7, **changes: object) -> object:
    values = {"base_url": _BASE_URL, "token_version": "v1", "token_secret": _TOKEN}
    values.update(changes)
    config = Config(**values)
    facade = SimpleNamespace(client=SimpleNamespace(config=config))
    authority.register_plugin_key_candidate(facade, config, endpoint_id=endpoint_id)
    return facade


@pytest.fixture(autouse=True)
def _isolated_sources(monkeypatch):
    authority.invalidate_plugin_key_authority()
    credentials.reset_encryption_cache()
    settings_client.invalidate_settings_cache()
    monkeypatch.delenv("PROXBOX_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(credentials, "_resolve_local_key_file", lambda: "")
    yield
    authority.invalidate_plugin_key_authority()
    credentials.reset_encryption_cache()
    settings_client.invalidate_settings_cache()


@pytest.fixture
def runtime(monkeypatch):
    """Use the actual private transport and registry with an in-memory opener."""
    state = SimpleNamespace(
        body=json.dumps({"encryption_key": _ROOT}).encode(),
        status=200,
        encoding="identity",
        url=_RUNTIME_URL,
    )
    requests = []

    def open_response(request, *, timeout):
        assert timeout == 2.0
        assert request.full_url == _RUNTIME_URL
        assert request.get_header("Authorization") == f"Token {_TOKEN}"
        assert request.get_header("Accept-encoding") == "identity"
        requests.append(request)
        return _Response(state)

    opener = SimpleNamespace(open=open_response)
    monkeypatch.setattr(authority, "_authority_opener", lambda _candidate: opener)
    facade = _register()
    authority.designate_default_plugin_authority(facade)
    return SimpleNamespace(state=state, requests=requests, facade=facade)


def _sensitive_operation(operation: str, ciphertext: str | None) -> object:
    operations = {
        "decrypt": lambda: credentials.decrypt_value(ciphertext),
        "legacy-plaintext": lambda: credentials.decrypt_value("synthetic-legacy-secret"),
        "encrypt": lambda: credentials.encrypt_value("synthetic-new-secret"),
        "fingerprint": lambda: credentials.stable_keyed_fingerprint(b"payload", purpose="test"),
        "sign": lambda: credentials.derive_service_signing_key("test"),
        "key": credentials._get_encryption_key,
        "fernet": credentials._get_fernet,
    }
    return operations[operation]()


@pytest.mark.parametrize("status", [401, 403, 404, 302, 500])
@pytest.mark.parametrize(
    "operation", ["decrypt", "legacy-plaintext", "encrypt", "fingerprint", "sign", "key", "fernet"]
)
def test_warm_crypto_rechecks_runtime_denial_without_fallback(
    monkeypatch, runtime, status, operation
):
    ciphertext = credentials.encrypt_value("synthetic-old-secret")
    assert credentials._FERNET is not None
    assert credentials._ENCRYPTION_KEY is not None
    assert credentials.get_encryption_source() == "plugin"
    runtime.state.status = status
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "forbidden-late-env-root")
    monkeypatch.setenv("PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS", "1")
    monkeypatch.setattr(credentials, "_resolve_local_key_file", lambda: "forbidden-local-root")
    with settings_client.override_settings_for_current_thread({"encryption_key": "poison-root"}):
        with pytest.raises(authority.PluginKeyAuthorityError) as raised:
            _sensitive_operation(operation, ciphertext)
    assert len(runtime.requests) == 2
    assert str(raised.value) == "Plugin encryption-key authorization is unavailable."
    assert credentials.get_encryption_source() == "plugin"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not-json",
        b"[]",
        b"null",
        b'{"encryption_key": null}',
        b'{"encryption_key": 4}',
        b'{"encryption_key": ""}',
        b'{"encryption_key": "   "}',
        b'{"encryption_key": true}',
        b'{"encryption_key": "' + b"x" * 4097 + b'"}',
        b"x" * 65_537,
        b"\xff",
    ],
)
def test_warm_crypto_refuses_invalid_runtime_payload(runtime, body):
    ciphertext = credentials.encrypt_value("synthetic-old-secret")
    runtime.state.body = body
    with pytest.raises(authority.PluginKeyAuthorityError):
        credentials.decrypt_value(ciphertext)
    assert len(runtime.requests) == 2


@pytest.mark.parametrize("failure", [TimeoutError, OSError, ValueError])
def test_transport_errors_are_fixed_and_secret_free(
    monkeypatch, runtime, failure, proxbox_log_capture
):
    credentials._get_fernet()
    opener = SimpleNamespace(open=Mock(side_effect=failure(f"{_TOKEN} {_ROOT}")))
    monkeypatch.setattr(authority, "_authority_opener", lambda _candidate: opener)
    with pytest.raises(authority.PluginKeyAuthorityError) as raised:
        credentials._get_fernet()
    assert _TOKEN not in str(raised.value)
    assert _ROOT not in str(raised.value)
    messages = "\n".join(proxbox_log_capture.messages())
    assert _TOKEN not in messages
    assert _ROOT not in messages


def test_request_construction_failure_is_secret_free(monkeypatch, runtime):
    constructor = Mock(side_effect=ValueError(f"{_TOKEN} {_ROOT}"))
    monkeypatch.setattr(authority.urllib.request, "Request", constructor)
    with pytest.raises(authority.PluginKeyAuthorityError) as raised:
        authority.get_fresh_plugin_key()
    assert str(raised.value) == "Plugin encryption-key authorization is unavailable."
    assert runtime.requests == []


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br"])
def test_authority_refuses_encoded_bodies(runtime, encoding):
    runtime.state.encoding = encoding
    with pytest.raises(authority.PluginKeyAuthorityError):
        authority.get_fresh_plugin_key()


def test_authority_refuses_changed_response_url(runtime):
    runtime.state.url = "https://unapproved.example.test/"
    with pytest.raises(authority.PluginKeyAuthorityError):
        authority.get_fresh_plugin_key()


def test_redirect_handler_never_replays_authentication():
    handler = authority._RefuseRedirects()
    assert (
        handler.redirect_request(None, None, 302, "redirect", {}, "https://elsewhere.test") is None
    )


@pytest.mark.parametrize("verify", [True, False])
def test_opener_preserves_explicit_tls_policy_and_refuses_redirects(monkeypatch, verify):
    config = Config(base_url=_BASE_URL, token_version="v1", token_secret=_TOKEN, ssl_verify=verify)
    facade = object()
    authority.register_plugin_key_candidate(facade, config, endpoint_id=7)
    candidate = authority._CLIENTS[id(facade)][1]
    verified, unverified = object(), object()
    monkeypatch.setattr(authority.ssl, "create_default_context", lambda: verified)
    monkeypatch.setattr(authority.ssl, "_create_unverified_context", lambda: unverified)
    https_handler = Mock()
    builder = Mock()
    monkeypatch.setattr(authority.urllib.request, "HTTPSHandler", https_handler)
    monkeypatch.setattr(authority.urllib.request, "build_opener", builder)
    authority._authority_opener(candidate)
    https_handler.assert_called_once_with(context=verified if verify else unverified)
    assert isinstance(builder.call_args.args[0], authority._RefuseRedirects)


def test_authority_deadline_bounds_a_successful_response(monkeypatch, runtime):
    clock = Mock(side_effect=[0.0, 0.0, 0.0, 3.0])
    monkeypatch.setattr(authority.time, "monotonic", clock)
    with pytest.raises(authority.PluginKeyAuthorityError):
        authority.get_fresh_plugin_key()


def test_authority_deadline_refuses_to_start_an_expired_read(monkeypatch):
    response = Mock()
    monkeypatch.setattr(authority.time, "monotonic", lambda: 3.0)
    with pytest.raises(authority.PluginKeyAuthorityError):
        authority._read_bounded_response(response, deadline=2.0)
    response.read1.assert_not_called()


def test_metadata_overrides_and_warm_cache_never_select_a_plugin_root(monkeypatch):
    monkeypatch.setenv("PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS", "1")
    metadata = {"encryption_key": "forbidden-metadata-root", "proxmox_timeout": 17}
    monkeypatch.setattr(settings_client, "_SETTINGS_CACHE", metadata)
    monkeypatch.setattr(settings_client, "_SETTINGS_CACHE_TIME", settings_client.time.monotonic())
    with settings_client.override_settings_for_current_thread(metadata):
        assert settings_client.get_settings()["encryption_key"] == ""
        assert credentials.is_encryption_enabled() is False
    assert settings_client.get_settings()["encryption_key"] == ""
    assert settings_client._SETTINGS_CACHE["encryption_key"] == ""
    assert metadata["encryption_key"] == "forbidden-metadata-root"
    assert credentials.get_encryption_source() is None


def test_metadata_result_publication_removes_key_from_all_shared_results():
    payload = {"encryption_key": _ROOT, "proxmox_timeout": 19}
    settings_client._publish_settings_result(payload, fetched=True, cache_fallback=True)
    assert settings_client._SETTINGS_CACHE["encryption_key"] == ""
    assert settings_client._SETTINGS_LAST_RESULT["encryption_key"] == ""
    assert payload["encryption_key"] == _ROOT


def test_private_candidate_and_root_repr_do_not_contain_secrets(runtime):
    result = authority.get_fresh_plugin_key()
    candidate = authority._CLIENTS[id(runtime.facade)][1]
    for representation in [repr(candidate), repr(result), repr(result.binding)]:
        assert _TOKEN not in representation
        assert _ROOT not in representation
        assert _BASE_URL not in representation


def test_successful_plugin_key_rotation_replaces_derived_and_fernet_material(runtime):
    old_ciphertext = credentials.encrypt_value("synthetic-old-secret")
    old_key, old_fernet = credentials._ENCRYPTION_KEY, credentials._FERNET
    runtime.state.body = b'{"encryption_key": "synthetic-rotated-root"}'
    new_ciphertext = credentials.encrypt_value("synthetic-new-secret")
    assert credentials._ENCRYPTION_KEY != old_key
    assert credentials._FERNET is not old_fernet
    assert credentials.decrypt_value(new_ciphertext) == "synthetic-new-secret"
    with pytest.raises(credentials.ProxboxException, match="Credential decryption failed"):
        credentials.decrypt_value(old_ciphertext)
    assert len(runtime.requests) == 4


@pytest.mark.parametrize("endpoint_id", [7, None])
def test_retirement_clears_material_and_preserves_a_blocked_plugin_source(runtime, endpoint_id):
    credentials._get_fernet()
    binding = credentials._PLUGIN_KEY_BINDING
    authority.invalidate_plugin_key_authority(endpoint_id)
    assert credentials._ENCRYPTION_KEY is None
    assert credentials._FERNET is None
    assert credentials._FERNET_KEY is None
    assert credentials.get_encryption_source() == "plugin"
    assert credentials._PLUGIN_KEY_BINDING == binding
    with pytest.raises(authority.PluginKeyAuthorityError):
        credentials._get_fernet()
    assert len(runtime.requests) == 1


def test_other_endpoint_retirement_does_not_revoke_the_selected_source(runtime):
    first = credentials._get_fernet()
    _register(endpoint_id=9)
    authority.invalidate_plugin_key_authority(9)
    assert credentials._get_fernet() is first
    assert len(runtime.requests) == 2


@pytest.mark.parametrize("same_facade", [True, False])
def test_configuration_replacement_blocks_the_old_generation(runtime, same_facade):
    credentials._get_fernet()
    replacement = runtime.facade if same_facade else object()
    changed = Config(base_url=_BASE_URL, token_version="v1", token_secret="synthetic-rotated-token")
    authority.register_plugin_key_candidate(replacement, changed, endpoint_id=7)
    authority.designate_default_plugin_authority(replacement)
    assert credentials._ENCRYPTION_KEY is None
    with pytest.raises(authority.PluginKeyAuthorityError):
        credentials._get_fernet()
    assert len(runtime.requests) == 1


def test_identical_candidate_registration_keeps_the_current_generation(runtime):
    first = authority.get_fresh_plugin_key()
    authority.register_plugin_key_candidate(
        runtime.facade, runtime.facade.client.config, endpoint_id=7
    )
    assert authority.get_fresh_plugin_key().binding == first.binding


def test_explicit_reselection_is_required_after_retirement(runtime):
    credentials._get_fernet()
    authority.invalidate_plugin_key_authority(7)
    replacement = _register()
    authority.designate_default_plugin_authority(replacement)
    with pytest.raises(authority.PluginKeyAuthorityError):
        credentials._get_fernet()
    credentials.reset_encryption_cache()
    authority.designate_default_plugin_authority(replacement)
    assert credentials._get_fernet() is not None
    assert len(runtime.requests) == 2


def test_an_unrelated_metadata_facade_cannot_replace_default_authority(runtime):
    expected = authority.get_fresh_plugin_key().binding
    unrelated = _register(endpoint_id=9, base_url="https://metadata.example.test")
    authority.designate_default_plugin_authority(unrelated)
    assert authority.get_fresh_plugin_key(expected_binding=expected).binding == expected
    assert len(runtime.requests) == 2


def test_expected_binding_mismatch_denies_before_transport(runtime):
    mismatch = authority.PluginKeyBinding(99, "synthetic-different-generation")
    with pytest.raises(authority.PluginKeyAuthorityError):
        authority.get_fresh_plugin_key(expected_binding=mismatch)
    assert runtime.requests == []


@pytest.mark.parametrize(
    "base_url",
    [
        "",
        "file:///tmp/example",
        "https://user:pass@example.test",
        "https://example.test?query=1",
        "https://example.test#fragment",
        "https://example.test:bad",
        "https://[bad",
        "https://x\n.test",
        "https://" + "x" * 4097,
    ],
)
def test_invalid_default_configuration_denies_instead_of_selecting_plaintext(monkeypatch, base_url):
    config = SimpleNamespace(base_url=base_url)
    facade = object()
    authority.register_plugin_key_candidate(facade, config, endpoint_id=7)
    authority.designate_default_plugin_authority(facade)
    opener = Mock()
    monkeypatch.setattr(authority, "_authority_opener", opener)
    with pytest.raises(authority.PluginKeyAuthorityError):
        credentials.encrypt_value("synthetic-secret")
    opener.assert_not_called()


@pytest.mark.parametrize("authorization", [None, "", "Token x\r\nInjected: value"])
def test_invalid_authorization_is_a_blocked_default(monkeypatch, authorization):
    monkeypatch.setattr(authority, "authorization_header_value", lambda _config: authorization)
    facade = _register()
    authority.designate_default_plugin_authority(facade)
    with pytest.raises(authority.PluginKeyAuthorityError):
        authority.get_fresh_plugin_key()


def test_authorization_configuration_exception_blocks_default(monkeypatch):
    malformed = Mock(side_effect=ValueError("synthetic-malformed-authorization"))
    monkeypatch.setattr(authority, "authorization_header_value", malformed)
    facade = _register()
    authority.designate_default_plugin_authority(facade)
    with pytest.raises(authority.PluginKeyAuthorityError):
        authority.get_fresh_plugin_key()


def test_retirement_before_fernet_publication_cannot_return_a_cached_cipher(monkeypatch, runtime):
    credentials._get_fernet()
    original = credentials._get_encryption_key

    def retire_after_key_acquisition():
        key = original()
        authority.invalidate_plugin_key_authority(7)
        return key

    monkeypatch.setattr(credentials, "_get_encryption_key", retire_after_key_acquisition)
    with pytest.raises(authority.PluginKeyAuthorityError):
        credentials._get_fernet()
    assert credentials._FERNET is None


@pytest.mark.parametrize("source", ["env", "local"])
def test_independent_operator_keys_keep_their_own_cache(monkeypatch, runtime, source):
    if source == "env":
        monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "synthetic-operator-root")
    else:
        runtime.state.status = 403
        monkeypatch.setattr(
            credentials, "_resolve_local_key_file", lambda: "synthetic-operator-root"
        )
    first = credentials._get_fernet()
    assert credentials.get_encryption_source() == source
    count = len(runtime.requests)
    runtime.state.status = 403
    authority.invalidate_plugin_key_authority()
    assert credentials._get_fernet() is first
    assert len(runtime.requests) == count


def test_delayed_runtime_response_cannot_reinstall_retired_material(monkeypatch, runtime):
    started, permit = threading.Event(), threading.Event()
    results = []

    def delayed_request(_candidate):
        started.set()
        assert permit.wait(timeout=3)
        return _ROOT

    def acquire():
        try:
            credentials._get_fernet()
        except authority.PluginKeyAuthorityError:
            results.append("denied")

    monkeypatch.setattr(authority, "_request_runtime_key", delayed_request)
    worker = threading.Thread(target=acquire)
    worker.start()
    try:
        assert started.wait(timeout=3)
        authority.invalidate_plugin_key_authority(7)
    finally:
        permit.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert results == ["denied"]
    assert credentials._ENCRYPTION_KEY is None
    assert credentials._FERNET is None


@pytest.mark.parametrize("warm", [True, False])
def test_retirement_between_authorization_and_publication_denies(monkeypatch, runtime, warm):
    original = credentials.get_fresh_plugin_key

    def retire_after_success(*args, **kwargs):
        result = original(*args, **kwargs)
        authority.invalidate_plugin_key_authority(7)
        return result

    if warm:
        credentials._get_fernet()
    monkeypatch.setattr(credentials, "get_fresh_plugin_key", retire_after_success)
    with pytest.raises(authority.PluginKeyAuthorityError):
        credentials._get_fernet()
    assert credentials._ENCRYPTION_KEY is None
    assert credentials._FERNET is None


@pytest.mark.asyncio
async def test_cancelled_worker_cannot_reinstall_a_retired_result(monkeypatch, runtime):
    started, permit = threading.Event(), threading.Event()
    finished = threading.Event()

    def delayed_request(_candidate):
        started.set()
        assert permit.wait(timeout=3)
        return _ROOT

    def acquire():
        try:
            credentials._get_fernet()
        except authority.PluginKeyAuthorityError:
            pass
        finally:
            finished.set()

    monkeypatch.setattr(authority, "_request_runtime_key", delayed_request)
    task = asyncio.create_task(asyncio.to_thread(acquire))
    try:
        assert await asyncio.to_thread(started.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        authority.invalidate_plugin_key_authority(7)
    finally:
        permit.set()
        assert await asyncio.to_thread(finished.wait, 3)
    assert credentials._ENCRYPTION_KEY is None
    assert credentials._FERNET is None
