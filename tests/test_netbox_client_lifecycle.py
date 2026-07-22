"""Deterministic lifecycle tests for cached async NetBox clients."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from sqlmodel import Session

from proxbox_api.app import factory
from proxbox_api.app.netbox_session import get_raw_netbox_session
from proxbox_api.database import NetBoxEndpoint
from proxbox_api.exception import ProxboxException
from proxbox_api.session import netbox as netbox_session_module


def _endpoint(endpoint_id: int, token: str) -> NetBoxEndpoint:
    return NetBoxEndpoint(
        id=endpoint_id,
        name=f"netbox-{endpoint_id}",
        ip_address=f"10.0.0.{endpoint_id}",
        domain="",
        port=8000,
        token_version="v1",
        token=token,
        verify_ssl=False,
    )


@pytest.fixture
def tracked_clients(monkeypatch):
    clients = []

    class TrackedClient:
        def __init__(self, config):
            self.config = config
            self.close_calls = 0
            self.close_started: asyncio.Event | None = None
            self.close_release: asyncio.Event | None = None
            self.close_error: Exception | None = None
            clients.append(self)

        async def close(self) -> None:
            self.close_calls += 1
            if self.close_started is not None:
                self.close_started.set()
            if self.close_release is not None:
                await self.close_release.wait()
            if self.close_error is not None:
                raise self.close_error

    class TrackedApi:
        def __init__(self, client, schema=None):
            self.client = client
            self.schema = schema

    monkeypatch.setattr(
        netbox_session_module, "_API_CACHE", netbox_session_module._NetBoxApiLifecycle()
    )
    monkeypatch.setattr(netbox_session_module, "NetBoxApiClient", TrackedClient)
    monkeypatch.setattr(netbox_session_module, "Api", TrackedApi)
    monkeypatch.setattr(netbox_session_module, "build_schema_index", lambda **kwargs: None)
    return clients


@pytest.mark.asyncio
async def test_rotation_and_repeated_invalidation_close_each_client_exactly_once(
    tracked_clients,
) -> None:
    endpoint = _endpoint(1, "old-token")
    old_api = await netbox_session_module.netbox_api_from_endpoint(endpoint)

    endpoint.token = "new-token"
    new_api = await netbox_session_module.netbox_api_from_endpoint(endpoint)

    assert new_api is not old_api
    assert old_api.client.close_calls == 1
    assert await netbox_session_module.netbox_api_from_endpoint(endpoint) is new_api
    assert list(netbox_session_module._API_CACHE._entries) == [1]

    await netbox_session_module.invalidate_netbox_api_cache(1)
    await netbox_session_module.invalidate_netbox_api_cache(1)

    assert new_api.client.close_calls == 1
    assert len(tracked_clients) == 2


@pytest.mark.asyncio
async def test_fingerprint_frames_token_fields_without_delimiter_collisions(
    tracked_clients,
) -> None:
    endpoint = _endpoint(12, "c")
    endpoint.token_version = "v2"
    endpoint.token_key = "a|b"
    old_api = await netbox_session_module.netbox_api_from_endpoint(endpoint)

    endpoint.token_key = "a"
    endpoint.token = "b|c"
    new_api = await netbox_session_module.netbox_api_from_endpoint(endpoint)

    assert new_api is not old_api
    assert old_api.client.close_calls == 1


@pytest.mark.asyncio
async def test_fingerprint_preserves_exact_timeout_identity(tracked_clients) -> None:
    endpoint = _endpoint(15, "token")
    endpoint_api = await netbox_session_module.netbox_api_from_endpoint(endpoint)
    original_timeout = endpoint_api.client.config.timeout

    endpoint_api.client.config.timeout = 1.0001
    first = netbox_session_module._config_fingerprint(endpoint_api.client.config, False)
    endpoint_api.client.config.timeout = 1.0004
    second = netbox_session_module._config_fingerprint(endpoint_api.client.config, False)
    endpoint_api.client.config.timeout = original_timeout

    assert first != second


@pytest.mark.asyncio
async def test_transient_endpoint_is_rejected_before_client_creation(tracked_clients) -> None:
    endpoint = _endpoint(16, "transient-token")
    endpoint.id = None

    with pytest.raises(ProxboxException, match="must be persisted"):
        await netbox_session_module.netbox_api_from_endpoint(endpoint)

    assert tracked_clients == []


@pytest.mark.asyncio
async def test_endpoint_invalidation_is_scoped_and_close_runs_outside_lock(
    tracked_clients,
) -> None:
    api_a = await netbox_session_module.netbox_api_from_endpoint(_endpoint(1, "token-a"))
    api_b = await netbox_session_module.netbox_api_from_endpoint(_endpoint(2, "token-b"))
    api_a.client.close_started = asyncio.Event()
    api_a.client.close_release = asyncio.Event()

    invalidate_a = asyncio.create_task(netbox_session_module.invalidate_netbox_api_cache(1))
    await asyncio.wait_for(api_a.client.close_started.wait(), timeout=1)

    await asyncio.wait_for(
        netbox_session_module.invalidate_netbox_api_cache(2),
        timeout=1,
    )
    assert api_b.client.close_calls == 1

    api_a.client.close_release.set()
    await invalidate_a
    assert api_a.client.close_calls == 1


@pytest.mark.asyncio
async def test_repeated_invalidation_joins_pending_retirement(tracked_clients) -> None:
    api = await netbox_session_module.netbox_api_from_endpoint(_endpoint(20, "token"))
    api.client.close_started = asyncio.Event()
    api.client.close_release = asyncio.Event()

    first = asyncio.create_task(netbox_session_module.invalidate_netbox_api_cache(20))
    await asyncio.wait_for(api.client.close_started.wait(), timeout=0.2)
    second = asyncio.create_task(netbox_session_module.invalidate_netbox_api_cache(20))
    await asyncio.sleep(0)

    assert second.done() is False
    api.client.close_release.set()
    first_revision, second_revision = await asyncio.gather(first, second)

    assert second_revision > first_revision
    assert api.client.close_calls == 1
    assert netbox_session_module._API_CACHE._pending == {}


@pytest.mark.asyncio
async def test_concurrent_rotation_never_returns_client_retired_by_invalidation(
    tracked_clients,
) -> None:
    endpoint = _endpoint(1, "old-token")
    old_api = await netbox_session_module.netbox_api_from_endpoint(endpoint)
    old_api.client.close_started = asyncio.Event()
    old_api.client.close_release = asyncio.Event()

    endpoint.token = "rotating-token"
    revision = netbox_session_module._API_CACHE.revision()
    rotating = asyncio.create_task(
        netbox_session_module.netbox_api_from_endpoint(
            endpoint,
            expected_revision=revision,
        )
    )
    await asyncio.wait_for(old_api.client.close_started.wait(), timeout=1)

    invalidating = asyncio.create_task(netbox_session_module.invalidate_netbox_api_cache(1))
    await asyncio.sleep(0)
    assert invalidating.done() is False
    retired_candidate = tracked_clients[-1]
    old_api.client.close_release.set()
    await invalidating

    with pytest.raises(netbox_session_module._NetBoxApiCacheChanged):
        await rotating

    assert retired_candidate.close_calls == 1
    current = await netbox_session_module.netbox_api_from_endpoint(endpoint)
    assert current.client is not retired_candidate
    await netbox_session_module.close_netbox_api_cache()


@pytest.mark.asyncio
async def test_cancelled_invalidation_finishes_close_before_propagating(
    tracked_clients,
) -> None:
    api = await netbox_session_module.netbox_api_from_endpoint(_endpoint(3, "token"))
    api.client.close_started = asyncio.Event()
    api.client.close_release = asyncio.Event()

    invalidation = asyncio.create_task(netbox_session_module.invalidate_netbox_api_cache(3))
    await asyncio.wait_for(api.client.close_started.wait(), timeout=1)
    invalidation.cancel()
    await asyncio.sleep(0)

    assert invalidation.done() is False
    api.client.close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await invalidation

    assert api.client.close_calls == 1
    await netbox_session_module.invalidate_netbox_api_cache(3)
    assert api.client.close_calls == 1


@pytest.mark.asyncio
async def test_close_timeout_bounds_invalidation_and_shutdown(
    tracked_clients,
    monkeypatch,
) -> None:
    monkeypatch.setattr(netbox_session_module, "_NETBOX_CLIENT_CLOSE_TIMEOUT", 0.01)
    invalidated = await netbox_session_module.netbox_api_from_endpoint(
        _endpoint(17, "invalidate-token")
    )
    invalidated.client.close_release = asyncio.Event()

    await asyncio.wait_for(
        netbox_session_module.invalidate_netbox_api_cache(17),
        timeout=0.2,
    )

    shutdown_client = await netbox_session_module.netbox_api_from_endpoint(
        _endpoint(18, "shutdown-token")
    )
    shutdown_client.client.close_release = asyncio.Event()
    await asyncio.wait_for(netbox_session_module.close_netbox_api_cache(), timeout=0.2)

    assert invalidated.client.close_calls == 1
    assert shutdown_client.client.close_calls == 1
    assert netbox_session_module._API_CACHE._state == "closed"


@pytest.mark.asyncio
async def test_cancelled_invalidation_propagates_after_close_timeout(
    tracked_clients,
    monkeypatch,
) -> None:
    monkeypatch.setattr(netbox_session_module, "_NETBOX_CLIENT_CLOSE_TIMEOUT", 0.01)
    api = await netbox_session_module.netbox_api_from_endpoint(
        _endpoint(19, "cancel-timeout-token")
    )
    api.client.close_started = asyncio.Event()
    api.client.close_release = asyncio.Event()

    invalidation = asyncio.create_task(netbox_session_module.invalidate_netbox_api_cache(19))
    await asyncio.wait_for(api.client.close_started.wait(), timeout=0.2)
    invalidation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(invalidation, timeout=0.2)

    assert api.client.close_calls == 1
    assert netbox_session_module._API_CACHE._pending == {}


@pytest.mark.asyncio
async def test_terminal_shutdown_blocks_rotation_retry_and_drains_pending_clients(
    tracked_clients,
) -> None:
    endpoint = _endpoint(4, "old-token")
    old_api = await netbox_session_module.netbox_api_from_endpoint(endpoint)
    old_api.client.close_started = asyncio.Event()
    old_api.client.close_release = asyncio.Event()

    endpoint.token = "new-token"
    rotating = asyncio.create_task(netbox_session_module.netbox_api_from_endpoint(endpoint))
    await asyncio.wait_for(old_api.client.close_started.wait(), timeout=1)
    candidate = tracked_clients[-1]

    shutdown = asyncio.create_task(netbox_session_module.close_netbox_api_cache())
    await asyncio.sleep(0)
    with pytest.raises(netbox_session_module._NetBoxApiLifecycleClosed):
        await netbox_session_module.netbox_api_from_endpoint(endpoint)

    old_api.client.close_release.set()
    await shutdown
    with pytest.raises(netbox_session_module._NetBoxApiCacheChanged):
        await rotating

    assert old_api.client.close_calls == 1
    assert candidate.close_calls == 1


@pytest.mark.asyncio
async def test_provider_refreshes_a_stale_sqlalchemy_identity_map(
    tracked_clients,
    db_engine,
) -> None:
    with Session(db_engine) as stale_session:
        endpoint = _endpoint(5, "old-token")
        endpoint.id = None
        stale_session.add(endpoint)
        stale_session.commit()
        stale_session.refresh(endpoint)
        assert endpoint.id is not None

        old_api = await netbox_session_module.get_netbox_session(
            stale_session,
            endpoint.id,
        )

        with Session(db_engine) as writer_session:
            current = writer_session.get(NetBoxEndpoint, endpoint.id)
            assert current is not None
            current.token = "new-token"
            writer_session.add(current)
            writer_session.commit()

        await netbox_session_module.invalidate_netbox_api_cache(endpoint.id)
        refreshed = await netbox_session_module.get_netbox_session(
            stale_session,
            endpoint.id,
        )

    assert old_api.client.close_calls == 1
    assert refreshed is not old_api
    assert refreshed.client.config.token_secret == "new-token"


@pytest.mark.asyncio
async def test_canonical_provider_uses_async_database_session(
    tracked_clients,
) -> None:
    endpoint = _endpoint(11, "async-token")

    class AsyncResult:
        def first(self) -> NetBoxEndpoint:
            return endpoint

    class AsyncDatabaseSession:
        async def exec(self, statement) -> AsyncResult:
            return AsyncResult()

    api = await netbox_session_module.get_netbox_session(AsyncDatabaseSession())

    assert api.client.config.token_secret == "async-token"


@pytest.mark.asyncio
async def test_explicit_disabled_endpoint_is_rejected(tracked_clients) -> None:
    endpoint = _endpoint(13, "disabled-token")
    endpoint.enabled = False

    class DisabledDatabaseSession:
        async def get(self, model, endpoint_id, **kwargs) -> NetBoxEndpoint:
            assert model is NetBoxEndpoint
            assert endpoint_id == endpoint.id
            return endpoint

    with pytest.raises(ProxboxException, match="is disabled"):
        await netbox_session_module.get_netbox_session(
            DisabledDatabaseSession(),
            endpoint.id,
        )

    assert tracked_clients == []


@pytest.mark.asyncio
async def test_unexpected_provider_error_suppresses_sensitive_message(
    tracked_clients,
) -> None:
    secret = "provider-error-secret-canary"

    class FailingDatabaseSession:
        async def get(self, model, endpoint_id, **kwargs):
            raise RuntimeError(secret)

    with pytest.raises(ProxboxException) as captured:
        await netbox_session_module.get_netbox_session(FailingDatabaseSession(), 14)

    error = captured.value
    assert error.message == "Error establishing NetBox API session"
    assert error.python_exception == "RuntimeError: details suppressed"
    assert secret not in str(error)
    assert error.__context__ is None
    assert error.__cause__ is None
    assert tracked_clients == []


@pytest.mark.asyncio
async def test_default_refresh_republishes_lifecycle_owned_client(tracked_clients) -> None:
    endpoint = _endpoint(6, "old-token")
    old_api = await netbox_session_module.netbox_api_from_endpoint(endpoint)
    netbox_session_module._publish_default_netbox_api(old_api)

    endpoint.token = "new-token"
    revision = await netbox_session_module.invalidate_netbox_api_cache(endpoint.id)
    assert get_raw_netbox_session() is None

    refreshed = await netbox_session_module.refresh_default_netbox_api(
        endpoint,
        expected_revision=revision,
    )

    assert refreshed is True
    assert get_raw_netbox_session() is not old_api
    assert get_raw_netbox_session() is not None


@pytest.mark.asyncio
async def test_real_sdk_session_is_closed_by_shutdown(monkeypatch) -> None:
    monkeypatch.setattr(
        netbox_session_module,
        "_API_CACHE",
        netbox_session_module._NetBoxApiLifecycle(),
    )
    monkeypatch.setattr(netbox_session_module, "build_schema_index", lambda **kwargs: None)

    api = await netbox_session_module.netbox_api_from_endpoint(_endpoint(10, "real-token"))
    session = await api.client._get_session()
    assert api.client.session_active is True

    await netbox_session_module.close_netbox_api_cache()

    assert session.closed is True
    assert api.client.session_active is False


@pytest.mark.asyncio
async def test_close_failure_logging_omits_exception_message_and_credentials(
    tracked_clients,
    monkeypatch,
) -> None:
    secret = "credential-that-must-not-reach-logs"
    api = await netbox_session_module.netbox_api_from_endpoint(_endpoint(7, secret))
    api.client.close_error = RuntimeError(secret)
    messages: list[str] = []

    def capture_warning(message, *args, **kwargs) -> None:
        messages.append(message % args)

    monkeypatch.setattr(netbox_session_module.logger, "warning", capture_warning)
    await netbox_session_module.invalidate_netbox_api_cache(7)

    logged = "\n".join(messages)
    fingerprint = netbox_session_module._config_fingerprint(api.client.config, False)
    assert api.client.close_calls == 1
    assert secret not in logged
    assert api.client.config.base_url not in logged
    assert fingerprint not in logged
    assert "RuntimeError" in logged


@pytest.mark.asyncio
async def test_lifespan_drains_client_when_startup_fails(monkeypatch, tracked_clients) -> None:
    async def failing_bootstrap() -> None:
        await netbox_session_module.netbox_api_from_endpoint(_endpoint(8, "startup-token"))
        raise RuntimeError("startup failed")

    monkeypatch.setattr(factory.bootstrap, "init_database_and_netbox", failing_bootstrap)

    with pytest.raises(RuntimeError, match="startup failed"):
        async with factory._lifespan(FastAPI()):
            pass

    assert tracked_clients[0].close_calls == 1


@pytest.mark.asyncio
async def test_lifespan_drains_client_when_request_scope_fails(
    monkeypatch, tracked_clients
) -> None:
    async def successful_bootstrap() -> None:
        await netbox_session_module.netbox_api_from_endpoint(_endpoint(9, "request-token"))

    async def skip_bootstrap_pass(app: FastAPI) -> None:
        return None

    monkeypatch.setattr(factory.bootstrap, "init_database_and_netbox", successful_bootstrap)
    monkeypatch.setattr(factory, "register_generated_proxmox_routes", lambda app: None)
    monkeypatch.setattr(factory, "_run_bootstrap_pass", skip_bootstrap_pass)
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.proxmox_schema.available_proxmox_sdk_versions",
        lambda: [],
    )

    with pytest.raises(RuntimeError, match="request failed"):
        async with factory._lifespan(FastAPI()):
            raise RuntimeError("request failed")

    assert tracked_clients[0].close_calls == 1
