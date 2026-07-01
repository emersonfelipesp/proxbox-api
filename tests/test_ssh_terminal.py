"""Tests for SSH terminal tickets and WebSocket handoff."""

from __future__ import annotations

from pathlib import Path

import pytest

from proxbox_api.services.ssh_terminal import (
    OneShotTerminalCredential,
    TerminalCredential,
    TerminalSessionError,
    TerminalSessionManager,
    fetch_terminal_credential,
    terminal_session_manager,
)

_FINGERPRINT = "SHA256:abcdefghijklmnopqrstuvwxyz12345678901234567"


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
    assert 'known_hosts=b""' in source
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


@pytest.mark.asyncio
async def test_one_shot_credential_bypasses_netbox_fetch() -> None:
    """A one-shot session builds its credential inline, never touching NetBox."""
    manager = TerminalSessionManager()
    session, _ticket = await manager.create_session(
        target_type="node",
        endpoint_id=1,
        node_id=15,
        host="10.0.0.15",
        actor="alice",
        cols=120,
        rows=32,
        one_shot_credential=OneShotTerminalCredential(
            username="root",
            port=22,
            known_host_fingerprint=_FINGERPRINT,
            password="one-shot-secret",
        ),
    )

    # Passing a sentinel NetBox session that would raise if used proves the
    # one-shot path never performs the stored-credential fetch.
    class _Boom:
        def __getattr__(self, _name):  # pragma: no cover - must never run
            raise AssertionError("NetBox session must not be used for one-shot")

    credential = await fetch_terminal_credential(_Boom(), session)

    assert credential.target_type == "node"
    assert credential.target_id == 15
    assert credential.host == "10.0.0.15"
    assert credential.username == "root"
    assert credential.password == "one-shot-secret"
    assert credential.known_host_fingerprint == _FINGERPRINT
    assert credential.display == "root@10.0.0.15:22"


@pytest.mark.asyncio
async def test_one_shot_credential_supports_endpoint_target() -> None:
    manager = TerminalSessionManager()
    session, _ticket = await manager.create_session(
        target_type="endpoint",
        endpoint_id=7,
        node_id=None,
        host="pve.example.com",
        actor="alice",
        cols=120,
        rows=32,
        one_shot_credential=OneShotTerminalCredential(
            username="root",
            port=2222,
            known_host_fingerprint=_FINGERPRINT,
            private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END OPENSSH PRIVATE KEY-----",
        ),
    )

    credential = await fetch_terminal_credential(None, session)

    assert credential.target_type == "endpoint"
    assert credential.target_id == 7
    assert credential.host == "pve.example.com"
    assert credential.port == 2222
    assert credential.private_key is not None
    assert credential.password is None


def test_one_shot_terminal_credential_repr_redacts_secrets() -> None:
    cred = OneShotTerminalCredential(
        username="root",
        port=22,
        known_host_fingerprint=_FINGERPRINT,
        password="super-secret",
        private_key="PRIVATE-KEY-MATERIAL",
    )
    text = repr(cred)
    assert "super-secret" not in text
    assert "PRIVATE-KEY-MATERIAL" not in text
    assert "<redacted>" in text


@pytest.mark.asyncio
async def test_terminal_session_repr_omits_one_shot_credential() -> None:
    manager = TerminalSessionManager()
    session, _ticket = await manager.create_session(
        target_type="node",
        endpoint_id=1,
        node_id=15,
        host="10.0.0.15",
        actor="alice",
        cols=120,
        rows=32,
        one_shot_credential=OneShotTerminalCredential(
            username="root",
            port=22,
            known_host_fingerprint=_FINGERPRINT,
            password="never-in-repr",
        ),
    )
    assert "never-in-repr" not in repr(session)


def test_create_session_accepts_one_shot_credential(auth_test_client) -> None:
    response = auth_test_client.post(
        "/ssh/sessions",
        json={
            "target_type": "node",
            "endpoint_id": 1,
            "node_id": 15,
            "host": "10.0.0.15",
            "one_shot_credential": {
                "username": "root",
                "port": 22,
                "known_host_fingerprint": _FINGERPRINT,
                "password": "one-shot",
            },
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["ticket"]
    # The secret must never be echoed back to the caller.
    assert "one-shot" not in response.text


def test_one_shot_credential_requires_a_secret(auth_test_client) -> None:
    response = auth_test_client.post(
        "/ssh/sessions",
        json={
            "target_type": "node",
            "endpoint_id": 1,
            "node_id": 15,
            "host": "10.0.0.15",
            "one_shot_credential": {
                "username": "root",
                "known_host_fingerprint": _FINGERPRINT,
            },
        },
    )
    assert response.status_code == 422


def test_one_shot_credential_requires_fingerprint(auth_test_client) -> None:
    response = auth_test_client.post(
        "/ssh/sessions",
        json={
            "target_type": "node",
            "endpoint_id": 1,
            "node_id": 15,
            "host": "10.0.0.15",
            "one_shot_credential": {
                "username": "root",
                "known_host_fingerprint": "",
                "password": "one-shot",
            },
        },
    )
    assert response.status_code == 422


def test_one_shot_endpoint_requires_host(auth_test_client) -> None:
    response = auth_test_client.post(
        "/ssh/sessions",
        json={
            "target_type": "endpoint",
            "endpoint_id": 7,
            "one_shot_credential": {
                "username": "root",
                "known_host_fingerprint": _FINGERPRINT,
                "password": "one-shot",
            },
        },
    )
    assert response.status_code == 422
