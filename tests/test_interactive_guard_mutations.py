"""Narrow mutation oracles for independent interactive execution guards.

Each oracle proves that the real guard denies before its named effect, then
bypasses only that guard and requires ``GuardMutationReached`` from the effect.
"""

import asyncio
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect

from proxbox_api.services import interactive_policy
from proxbox_api.services.interactive_policy import (
    ExecutionPolicy,
    InteractiveDenied,
    InteractiveRuntime,
)
from tests.websocket_test_support import websocket_error_after_effect, websocket_session


class GuardMutationReached(RuntimeError):
    """A removed guard allowed execution to reach its designated effect."""


async def _receive_connect():
    return {"type": "websocket.connect"}


@pytest.mark.parametrize(
    "kind,path,method,effect",
    [
        ("http", "/ssh/sessions", "POST", "SSH ticket creator"),
        ("http", "/proxmox/console/sessions", "POST", "console capability creator"),
        ("http", "/proxmox/console/browser-sessions", "POST", "browser capability creator"),
        ("websocket", "/ssh/sessions/existing/ws", "GET", "old SSH ticket consumer"),
        ("websocket", "/proxmox/console/browser-stream", "GET", "browser relay ticket consumer"),
        ("websocket", "/ws", "GET", "actorless synchronization consumer"),
        ("websocket", "/ws/virtual-machines", "GET", "virtual-machine consumer"),
    ],
)
async def test_exact_asgi_entry_mutation_reaches_only_its_named_effect(
    monkeypatch, kind, path, method, effect
):
    """Delete one path+method entry while retaining every sibling entry."""
    scope = {"type": kind, "path": path, "root_path": "", "method": method}
    reached, frames = [], []

    async def downstream(*_args):
        reached.append(effect)

    async def send(frame):
        frames.append(frame)

    middleware = interactive_policy.InteractiveBoundaryMiddleware(
        downstream, InteractiveRuntime(ExecutionPolicy())
    )
    await middleware(scope, _receive_connect, send)
    assert reached == []
    assert frames[0].get("status", frames[0].get("code")) == (403 if kind == "http" else 1008)

    original = interactive_policy._guarded

    def without_exact_entry(candidate):
        keys = ("type", "path", "method")
        return (
            False
            if all(candidate.get(key) == scope.get(key) for key in keys)
            else original(candidate)
        )

    async def mutated_downstream(*_args):
        raise GuardMutationReached(f"exact ASGI entry exposed {effect}")

    monkeypatch.setattr(interactive_policy, "_guarded", without_exact_entry)
    mutated = interactive_policy.InteractiveBoundaryMiddleware(
        mutated_downstream, InteractiveRuntime(ExecutionPolicy())
    )
    with pytest.raises(GuardMutationReached, match=f"exact ASGI entry exposed {effect}"):
        await mutated(scope, _receive_connect, send)
    sibling = dict(scope, path="/unrelated")
    assert without_exact_entry(sibling) is original(sibling)


async def _locked_ticket_guard(*, consumer: bool, mutate: bool) -> None:
    """Exercise only the post-lock creator or consumer guard."""
    from proxbox_api.services import ssh_terminal

    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "mutation"))
    manager = ssh_terminal.TerminalSessionManager()
    session = ticket = None
    if consumer:
        async with runtime.admission():
            session, ticket = await manager.create_session(
                target_type="endpoint",
                endpoint_id=1,
                node_id=None,
                host=None,
                actor="mutation",
                cols=80,
                rows=24,
            )
    entered = asyncio.Event()
    original = ssh_terminal.require_interactive
    calls = 0

    def indexed_require():
        nonlocal calls
        calls += 1
        if mutate and calls == 2:
            return runtime
        return original()

    def ticket_effect(*_args):
        effect = "old SSH ticket authentication" if consumer else "SSH ticket persistence"
        raise GuardMutationReached(effect)

    async def operation():
        async with runtime.admission():
            entered.set()
            if consumer:
                return await manager.consume_ticket(session.session_id, ticket)
            return await manager.create_session(
                target_type="endpoint",
                endpoint_id=1,
                node_id=None,
                host=None,
                actor="mutation",
                cols=80,
                rows=24,
            )

    await manager._lock.acquire()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ssh_terminal, "require_interactive", indexed_require)
        if consumer:
            patch.setattr(ssh_terminal.secrets, "compare_digest", ticket_effect)
        else:
            patch.setattr(manager, "_hash_ticket", ticket_effect)
        task = asyncio.create_task(operation())
        await entered.wait()
        runtime.quiescing = True
        manager._lock.release()
        expected = GuardMutationReached if mutate else InteractiveDenied
        with pytest.raises(expected):
            await task
    assert calls == 2
    if consumer:
        assert session.consumed is False


