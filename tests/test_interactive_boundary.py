"""Mounted application regressions for the interactive RPC-only boundary."""

import pytest
from starlette.websockets import WebSocketDisconnect

from proxbox_api.services.ssh_terminal import terminal_session_manager


def test_default_rpc_only_denies_ticket_creation_before_store(auth_test_client):
    """A valid service identity cannot create an unrestricted capability."""
    before = set(terminal_session_manager._sessions)
    response = auth_test_client.post(
        "/ssh/sessions",
        json={"target_type": "endpoint", "endpoint_id": 1},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Interactive execution is unavailable."}
    assert set(terminal_session_manager._sessions) == before


def test_default_denial_precedes_every_eager_provider(auth_test_client, monkeypatch):
    """Real mounted HTTP and WebSocket routes must reject before provider entry."""
    from proxbox_api.routes import ssh_terminal
    from proxbox_api.routes.proxmox import console

    async def forbidden(*args, **kwargs):
        pytest.fail("Denied request reached a material provider")

    monkeypatch.setattr(console, "_load_endpoint", forbidden)
    monkeypatch.setattr(ssh_terminal, "_resolve_terminal_credential", forbidden)
    for vm_type, console_type in (("qemu", "novnc"), ("qemu", "term"), ("lxc", "term")):
        response = auth_test_client.post(
            "/proxmox/console/sessions",
            json={
                "endpoint_id": 1,
                "vmid": 1,
                "node": "test-node",
                "vm_type": vm_type,
                "console_type": console_type,
            },
        )
        assert response.status_code == 403
    for path in ("/ssh/sessions/old-ticket/ws", "/ws", "/ws/virtual-machines"):
        with pytest.raises(WebSocketDisconnect) as denied:
            with auth_test_client.websocket_connect(path):
                pytest.fail("Denied WebSocket accepted")
        assert denied.value.code == 1008


def test_authenticated_policy_status_has_no_provider_dependencies(auth_test_client):
    response = auth_test_client.get("/execution-policy")
    assert response.status_code == 200
    status = response.json()
    assert status["component"] == "proxbox-api"
    assert status["aggregate_ready"] is False
    assert status["active"] == 0


def test_legacy_sync_auth_precedes_the_entire_provider_graph(
    legacy_auth_test_client, monkeypatch, client_with_fake_netbox
):
    """Pin the deployed FastAPI solver ordering with real API-key validation."""
    from sqlmodel import Session

    from proxbox_api.app import bootstrap, websockets
    from proxbox_api.database import ApiKey, get_engine
    from proxbox_api.dependencies import proxbox_tag
    from proxbox_api.routes.proxmox.cluster import cluster_resources, cluster_status
    from proxbox_api.session.netbox import get_netbox_async_session, get_netbox_session
    from proxbox_api.session.proxmox_providers import proxmox_sessions_dep

    # The unchanged WebSocket authenticator uses the worker database rather
    # than FastAPI's HTTP dependency override. Both databases are disposable.
    with Session(get_engine()) as session:
        ApiKey.store_key(session, legacy_auth_test_client.headers["X-Proxbox-API-Key"])

    events = []
    original = websockets.check_auth_header

    def authenticate(*args):
        result = original(*args)
        events.append("auth-ok" if result[0] else "auth-denied")
        return result

    def effect(name, value):
        def provider():
            assert events[0] == "auth-ok"
            events.append(name)
            return value

        return provider

    async def sessions():
        assert events[0] == "auth-ok"
        events.append("connect")
        try:
            yield []
        finally:
            events.append("close")

    async def synchronize(**kwargs):
        assert events[0] == "auth-ok"
        events.append("sync")
        await kwargs["websocket"].send_text("test-sync-complete")

    app = legacy_auth_test_client.app
    app.dependency_overrides.update(
        {
            get_netbox_session: effect("netbox-token", client_with_fake_netbox),
            get_netbox_async_session: effect("netbox-token", client_with_fake_netbox),
            proxmox_sessions_dep: sessions,
            cluster_status: effect("cluster-status", []),
            cluster_resources: effect("cluster-resources", []),
            proxbox_tag: effect("tag-write", object()),
        }
    )
    monkeypatch.setattr(websockets, "check_auth_header", authenticate)
    monkeypatch.setattr(websockets, "create_virtual_machines", synchronize)
    monkeypatch.setattr(websockets, "create_proxmox_devices", synchronize)
    monkeypatch.setattr(bootstrap, "netbox_session", client_with_fake_netbox)

    for path in ("/ws", "/ws/virtual-machines"):
        events.clear()
        with legacy_auth_test_client.websocket_connect(path) as websocket:
            websocket.send_json({"api_key": "synthetic-invalid-key"})
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_text()
        assert events == ["auth-denied"]

        events.clear()
        with legacy_auth_test_client.websocket_connect(path) as websocket:
            websocket.send_json({"api_key": legacy_auth_test_client.headers["X-Proxbox-API-Key"]})
            assert websocket.receive_text() == "Connected!"
            if path == "/ws":
                assert websocket.receive_text() == "Connected 2!"
                websocket.send_text("Sync Nodes")
                assert websocket.receive_text() == "Sync Nodes"
            assert websocket.receive_text() == "test-sync-complete"
        assert events[0] == "auth-ok"
        assert {
            "connect",
            "cluster-status",
            "cluster-resources",
            "tag-write",
            "sync",
            "close",
        } <= set(events)


def test_legacy_one_shot_skips_netbox_configuration_and_invalid_ticket_skips_material(
    legacy_auth_test_client, monkeypatch
):
    from proxbox_api.routes import ssh_terminal

    calls = []

    async def forbidden(*args, **kwargs):
        pytest.fail("One-shot or invalid ticket acquired NetBox configuration")

    async def relay(websocket, session, credential):
        calls.append(credential.username)
        await websocket.send_json({"type": "ready"})

    monkeypatch.setattr(ssh_terminal, "get_netbox_async_session", forbidden)
    monkeypatch.setattr(ssh_terminal, "connect_and_relay", relay)
    created = legacy_auth_test_client.post(
        "/ssh/sessions",
        json={
            "target_type": "endpoint",
            "endpoint_id": 1,
            "host": "test.invalid",
            "one_shot_credential": {
                "username": "synthetic-user",
                "password": "synthetic-canary",
                "known_host_fingerprint": "SHA256:test",
            },
        },
    ).json()
    with legacy_auth_test_client.websocket_connect(created["websocket_path"]) as websocket:
        websocket.send_json({"type": "auth", "ticket": "invalid"})
        assert websocket.receive_json()["type"] == "error"
    assert calls == []
    with legacy_auth_test_client.websocket_connect(created["websocket_path"]) as websocket:
        websocket.send_json({"type": "auth", "ticket": created["ticket"]})
        assert websocket.receive_json() == {"type": "ready"}
    assert calls == ["synthetic-user"]


def test_existing_ticket_cannot_cross_the_default_boundary(
    legacy_auth_test_client, auth_test_client, monkeypatch
):
    from proxbox_api.routes import ssh_terminal

    async def forbidden(*args, **kwargs):
        pytest.fail("An old ticket reached credential acquisition")

    monkeypatch.setattr(ssh_terminal, "_resolve_terminal_credential", forbidden)
    response = legacy_auth_test_client.post(
        "/ssh/sessions", json={"target_type": "endpoint", "endpoint_id": 1}
    )
    assert response.status_code == 201
    ticket = response.json()
    retained = terminal_session_manager._sessions[ticket["session_id"]]
    with pytest.raises(WebSocketDisconnect) as denied:
        with auth_test_client.websocket_connect(ticket["websocket_path"]):
            pytest.fail("The default consumer accepted an existing valid ticket")
    assert denied.value.code == 1008
    assert retained.consumed is False


def test_policy_is_pinned_and_local_quiesce_precedes_auth(legacy_auth_test_client, monkeypatch):
    monkeypatch.setenv("PROXBOX_EXECUTION_MODE", "rpc_only")
    monkeypatch.setenv("PROXBOX_EXECUTION_GENERATION", "different-generation")
    response = legacy_auth_test_client.post(
        "/ssh/sessions", json={"target_type": "endpoint", "endpoint_id": 1}
    )
    assert response.status_code == 201
    runtime = legacy_auth_test_client.app.state.interactive_runtime
    assert runtime.policy.mode == "legacy"
    assert runtime.policy.generation != "different-generation"
    legacy_auth_test_client.portal.call(runtime.quiesce)
    assert response.json()["session_id"] not in terminal_session_manager._sessions
    for path in ("/ssh/sessions", "/proxmox/console/sessions"):
        denied = legacy_auth_test_client.post(path, headers={"X-Proxbox-API-Key": "invalid"})
        assert denied.status_code == 403
    for path in ("/ws", "/ws/virtual-machines"):
        with pytest.raises(WebSocketDisconnect) as denied:
            with legacy_auth_test_client.websocket_connect(path):
                pytest.fail("A quiescing worker accepted a new WebSocket")
        assert denied.value.code == 1008


def test_legacy_http_authentication_precedes_console_and_ssh_effects(
    legacy_test_client, monkeypatch
):
    from proxbox_api.routes.proxmox import console

    async def forbidden(*args, **kwargs):
        pytest.fail("An unauthenticated request reached console configuration")

    monkeypatch.setattr(console, "_load_endpoint", forbidden)
    before = set(terminal_session_manager._sessions)
    for path in ("/ssh/sessions", "/proxmox/console/sessions"):
        assert legacy_test_client.post(path, json={}).status_code == 401
    assert legacy_test_client.get("/execution-policy").status_code == 401
    assert set(terminal_session_manager._sessions) == before
