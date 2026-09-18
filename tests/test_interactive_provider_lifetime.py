"""Actual shared-provider acquisition and late-result ownership regressions."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from proxbox_api.services.interactive_policy import (
    ExecutionPolicy,
    InteractiveDenied,
    InteractiveRuntime,
    acquire_interactive_resource,
    owned_resource,
)
from proxbox_api.session import proxmox_providers


@pytest.mark.parametrize("failure", ["cancel", "sibling"])
async def test_provider_owns_successful_and_late_sibling_acquisitions(monkeypatch, failure):
    entered, release, first_closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closed = []
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "isolated-test"))

    class Client:
        def __init__(self, name):
            self.name = name

        async def aclose(self):
            closed.append(self.name)
            first_closed.set()

    async def schemas(**kwargs):
        return [SimpleNamespace(name="first"), SimpleNamespace(name="second")]

    async def create(schema):
        if schema.name == "first":
            return Client("first")
        entered.set()
        await release.wait()
        return Client("second")

    monkeypatch.setattr(proxmox_providers, "load_proxmox_session_schemas", schemas)
    monkeypatch.setattr(proxmox_providers.ProxmoxSession, "create", create)

    async def operation():
        async with runtime.admission():
            if failure == "sibling":

                async def fail():
                    await entered.wait()
                    raise ValueError("synthetic-upstream-failure")

                await asyncio.gather(
                    proxmox_providers.proxmox_sessions(database_session=None), fail()
                )
            else:
                await proxmox_providers.proxmox_sessions(database_session=None)

    task = asyncio.create_task(operation())
    await entered.wait()
    if failure == "cancel":
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
    else:
        # Allow the failed sibling to begin admission teardown before the late
        # connector returns. The owner must retain that connector meanwhile.
        while not runtime._cleanup:
            await asyncio.sleep(0)
    release.set()
    with pytest.raises((asyncio.CancelledError, ValueError)):
        await asyncio.wait_for(task, 2)
    assert sorted(closed) == ["first", "second"]
    assert runtime.status()["active"] == runtime.status()["cleanup_active"] == 0


async def test_provider_generator_does_not_double_close_owned_clients(monkeypatch):
    closed = []

    class Client:
        async def aclose(self):
            closed.append("close")

    async def create(schema):
        return Client()

    monkeypatch.setattr(proxmox_providers.ProxmoxSession, "create", create)
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "test"))
    async with runtime.admission():
        session = await proxmox_providers._create_request_session(SimpleNamespace())
        generator = proxmox_providers.proxmox_sessions_dep([session])
        assert await anext(generator) == [session]
        await generator.aclose()
        assert closed == []
    assert closed == ["close"]
    # A standalone inventory dependency retains its original cleanup contract.
    generator = proxmox_providers.proxmox_sessions_dep([Client()])
    await anext(generator)
    await generator.aclose()
    assert closed == ["close", "close"]


async def test_acquisition_rechecks_owner_when_scheduled_after_quiesce():
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "test"))
    calls = []

    async def acquire():
        calls.append("forbidden-acquisition")
        return object()

    async def close(value):
        calls.append("close")

    async with runtime.admission():
        asyncio.get_running_loop().call_soon(setattr, runtime, "quiescing", True)
        with pytest.raises(InteractiveDenied):
            async with owned_resource(acquire, close):
                pytest.fail("A late task entered the acquisition")
    assert calls == []


async def test_unfinished_acquisition_remains_owned_after_bounded_shutdown():
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "test"), cleanup_timeout=0.01)
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def acquire():
        entered.set()
        await release.wait()
        return object()

    async def close(value):
        closed.set()

    async def operation():
        async with runtime.admission():
            await acquire_interactive_resource(acquire, close)

    task = asyncio.create_task(operation())
    await entered.wait()
    await runtime.quiesce()
    assert runtime.status()["remote_outcome_unknown"] is True
    assert runtime.status()["cleanup_active"] > 0
    release.set()
    await asyncio.wait_for(closed.wait(), 2)
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert runtime.status()["remote_outcome_unknown"] is True


async def test_quiesce_denies_late_db_and_netbox_material_parsing(monkeypatch):
    from proxbox_api.session import netbox

    def forbidden(*args):
        pytest.fail("A quiesced provider reached NetBox token decryption")

    monkeypatch.setattr(netbox, "netbox_config_from_endpoint", forbidden)
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "test"))
    async with runtime.admission():
        runtime.quiescing = True
        for function in (
            netbox.netbox_api_from_endpoint,
            proxmox_providers._parse_db_endpoint,
            proxmox_providers._parse_netbox_endpoint,
        ):
            with pytest.raises(InteractiveDenied):
                function(SimpleNamespace())


async def test_repeated_cancellation_during_admission_cleanup_keeps_every_close():
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "test"))
    cleanup_started, release = asyncio.Event(), asyncio.Event()
    closed = []

    async def acquire():
        return "synthetic-client"

    async def close(value):
        cleanup_started.set()
        await release.wait()
        closed.append(value)

    async def operation():
        async with runtime.admission():
            await acquire_interactive_resource(acquire, close)

    task = asyncio.create_task(operation())
    await cleanup_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert closed == ["synthetic-client"]
    assert runtime.status()["active"] == runtime.status()["cleanup_active"] == 0


@pytest.mark.parametrize("path", ["/ws", "/ws/virtual-machines"])
async def test_mounted_provider_cancellation_owns_partial_and_late_clients(
    legacy_interactive_app, monkeypatch, path
):
    """Mount the real factory graph; isolate only authentication and remote I/O.

    Full-lifespan companion tests separately exercise actual API-key validation.
    This case does not replace generated registration or claim to test startup.
    """
    from proxbox_api.app import websockets
    from proxbox_api.dependencies import proxbox_tag
    from proxbox_api.routes.proxmox.cluster import cluster_resources, cluster_status

    entered, release = asyncio.Event(), asyncio.Event()
    closed, frames = [], []

    class Client:
        def __init__(self, name):
            self.name = name

        async def aclose(self):
            closed.append(self.name)

    async def schemas(**kwargs):
        return [SimpleNamespace(name="first"), SimpleNamespace(name="second")]

    async def create(schema):
        if schema.name == "second":
            entered.set()
            await release.wait()
        return Client(schema.name)

    def forbidden():
        pytest.fail("A quiesced provider graph reached a collector or tag mutation")

    monkeypatch.setattr(websockets, "check_auth_header", lambda *args: (True, None))
    monkeypatch.setattr(proxmox_providers, "load_proxmox_session_schemas", schemas)
    monkeypatch.setattr(proxmox_providers.ProxmoxSession, "create", create)
    app = legacy_interactive_app
    app.dependency_overrides.update(
        {cluster_resources: forbidden, cluster_status: forbidden, proxbox_tag: forbidden}
    )
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "websocket.connect"})
    incoming.put_nowait({"type": "websocket.receive", "text": json.dumps({"api_key": "synthetic"})})

    async def send(frame):
        frames.append(frame)

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
        "subprotocols": [],
        "state": {},
    }
    task = asyncio.create_task(app(scope, incoming.get, send))
    await asyncio.wait_for(entered.wait(), 5)
    quiesce = asyncio.create_task(app.state.interactive_runtime.quiesce())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(quiesce, 5)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(closed) == ["first", "second"]
    assert [frame["type"] for frame in frames] == ["websocket.accept", "websocket.close"]


async def test_resource_returned_after_policy_change_is_closed_without_delivery():
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "test"))
    closed = []

    async def acquire():
        runtime.quiescing = True
        return "late-resource"

    async def close(value):
        closed.append(value)

    async with runtime.admission():
        with pytest.raises(InteractiveDenied):
            async with owned_resource(acquire, close):
                pytest.fail("A revoked resource was delivered")
    assert closed == ["late-resource"]


@pytest.mark.parametrize("path", ["/ws", "/ws/virtual-machines"])
async def test_mounted_denied_authentication_never_enters_shared_provider(
    legacy_interactive_app, monkeypatch, path
):
    from proxbox_api.app import websockets

    async def forbidden(**kwargs):
        pytest.fail("Rejected authentication entered the shared Proxmox provider")

    monkeypatch.setattr(
        websockets, "check_auth_header", lambda *args: (False, "Authentication failed")
    )
    monkeypatch.setattr(proxmox_providers, "load_proxmox_session_schemas", forbidden)
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "websocket.connect"})
    incoming.put_nowait(
        {"type": "websocket.receive", "text": json.dumps({"api_key": "synthetic-invalid"})}
    )
    frames = []

    async def send(frame):
        frames.append(frame)

    await legacy_interactive_app(
        {
            "type": "websocket",
            "scheme": "ws",
            "path": path,
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "subprotocols": [],
            "state": {},
        },
        incoming.get,
        send,
    )
    assert [frame["type"] for frame in frames] == ["websocket.accept", "websocket.close"]
    assert frames[-1]["code"] == 4001


@pytest.mark.parametrize("selector", ["ip_address", "domain", "name"])
@pytest.mark.parametrize("matches", [True, False])
async def test_interactive_provider_preserves_single_endpoint_selectors(
    monkeypatch, selector, matches
):
    from proxbox_api.exception import ProxboxException

    schema = SimpleNamespace(ip_address="synthetic", domain="synthetic", name="synthetic")
    closed = []

    async def load(**kwargs):
        return [schema]

    class Client:
        async def aclose(self):
            closed.append("close")

    async def create(selected):
        assert selected is schema
        return Client()

    monkeypatch.setattr(proxmox_providers, "load_proxmox_session_schemas", load)
    monkeypatch.setattr(proxmox_providers.ProxmoxSession, "create", create)
    async with InteractiveRuntime(ExecutionPolicy("legacy", "test")).admission():
        arguments = {selector: "synthetic" if matches else "missing"}
        if matches:
            assert (
                len(await proxmox_providers.proxmox_sessions(database_session=None, **arguments))
                == 1
            )
        else:
            with pytest.raises(ProxboxException, match="No result found"):
                await proxmox_providers.proxmox_sessions(database_session=None, **arguments)
    assert closed == (["close"] if matches else [])


@pytest.mark.parametrize(
    "arguments",
    [
        {"source": "unknown"},
        {"endpoint_ids": "1" * 256},
        {"endpoint_ids": ",".join(["1"] * 101)},
        {"endpoint_ids": "invalid"},
    ],
)
async def test_interactive_provider_preserves_query_validation_before_acquisition(
    monkeypatch, arguments
):
    from proxbox_api.exception import ProxboxException

    async def forbidden(**kwargs):
        pytest.fail("Invalid endpoint selection reached a provider")

    monkeypatch.setattr(proxmox_providers, "load_proxmox_session_schemas", forbidden)
    async with InteractiveRuntime(ExecutionPolicy("legacy", "test")).admission():
        with pytest.raises(ProxboxException):
            await proxmox_providers.proxmox_sessions(database_session=None, **arguments)


async def test_interactive_provider_failure_never_logs_upstream_material(monkeypatch, caplog):
    import logging

    from proxbox_api.exception import ProxboxException

    async def load(**kwargs):
        return [SimpleNamespace(name="synthetic")]

    async def fail(schema):
        raise ValueError("synthetic-interactive-provider-private-canary")

    monkeypatch.setattr(proxmox_providers, "load_proxmox_session_schemas", load)
    monkeypatch.setattr(proxmox_providers.ProxmoxSession, "create", fail)
    caplog.set_level(logging.DEBUG, logger="proxbox")
    async with InteractiveRuntime(ExecutionPolicy("legacy", "test")).admission():
        with pytest.raises(ProxboxException) as denied:
            await proxmox_providers.proxmox_sessions(database_session=None)
    evidence = caplog.text + str(denied.value) + str(denied.value.python_exception)
    assert "synthetic-interactive-provider-private-canary" not in evidence


async def test_provider_preserves_static_denial_after_late_connection(monkeypatch):
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "test"))
    closed = []

    class Client:
        async def aclose(self):
            closed.append("close")

    async def load(**kwargs):
        return [SimpleNamespace(name="synthetic")]

    async def create(schema):
        runtime.quiescing = True
        return Client()

    monkeypatch.setattr(proxmox_providers, "load_proxmox_session_schemas", load)
    monkeypatch.setattr(proxmox_providers.ProxmoxSession, "create", create)
    async with runtime.admission():
        with pytest.raises(InteractiveDenied):
            await proxmox_providers.proxmox_sessions(database_session=None)
    assert closed == ["close"]
