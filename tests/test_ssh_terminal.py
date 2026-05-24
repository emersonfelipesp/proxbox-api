"""Tests for SSH terminal tickets and WebSocket handoff."""

from __future__ import annotations

from pathlib import Path

import pytest

from proxbox_api.services.ssh_terminal import (
    TerminalCredential,
    TerminalSessionError,
    TerminalSessionManager,
    terminal_session_manager,
)


@pytest.fixture(autouse=True)
def clear_terminal_sessions():
    terminal_session_manager._sessions.clear()
    yield
    terminal_session_manager._sessions.clear()


@pytest.mark.asyncio
async def test_terminal_session_ticket_is_one_time() -> None:
    manager = TerminalSessionManager()
    session, ticket = await manager.create_session(
        target_type="node",
        endpoint_id=1,
        node_id=2,
        host="10.0.0.2",
        actor="alice",
        cols=120,
        rows=32,
    )

    consumed = await manager.consume_ticket(session.session_id, ticket)

    assert consumed.session_id == session.session_id
    assert consumed.consumed is True
    with pytest.raises(TerminalSessionError, match="already been used"):
        await manager.consume_ticket(session.session_id, ticket)


def test_create_ssh_terminal_session_requires_backend_api_key(test_client) -> None:
    response = test_client.post(
        "/ssh/sessions",
        json={
            "target_type": "node",
            "endpoint_id": 1,
            "node_id": 2,
            "host": "10.0.0.2",
        },
    )

    assert response.status_code == 401


def test_create_ssh_terminal_session_returns_browser_ticket(auth_test_client) -> None:
    response = auth_test_client.post(
        "/ssh/sessions",
        json={
            "target_type": "node",
            "endpoint_id": 1,
            "node_id": 2,
            "host": "10.0.0.2",
            "cols": 100,
            "rows": 30,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["ticket"]
    assert payload["websocket_path"] == f"/ssh/sessions/{payload['session_id']}/ws"
    assert "X-Proxbox-API-Key" not in payload


def test_asyncssh_relay_pins_host_key_instead_of_disabling_validation() -> None:
    source = Path("proxbox_api/services/ssh_terminal.py").read_text()
    assert "known_hosts=b\"\"" in source
    assert 'server_host_key_algs="default"' in source
    assert "known_hosts=None" not in source
    assert "validate_host_public_key" in source


def test_ssh_terminal_websocket_rejects_invalid_ticket(auth_test_client) -> None:
    created = auth_test_client.post(
        "/ssh/sessions",
        json={
            "target_type": "node",
            "endpoint_id": 1,
            "node_id": 2,
            "host": "10.0.0.2",
        },
    ).json()

    with auth_test_client.websocket_connect(created["websocket_path"]) as websocket:
        websocket.send_json({"type": "auth", "ticket": "wrong"})
        frame = websocket.receive_json()

    assert frame["type"] == "error"
    assert "Invalid SSH terminal ticket" in frame["message"]


def test_ssh_terminal_websocket_uses_ticket_without_backend_api_key(
    monkeypatch,
    auth_test_client,
) -> None:
    created = auth_test_client.post(
        "/ssh/sessions",
        json={
            "target_type": "node",
            "endpoint_id": 1,
            "node_id": 2,
            "host": "10.0.0.2",
        },
    ).json()

    async def fake_fetch_terminal_credential(netbox_session, session):
        return TerminalCredential(
            target_type="node",
            target_id=session.node_id or 0,
            host=session.host or "10.0.0.2",
            port=22,
            username="proxbox",
            known_host_fingerprint="SHA256:abcdefghijklmnopqrstuvwxyz12345678901234567",
            password="secret",
            display="proxbox@10.0.0.2:22",
        )

    async def fake_connect_and_relay(websocket, session, credential):
        await websocket.send_json(
            {
                "type": "ready",
                "session_id": session.session_id,
                "target": credential.display,
            }
        )
        message = await websocket.receive_json()
        assert message == {"type": "close"}
        await websocket.send_json({"type": "exit", "status": 0})

    monkeypatch.setattr(
        "proxbox_api.routes.ssh_terminal.fetch_terminal_credential",
        fake_fetch_terminal_credential,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.ssh_terminal.connect_and_relay",
        fake_connect_and_relay,
    )

    with auth_test_client.websocket_connect(created["websocket_path"]) as websocket:
        websocket.send_json({"type": "auth", "ticket": created["ticket"]})
        ready = websocket.receive_json()
        websocket.send_json({"type": "close"})
        exit_frame = websocket.receive_json()

    assert ready["type"] == "ready"
    assert ready["target"] == "proxbox@10.0.0.2:22"
    assert exit_frame == {"type": "exit", "status": 0}