@pytest.mark.parametrize("consumer", [False, True])
async def test_ssh_service_inner_guard_mutation_reaches_only_ticket_effect(consumer):
    """Keep the outer service guard active while mutating its post-lock sibling."""
    await _locked_ticket_guard(consumer=consumer, mutate=False)
    await _locked_ticket_guard(consumer=consumer, mutate=True)


async def test_console_service_guard_mutation_reaches_endpoint_material(monkeypatch):
    from proxbox_api.routes.proxmox import console

    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "mutation"))

    def parse_endpoint(_endpoint):
        raise GuardMutationReached("console endpoint material parsing")

    monkeypatch.setattr(console, "_parse_db_endpoint", parse_endpoint)
    async with runtime.admission():
        runtime.quiescing = True
        with pytest.raises(InteractiveDenied):
            await console._open_session(object())
        monkeypatch.setattr(console, "require_interactive", lambda: runtime)
        with pytest.raises(GuardMutationReached, match="console endpoint material parsing"):
            await console._open_session(object())


@pytest.mark.parametrize(
    "helper,effect_method,effect,payload",
    [
        ("_send_upstream", "send", "upstream frame write", b"mutation"),
        ("_send_browser_bytes", "send_bytes", "browser byte-frame write", b"mutation"),
        ("_send_browser_frame", "send_text", "browser text-frame write", "mutation"),
    ],
)
async def test_frame_guard_mutation_reaches_only_named_transport_effect(
    monkeypatch, helper, effect_method, effect, payload
):
    from proxbox_api.services import console_relay

    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "mutation"))

    async def sentinel(_data):
        raise GuardMutationReached(effect)

    target = SimpleNamespace(**{effect_method: sentinel})
    send = getattr(console_relay, helper)
    async with runtime.admission():
        runtime.quiescing = True
        with pytest.raises(InteractiveDenied):
            await send(target, payload)
        monkeypatch.setattr(console_relay, "require_interactive", lambda: runtime)
        with pytest.raises(GuardMutationReached, match=effect):
            await send(target, payload)


@pytest.mark.parametrize(
    "source,effect",
    [("proxmox", "Proxmox client acquisition"), ("netbox", "NetBox settings acquisition")],
)
async def test_provider_guard_mutation_reaches_only_named_acquisition(monkeypatch, source, effect):
    from proxbox_api.session import proxmox_providers

    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "mutation"))

    async def sentinel(*_args, **_kwargs):
        raise GuardMutationReached(effect)

    def sync_sentinel(*_args, **_kwargs):
        raise GuardMutationReached(effect)

    async with runtime.admission():
        runtime.quiescing = True
        if source == "proxmox":
            with pytest.raises(InteractiveDenied):
                await proxmox_providers._create_request_session(object())
            monkeypatch.setattr(proxmox_providers, "current_interactive_runtime", lambda: runtime)
            monkeypatch.setattr(proxmox_providers, "_interactive_acquisition", sentinel)
            with pytest.raises(GuardMutationReached, match=effect):
                await proxmox_providers._create_request_session(object())
        else:
            monkeypatch.setattr(
                proxmox_providers, "get_netbox_async_session", lambda **_kwargs: object()
            )
            monkeypatch.setattr(proxmox_providers, "get_settings", sync_sentinel)
            with pytest.raises(InteractiveDenied):
                await proxmox_providers._load_netbox_source_plugin_settings(object())
            monkeypatch.setattr(proxmox_providers, "current_interactive_runtime", lambda: runtime)
            with pytest.raises(GuardMutationReached, match=effect):
                await proxmox_providers._load_netbox_source_plugin_settings(object())


