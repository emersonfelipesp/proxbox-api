"""Lifecycle contracts for cached NetBox SDK clients."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass

import pytest

from proxbox_api.database import NetBoxEndpoint
from proxbox_api.session import netbox as netbox_session


@dataclass
class _ClientState:
    closed: int = 0
    fail_close: bool = False


class _FakeClient:
    states: list[_ClientState] = []

    def __init__(self, _config):
        self.state = _ClientState()
        self.states.append(self.state)

    async def close(self) -> None:
        self.state.closed += 1
        if self.state.fail_close:
            raise RuntimeError("synthetic close failure with secret-token")


class _FakeApi:
    def __init__(self, client, schema=None):
        self.client = client
        self.schema = schema


def _endpoint(endpoint_id: int, token: str = "token") -> NetBoxEndpoint:
    return NetBoxEndpoint(
        id=endpoint_id,
        name=f"netbox-{endpoint_id}",
        ip_address=f"10.0.0.{endpoint_id}",
        domain=f"netbox-{endpoint_id}.example.test",
        port=443,
        token=token,
        verify_ssl=True,
    )


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch):
    asyncio.run(netbox_session.invalidate_netbox_api_cache())
    _FakeClient.states = []
    monkeypatch.setattr(netbox_session, "NetBoxApiClient", _FakeClient)
    monkeypatch.setattr(netbox_session, "Api", _FakeApi)
    monkeypatch.setattr(netbox_session, "build_schema_index", lambda **_kwargs: None)
    yield
    asyncio.run(netbox_session.invalidate_netbox_api_cache())


@pytest.mark.asyncio
async def test_endpoint_invalidation_closes_only_detached_clients_once() -> None:
    first = netbox_session.netbox_api_from_endpoint(_endpoint(1))
    second = netbox_session.netbox_api_from_endpoint(_endpoint(2))

    await netbox_session.invalidate_netbox_api_cache(1)
    await netbox_session.invalidate_netbox_api_cache(1)

    assert first.client.state.closed == 0
    assert second.client.state.closed == 0
    assert netbox_session.netbox_api_from_endpoint(_endpoint(2)) is second

    await netbox_session.invalidate_netbox_api_cache()
    assert first.client.state.closed == 1
    assert second.client.state.closed == 1


@pytest.mark.asyncio
async def test_credential_rotation_retires_old_client_until_safe_shutdown() -> None:
    previous = netbox_session.netbox_api_from_endpoint(_endpoint(7, "old-token"))

    await netbox_session.invalidate_netbox_api_cache(7)
    replacement = netbox_session.netbox_api_from_endpoint(_endpoint(7, "new-token"))

    assert previous.client.state.closed == 0
    assert replacement is not previous
    assert replacement.client.state.closed == 0
    await netbox_session.invalidate_netbox_api_cache()
    assert previous.client.state.closed == 1
    assert replacement.client.state.closed == 1


@pytest.mark.asyncio
async def test_close_failure_is_retained_for_retry_without_disclosing_secrets() -> None:
    failing = netbox_session.netbox_api_from_endpoint(_endpoint(3))
    healthy = netbox_session.netbox_api_from_endpoint(_endpoint(4))
    failing.client.state.fail_close = True
    with pytest.raises(RuntimeError) as raised:
        await netbox_session.invalidate_netbox_api_cache()

    assert failing.client.state.closed == 1
    assert healthy.client.state.closed == 1
    assert str(raised.value) == "Failed to close retired NetBox API clients: RuntimeError"
    assert "secret-token" not in str(raised.value)

    failing.client.state.fail_close = False
    await netbox_session.invalidate_netbox_api_cache()
    assert failing.client.state.closed == 2


def test_invalidation_cannot_miss_a_client_being_constructed(monkeypatch) -> None:
    constructor_started = threading.Event()
    permit_constructor = threading.Event()

    class _BlockingClient(_FakeClient):
        def __init__(self, config):
            constructor_started.set()
            assert permit_constructor.wait(timeout=5)
            super().__init__(config)

    monkeypatch.setattr(netbox_session, "NetBoxApiClient", _BlockingClient)
    acquired: list[_FakeApi] = []
    getter = threading.Thread(
        target=lambda: acquired.append(netbox_session.netbox_api_from_endpoint(_endpoint(9)))
    )
    invalidator = threading.Thread(
        target=lambda: asyncio.run(netbox_session.invalidate_netbox_api_cache(9))
    )

    getter.start()
    assert constructor_started.wait(timeout=5)
    invalidator.start()
    permit_constructor.set()
    getter.join(timeout=5)
    invalidator.join(timeout=5)

    assert not getter.is_alive()
    assert not invalidator.is_alive()
    assert acquired[0].client.state.closed == 0
    assert netbox_session.netbox_api_from_endpoint(_endpoint(9)) is not acquired[0]
    asyncio.run(netbox_session.invalidate_netbox_api_cache())
    assert acquired[0].client.state.closed == 1


@pytest.mark.asyncio
async def test_overlapping_lifespan_owners_drain_only_after_final_release() -> None:
    netbox_session.acquire_netbox_api_cache_owner()
    netbox_session.acquire_netbox_api_cache_owner()
    cached = netbox_session.netbox_api_from_endpoint(_endpoint(11))

    await netbox_session.release_netbox_api_cache_owner()
    assert cached.client.state.closed == 0
    assert netbox_session.netbox_api_from_endpoint(_endpoint(11)) is cached

    await netbox_session.release_netbox_api_cache_owner()
    assert cached.client.state.closed == 1


@pytest.mark.asyncio
async def test_new_owner_during_final_release_cannot_lose_its_client(monkeypatch) -> None:
    close_started = asyncio.Event()
    permit_close = asyncio.Event()

    class _BlockingCloseClient(_FakeClient):
        async def close(self) -> None:
            close_started.set()
            await permit_close.wait()
            await super().close()

    monkeypatch.setattr(netbox_session, "NetBoxApiClient", _BlockingCloseClient)
    netbox_session.acquire_netbox_api_cache_owner()
    retiring = netbox_session.netbox_api_from_endpoint(_endpoint(12))
    release = asyncio.create_task(netbox_session.release_netbox_api_cache_owner())
    await close_started.wait()

    netbox_session.acquire_netbox_api_cache_owner()
    current = netbox_session.netbox_api_from_endpoint(_endpoint(13))
    permit_close.set()
    await release

    assert retiring.client.state.closed == 1
    assert current.client.state.closed == 0
    await netbox_session.release_netbox_api_cache_owner()
    assert current.client.state.closed == 1


@pytest.mark.asyncio
async def test_cancellation_finishes_all_detached_closes_before_propagating(monkeypatch) -> None:
    close_started = asyncio.Event()
    permit_close = asyncio.Event()

    class _BlockingCloseClient(_FakeClient):
        async def close(self) -> None:
            close_started.set()
            await permit_close.wait()
            await super().close()

    monkeypatch.setattr(netbox_session, "NetBoxApiClient", _BlockingCloseClient)
    first = netbox_session.netbox_api_from_endpoint(_endpoint(14))
    second = netbox_session.netbox_api_from_endpoint(_endpoint(15))
    invalidation = asyncio.create_task(netbox_session.invalidate_netbox_api_cache())
    await close_started.wait()
    invalidation.cancel()
    await asyncio.sleep(0)
    invalidation.cancel()
    permit_close.set()

    with pytest.raises(asyncio.CancelledError):
        await invalidation
    assert first.client.state.closed == 1
    assert second.client.state.closed == 1
