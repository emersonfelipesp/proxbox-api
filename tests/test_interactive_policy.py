"""Owned native admission and resource-lifetime safety boundaries."""

import asyncio

import pytest

from proxbox_api.services.interactive_policy import (
    ExecutionPolicy,
    InteractiveDenied,
    InteractiveRuntime,
    owned_resource,
    require_interactive,
)
from proxbox_api.services.ssh_terminal import TerminalSessionManager


def test_policy_defaults_and_strict_configuration():
    assert ExecutionPolicy.from_environment({}) == ExecutionPolicy()
    assert ExecutionPolicy.from_environment({"PROXBOX_EXECUTION_MODE": "legacy"}).mode == "legacy"
    for value in ("", "RPC_ONLY", " legacy", "true", "legacy\n"):
        with pytest.raises(ValueError, match="Invalid PROXBOX_EXECUTION_MODE"):
            ExecutionPolicy.from_environment({"PROXBOX_EXECUTION_MODE": value})
    for value in ("", " ", "x/secret", "x" * 129):
        with pytest.raises(ValueError, match="Invalid PROXBOX_EXECUTION_GENERATION"):
            ExecutionPolicy.from_environment({"PROXBOX_EXECUTION_GENERATION": value})


def test_status_is_scoped_and_missing_generation_is_unready():
    runtime = InteractiveRuntime(ExecutionPolicy())
    assert runtime.status()["local_ready"] is False
    runtime = InteractiveRuntime(ExecutionPolicy(generation="cutover-1"))
    assert runtime.status() == {
        "component": "proxbox-api",
        "capability": "interactive-rpc-boundary-v1",
        "mode": "rpc_only",
        "generation": "cutover-1",
        "quiescing": False,
        "active": 0,
        "cleanup_active": 0,
        "remote_outcome_unknown": False,
        "local_ready": True,
        "aggregate_ready": False,
    }


async def _ticket(manager):
    return await manager.create_session(
        target_type="endpoint",
        endpoint_id=1,
        node_id=None,
        host=None,
        actor="test-actor",
        cols=80,
        rows=24,
    )


async def test_service_calls_require_owned_legacy_admission():
    manager = TerminalSessionManager()
    with pytest.raises(InteractiveDenied):
        await _ticket(manager)
    assert not manager._sessions
    strict = InteractiveRuntime(ExecutionPolicy(generation="g1"))
    with pytest.raises(InteractiveDenied):
        async with strict.admission():
            pytest.fail("RPC-only admission succeeded")
    legacy = InteractiveRuntime(ExecutionPolicy("legacy", "g1"))
    async with legacy.admission():
        session, ticket = await _ticket(manager)
    with pytest.raises(InteractiveDenied):
        await manager.consume_ticket(session.session_id, ticket)
    assert not session.consumed
    async with legacy.admission():
        assert await manager.consume_ticket(session.session_id, ticket) is session


async def test_old_generation_ticket_does_not_become_authority():
    manager = TerminalSessionManager()
    async with InteractiveRuntime(ExecutionPolicy("legacy", "old")).admission():
        session, ticket = await _ticket(manager)
    async with InteractiveRuntime(ExecutionPolicy("legacy", "new")).admission():
        with pytest.raises(Exception, match="generation is no longer valid"):
            await manager.consume_ticket(session.session_id, ticket)
    assert not session.consumed


async def test_escaped_child_loses_admission_when_owner_returns():
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "g1"))
    resumed = asyncio.Event()

    async def child():
        await resumed.wait()
        require_interactive()

    async with runtime.admission():
        task = asyncio.create_task(child())
    resumed.set()
    with pytest.raises(InteractiveDenied):
        await task


async def test_quiesce_interrupts_idle_owner_and_denies_new_admissions():
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "g1"))
    entered = asyncio.Event()
    exited = asyncio.Event()

    async def idle():
        async with runtime.admission():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()

    task = asyncio.create_task(idle())
    await entered.wait()
    await runtime.quiesce()
    assert exited.is_set()
    assert task.cancelled()
    assert runtime.status()["active"] == 0
    with pytest.raises(InteractiveDenied):
        async with runtime.admission():
            pytest.fail("Quiescing worker admitted a new operation")


async def test_quiesce_drops_owned_pending_inline_material_only():
    from proxbox_api.services.ssh_terminal import OneShotTerminalCredential

    manager = TerminalSessionManager()
    first = InteractiveRuntime(ExecutionPolicy("legacy", "g1"))
    second = InteractiveRuntime(ExecutionPolicy("legacy", "g1"))
    async with first.admission():
        session, _ = await manager.create_session(
            target_type="endpoint",
            endpoint_id=1,
            node_id=None,
            host="test.invalid",
            actor=None,
            cols=80,
            rows=24,
            one_shot_credential=OneShotTerminalCredential("test", 22, "SHA256:test", "canary"),
        )
    async with second.admission():
        other, _ = await _ticket(manager)
    await first.quiesce()
    assert session.session_id not in manager._sessions
    assert session.one_shot_credential is None
    assert other.session_id in manager._sessions


