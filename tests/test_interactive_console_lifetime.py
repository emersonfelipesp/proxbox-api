"""Event-controlled native console acquisition, proxy, and cleanup boundaries."""

import asyncio
from types import SimpleNamespace

import pytest

from proxbox_api.routes.proxmox import console
from proxbox_api.services.interactive_policy import ExecutionPolicy, InteractiveRuntime


@pytest.mark.parametrize("boundary", ["connect", "proxy", "authentication"])
async def test_quiesce_never_delivers_late_console_capability(monkeypatch, boundary):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "test-generation"))
    endpoint = SimpleNamespace(id=1, host="synthetic.invalid", port=8006, verify_ssl=True)
    request = console.ConsoleSessionRequest(endpoint_id=1, vmid=100, node="test", vm_type="qemu")

    async def pause(name):
        calls.append(name)
        if boundary == name:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # Model a provider which cannot interrupt its pending request.
                await release.wait()

    class Session:
        async def aclose(self):
            calls.append("close")

    async def load(*args):
        return endpoint

    async def connect(*args):
        await pause("connect")
        return Session()

    async def proxy(*args):
        await pause("proxy")
        return {"ticket": "synthetic-private-ticket", "port": 5900}

    async def authentication(*args):
        await pause("authentication")
        return console.ConsoleWebSocketAuth(kind="cookie", value="synthetic-private-cookie")

    monkeypatch.setattr(console, "_load_endpoint", load)
    monkeypatch.setattr(console, "_connect_endpoint", connect)
    monkeypatch.setattr(console, "_request_console_proxy", proxy)
    monkeypatch.setattr(console, "_console_websocket_auth", authentication)

    async def operation():
        async with runtime.admission():
            await console.create_console_session(request, None)
            pytest.fail("A quiesced operation returned private relay material")

    task = asyncio.create_task(operation())
    await entered.wait()
    quiesce = asyncio.create_task(runtime.quiesce())
    await asyncio.sleep(0)
    release.set()
    await quiesce
    from proxbox_api.services.interactive_policy import InteractiveDenied

    with pytest.raises((asyncio.CancelledError, InteractiveDenied)):
        await task
    assert calls.count("close") == 1
    if boundary == "connect":
        assert "proxy" not in calls
    if boundary == "proxy":
        assert "authentication" not in calls
    assert runtime.status()["active"] == runtime.status()["cleanup_active"] == 0
    if boundary != "connect":
        assert runtime.status()["remote_outcome_unknown"] is True


@pytest.mark.parametrize("value", [None, True, False, -1, 0, 65536, "1.2", "١٢", object()])
def test_console_port_rejects_malformed_material(value):
    assert console._console_port(value) is None
