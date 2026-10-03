"""Fresh plugin-key checks at the actual database credential parsing boundary."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from netbox_sdk.config import Config

from proxbox_api import credentials, settings_client
from proxbox_api import plugin_key_authority as authority
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.session import proxmox_providers


class _Database:
    def __init__(self, endpoint: ProxmoxEndpoint) -> None:
        self.endpoint = endpoint

    async def exec(self, _query):
        return SimpleNamespace(all=lambda: [self.endpoint])


@pytest.fixture
def provider(monkeypatch):
    authority.invalidate_plugin_key_authority()
    credentials.reset_encryption_cache()
    monkeypatch.setattr(credentials, "_resolve_local_key_file", lambda: "")
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "synthetic-provider-root")
    password = credentials.encrypt_value("synthetic-provider-password")
    token = credentials.encrypt_value("synthetic-provider-token")
    credentials.reset_encryption_cache()
    monkeypatch.delenv("PROXBOX_ENCRYPTION_KEY")
    endpoint = ProxmoxEndpoint(
        id=17,
        name="synthetic-provider",
        ip_address="192.0.2.17",
        username="synthetic@pam",
        password=password,
        token_name="synthetic",
        token_value=token,
        verify_ssl=True,
    )
    config = Config(
        base_url="https://authority.example.test",
        token_version="v1",
        token_secret="synthetic-service-token",
    )
    facade = object()
    authority.register_plugin_key_candidate(facade, config, endpoint_id=7)
    authority.designate_default_plugin_authority(facade)
    threads: list[int] = []
    state = SimpleNamespace(allowed=True)

    def fresh_request(_candidate):
        threads.append(threading.get_ident())
        if not state.allowed:
            raise authority.PluginKeyAuthorityError()
        return "synthetic-provider-root"

    monkeypatch.setattr(authority, "_request_runtime_key", fresh_request)
    metadata = Mock(
        return_value={**settings_client.get_default_settings(), "encryption_key": "poison"}
    )
    monkeypatch.setattr(proxmox_providers, "get_settings", metadata)
    yield SimpleNamespace(endpoint=endpoint, metadata=metadata, threads=threads, state=state)
    authority.invalidate_plugin_key_authority()
    credentials.reset_encryption_cache()


@pytest.mark.asyncio
async def test_database_parser_uses_fresh_authority_not_its_bounded_metadata(provider):
    loop_thread = threading.get_ident()
    schemas = await proxmox_providers.load_proxmox_session_schemas(_Database(provider.endpoint))
    assert len(schemas) == 1
    assert schemas[0].password == "synthetic-provider-password"
    assert schemas[0].token.value == "synthetic-provider-token"
    provider.metadata.assert_called_once()
    assert len(provider.threads) == 2
    assert all(identity != loop_thread for identity in provider.threads)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_plaintext", [False, True])
async def test_database_parser_denies_revocation_before_returning_credentials(
    provider, legacy_plaintext
):
    await proxmox_providers.load_proxmox_session_schemas(_Database(provider.endpoint))
    provider.state.allowed = False
    if legacy_plaintext:
        provider.endpoint.password = "synthetic-legacy-password"
        provider.endpoint.token_value = "synthetic-legacy-token"
        provider.endpoint.timeout = 10
        provider.endpoint.max_retries = 0
        provider.endpoint.retry_backoff = 0.0
    with pytest.raises(credentials.ProxboxException) as raised:
        await proxmox_providers.load_proxmox_session_schemas(_Database(provider.endpoint))
    assert raised.value.http_status_code == 503
    assert len(provider.threads) == 3
    assert all(identity != threading.get_ident() for identity in provider.threads)


@pytest.mark.asyncio
async def test_plaintext_parser_does_not_block_the_event_loop(provider, monkeypatch):
    provider.endpoint.password = "synthetic-legacy-password"
    provider.endpoint.token_value = None
    started, permit = threading.Event(), threading.Event()

    def wait_for_authority(_candidate):
        started.set()
        assert permit.wait(timeout=3)
        return "synthetic-provider-root"

    monkeypatch.setattr(authority, "_request_runtime_key", wait_for_authority)
    parsing = asyncio.create_task(
        proxmox_providers.load_proxmox_session_schemas(_Database(provider.endpoint))
    )
    try:
        assert await asyncio.to_thread(started.wait, 3)
        assert not parsing.done()
    finally:
        permit.set()
    schemas = await asyncio.wait_for(parsing, timeout=3)
    assert schemas[0].password == "synthetic-legacy-password"