async def test_late_acquisition_closes_through_repeated_cancellation():
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "g1"))
    started, release = asyncio.Event(), asyncio.Event()
    resource = object()
    closed = []

    async def acquire():
        started.set()
        await release.wait()
        return resource

    async def close(value):
        closed.append(value)

    async def operation():
        async with runtime.admission():
            async with owned_resource(acquire, close):
                pytest.fail("A cancelled acquisition reached delivery")

    task = asyncio.create_task(operation())
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)
    assert closed == [resource]
    assert runtime.status()["cleanup_active"] == 0


async def test_cleanup_timeout_retains_late_owner_and_sticky_uncertainty():
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "g1"), cleanup_timeout=0.01)
    release = asyncio.Event()
    await runtime.finish_cleanup(release.wait())
    assert runtime.status()["cleanup_active"] == 1
    assert runtime.uncertain
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert runtime.status()["cleanup_active"] == 0
    assert runtime.status()["remote_outcome_unknown"] is True


async def test_cleanup_failure_is_value_free_and_explicit():
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "g1"))

    async def fail():
        raise ValueError("synthetic-credential-canary")

    await runtime.finish_cleanup(fail())
    await asyncio.sleep(0)
    assert runtime.uncertain
    assert "synthetic-credential-canary" not in str(runtime.status())


@pytest.mark.parametrize(
    "kind,path,method,denied",
    [
        ("http", "/ssh/sessions", "POST", True),
        ("http", "/proxmox/console/sessions/", "POST", True),
        ("websocket", "/ssh/sessions/existing/ws", "GET", True),
        ("websocket", "/ws/virtual-machines", "GET", True),
        ("http", "/ssh/host-key", "POST", False),
        ("http", "/ssh/sessions", "GET", False),
        ("websocket", "/", "GET", False),
        ("lifespan", "", "", False),
    ],
)
async def test_mount_relative_boundary_preserves_unrelated_surfaces(kind, path, method, denied):
    from proxbox_api.services.interactive_policy import InteractiveBoundaryMiddleware

    frames, calls = [], []

    async def downstream(*args):
        calls.append("downstream")

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(frame):
        frames.append(frame)

    scope = {"type": kind, "path": "/mounted" + path, "root_path": "/mounted", "method": method}
    await InteractiveBoundaryMiddleware(downstream, InteractiveRuntime(ExecutionPolicy()))(
        scope, receive, send
    )
    assert bool(calls) is not denied
    if denied:
        assert frames[0].get("status", frames[0].get("code")) == (403 if kind == "http" else 1008)


async def test_midstream_policy_denial_closes_websocket_exactly_once():
    from proxbox_api.services.interactive_policy import InteractiveBoundaryMiddleware

    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "g1"))
    frames = []

    async def downstream(scope, receive, send):
        await send({"type": "websocket.accept"})
        runtime.quiescing = True
        await send({"type": "websocket.send", "text": "synthetic-private-material"})

    async def receive():
        return {"type": "websocket.connect"}

    async def send(frame):
        frames.append(frame)

    await InteractiveBoundaryMiddleware(downstream, runtime)(
        {"type": "websocket", "path": "/ws", "root_path": ""}, receive, send
    )
    assert [frame["type"] for frame in frames] == ["websocket.accept", "websocket.close"]
    assert "synthetic-private-material" not in str(frames)


async def test_waiting_ticket_store_rechecks_quiesce_before_retaining_material():
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "g1"))
    manager = TerminalSessionManager()
    entered = asyncio.Event()

    async def operation():
        async with runtime.admission():
            entered.set()
            await _ticket(manager)

    await manager._lock.acquire()
    task = asyncio.create_task(operation())
    await entered.wait()
    runtime.quiescing = True
    manager._lock.release()
    with pytest.raises(InteractiveDenied):
        await task
    assert manager._sessions == {}


async def test_nested_admission_cannot_replace_an_existing_owner():
    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "test"))
    async with runtime.admission():
        with pytest.raises(InteractiveDenied):
            async with runtime.admission():
                pytest.fail("A nested admission replaced its owner")
        assert require_interactive() is runtime


@pytest.mark.parametrize("kind,path", [("http", "/ssh/sessions"), ("websocket", "/ws")])
async def test_dependency_denial_before_response_has_one_static_refusal(kind, path):
    from proxbox_api.services.interactive_policy import InteractiveBoundaryMiddleware

    runtime = InteractiveRuntime(ExecutionPolicy("legacy", "test"))
    frames = []

    async def downstream(*args):
        raise InteractiveDenied()

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(frame):
        frames.append(frame)

    await InteractiveBoundaryMiddleware(downstream, runtime)(
        {"type": kind, "path": path, "root_path": "", "method": "POST"}, receive, send
    )
    assert frames[0].get("status", frames[0].get("code")) == (403 if kind == "http" else 1008)
    if kind == "websocket":
        assert len(frames) == 1