@pytest.mark.parametrize(
    "boundary,effect",
    [("input", "SSH input write"), ("connect", "AsyncSSH connect"), ("pty", "PTY creation")],
)
async def test_ssh_post_wait_guard_mutation_reaches_only_named_effect(
    monkeypatch, boundary, effect
):
    from proxbox_api.services import ssh_terminal

    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "mutation"))

    async def async_sentinel(*_args, **_kwargs):
        raise GuardMutationReached(effect)

    def sync_sentinel(*_args, **_kwargs):
        raise GuardMutationReached(effect)

    if boundary == "input":
        process = SimpleNamespace(stdin=SimpleNamespace(write=sync_sentinel))

        async def invoke():
            await ssh_terminal._handle_terminal_message(
                SimpleNamespace(),
                process,
                SimpleNamespace(cols=80, rows=24),
                {"type": "input", "data": "mutation"},
            )
    elif boundary == "connect":

        class SSHClient:
            pass

        monkeypatch.setattr(
            ssh_terminal,
            "_load_asyncssh",
            lambda: SimpleNamespace(connect=async_sentinel, SSHClient=SSHClient),
        )
        credential = ssh_terminal.TerminalCredential(
            "endpoint", 1, "test.invalid", 22, "mutation", "SHA256:test", password="test"
        )

        async def invoke():
            await ssh_terminal._connect_terminal(credential)
    else:
        connection = SimpleNamespace(create_process=async_sentinel)

        async def invoke():
            await ssh_terminal._terminal_process(connection, SimpleNamespace(cols=80, rows=24))

    async with runtime.admission():
        runtime.quiescing = True
        with pytest.raises(InteractiveDenied):
            await invoke()
        monkeypatch.setattr(ssh_terminal, "require_interactive", lambda: runtime)
        with pytest.raises(GuardMutationReached, match=effect):
            await invoke()


async def _post_proxy_case(monkeypatch, *, mutate: bool) -> int:
    from proxbox_api.routes.proxmox import console

    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "mutation"))
    original = console.require_interactive
    calls = 0

    async def proxy(_px, _request):
        runtime.quiescing = True
        return {"ticket": "mutation", "port": 5900}

    def parser(_raw, _request):
        raise GuardMutationReached("post-proxy console capability parser")

    def indexed_require():
        nonlocal calls
        calls += 1
        if mutate and calls == 2:
            return runtime
        return original()

    monkeypatch.setattr(console, "require_interactive", indexed_require)
    monkeypatch.setattr(console, "_request_console_proxy", proxy)
    monkeypatch.setattr(console, "_console_ticket", parser)
    async with runtime.admission():
        expected = GuardMutationReached if mutate else InteractiveDenied
        with pytest.raises(expected):
            await console._console_response(SimpleNamespace(endpoint_id=1), object(), object())
    return calls


async def test_post_proxy_guard_mutation_reaches_only_capability_parser(monkeypatch):
    assert await _post_proxy_case(monkeypatch, mutate=False) == 2
    monkeypatch.undo()
    assert await _post_proxy_case(monkeypatch, mutate=True) == 2


def test_sync_auth_guard_mutation_reaches_effect_provider(legacy_test_client):
    from proxbox_api.app import websockets
    from proxbox_api.session.proxmox_providers import proxmox_sessions_dep

    reached = []

    async def provider():
        reached.append("provider")
        raise GuardMutationReached("sync auth mutation reached provider")
        yield []  # pragma: no cover

    app = legacy_test_client.app
    app.dependency_overrides[proxmox_sessions_dep] = provider
    try:
        with websocket_session(legacy_test_client, "/ws") as websocket:
            websocket.send_json({"api_key": "invalid"})
            with pytest.raises(WebSocketDisconnect) as denied:
                websocket.receive_text()
            assert denied.value.code == 4001
        assert reached == []

        async def removed_authentication():
            return None

        app.dependency_overrides[websockets._authorize_sync_websocket] = removed_authentication
        with websocket_error_after_effect(
            legacy_test_client,
            "/ws",
            expected_error=GuardMutationReached,
            effect_observed=lambda: reached == ["provider"],
        ):
            pass
        assert reached == ["provider"]
    finally:
        app.dependency_overrides.pop(websockets._authorize_sync_websocket, None)
        app.dependency_overrides.pop(proxmox_sessions_dep, None)
