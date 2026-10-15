"""Tests for SSH terminal tickets and WebSocket handoff."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest
from starlette.websockets import WebSocketDisconnect

import proxbox_api.services.ssh_terminal as ssh_terminal
from proxbox_api.services.ssh_terminal import (
    OneShotTerminalCredential,
    TerminalCredential,
    TerminalSessionError,
    TerminalSessionManager,
    fetch_terminal_credential,
    terminal_session_manager,
)
from proxbox_api.ssrf import ResolvedAddress
from tests.websocket_test_support import websocket_session

_FINGERPRINT = "SHA256:abcdefghijklmnopqrstuvwxyz12345678901234567"


class _FakeSocket:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


async def _open_fake_socket(*_args, **_kwargs) -> _FakeSocket:
    return _FakeSocket()


@pytest.fixture
def auth_test_client(legacy_auth_test_client):
    """Existing successful terminal contracts require explicit legacy mode."""
    return legacy_auth_test_client


@pytest.fixture
def test_client(legacy_test_client):
    return legacy_test_client


@pytest.fixture(autouse=True)
async def legacy_service_admission():
    """Direct service tests use an honest owned compatibility admission."""
    from proxbox_api.services.interactive_policy import ExecutionPolicy, InteractiveRuntime

    async with InteractiveRuntime(ExecutionPolicy("legacy", "test-native")).admission():
        yield


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


def test_create_ssh_terminal_session_rejects_ssrf_target(
    auth_test_client,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "proxbox_api.services.ssh_terminal.get_settings",
        lambda: {"allow_private_ips": False},
    )

    response = auth_test_client.post(
        "/ssh/sessions",
        json={
            "target_type": "node",
            "endpoint_id": 1,
            "node_id": 2,
            "host": "169.254.169.254",
        },
    )

    assert response.status_code == 400
    assert "reserved/internal IP address" in response.json()["detail"]
    assert terminal_session_manager._sessions == {}


def test_asyncssh_relay_pins_host_key_instead_of_disabling_validation() -> None:
    source = Path("proxbox_api/services/ssh_terminal.py").read_text()
    assert '"known_hosts": _empty_known_hosts' in source
    assert 'known_hosts=b""' not in source
    assert '"config": []' in source
    assert '"server_host_key_algs": "default"' in source
    assert "known_hosts=None" not in source
    assert "validate_host_public_key" in source


@pytest.mark.asyncio
async def test_terminal_checks_pinned_key_before_presenting_credentials(monkeypatch) -> None:
    events: list[str] = []
    credential = TerminalCredential(
        target_type="node",
        target_id=2,
        host="10.0.0.2",
        port=22,
        username="root",
        known_host_fingerprint=_FINGERPRINT,
        password="credential-sentinel",
    )

    class FakeKey:
        def get_fingerprint(self, _algorithm="sha256"):
            return _FINGERPRINT

    async def allow_host(_host):
        return (ResolvedAddress(socket.AF_INET, "203.0.113.9"),)

    class FakeAsyncSSH:
        class SSHClient:
            pass

        class Error(Exception):
            pass

        async def connect(self, **kwargs):
            assert isinstance(kwargs["sock"], _FakeSocket)
            client = kwargs["client_factory"]()
            assert client.validate_host_public_key("10.0.0.2", None, 22, FakeKey()) is True
            events.append("key-exchange-pin")
            assert kwargs["password"] == "credential-sentinel"
            assert kwargs["config"] == []
            assert kwargs["known_hosts"]("host", "addr", 22) == ((), (), ())
            assert kwargs["x509_trusted_certs"] is None
            assert kwargs["x509_trusted_cert_paths"] == []
            assert kwargs["gss_kex"] is False
            assert kwargs["gss_auth"] is False
            assert kwargs["gss_host"] is None
            assert kwargs["agent_path"] is None
            assert kwargs["agent_identities"] is None
            assert kwargs["host_based_auth"] is False
            assert kwargs["kbdint_auth"] is False
            assert kwargs["client_host_keys"] is None
            assert kwargs["client_host_certs"] == []
            assert kwargs["client_certs"] == []
            assert kwargs["pkcs11_provider"] is None
            events.append("authenticated-connect")
            return object()

    monkeypatch.setattr(ssh_terminal, "ensure_ssh_host_allowed", allow_host)
    monkeypatch.setattr(ssh_terminal, "_open_validated_ssh_socket", _open_fake_socket)
    monkeypatch.setattr(ssh_terminal, "_load_asyncssh", lambda: FakeAsyncSSH())

    await ssh_terminal._connect_terminal(credential)

    assert events == ["key-exchange-pin", "authenticated-connect"]


@pytest.mark.asyncio
async def test_authenticated_connector_uses_one_validated_dns_answer(monkeypatch) -> None:
    resolutions = 0
    connected_hosts = []
    credential = TerminalCredential(
        target_type="node",
        target_id=2,
        host="ssh.example.test",
        port=22,
        username="root",
        known_host_fingerprint=_FINGERPRINT,
        password="credential-sentinel",
    )

    def changing_dns(*_args, **_kwargs):
        nonlocal resolutions
        resolutions += 1
        address = "8.8.8.8" if resolutions == 1 else "169.254.169.254"
        return [(2, 1, 6, "", (address, 0))]

    class FakeKey:
        def get_fingerprint(self, _algorithm="sha256"):
            return _FINGERPRINT

    class FakeAsyncSSH:
        class SSHClient:
            pass

        class Error(Exception):
            pass

        async def connect(self, **kwargs):
            connected_hosts.append(kwargs["host_key_alias"])
            client = kwargs["client_factory"]()
            assert client.validate_host_public_key("ssh.example.test", None, 22, FakeKey())
            return object()

    monkeypatch.setattr(ssh_terminal, "get_settings", lambda: {"allow_private_ips": False})
    monkeypatch.setattr("proxbox_api.ssrf.get_registered_endpoints", lambda: (set(), set()))
    monkeypatch.setattr("proxbox_api.ssrf.socket.getaddrinfo", changing_dns)
    monkeypatch.setattr(ssh_terminal, "_load_asyncssh", lambda: FakeAsyncSSH())
    connected_addresses = []

    async def open_validated(addresses, _port, *, timeout):
        connected_addresses.extend(addresses)
        return _FakeSocket()

    monkeypatch.setattr(ssh_terminal, "_open_validated_ssh_socket", open_validated)

    await ssh_terminal._connect_terminal(credential)

    assert resolutions == 1
    assert connected_addresses == [ResolvedAddress(socket.AF_INET, "8.8.8.8")]
    assert connected_hosts == ["ssh.example.test"]


@pytest.mark.asyncio
async def test_authenticated_connector_rejects_a_skipped_pin_callback(monkeypatch) -> None:
    closed = []
    credential = TerminalCredential(
        target_type="node",
        target_id=2,
        host="ssh.example.test",
        port=22,
        username="root",
        known_host_fingerprint=_FINGERPRINT,
        password="credential-sentinel",
    )

    class Connection:
        def close(self):
            closed.append("close")

        async def wait_closed(self):
            closed.append("wait_closed")

    class FakeAsyncSSH:
        class SSHClient:
            pass

        class Error(Exception):
            pass

        async def connect(self, *_args, **_kwargs):
            return Connection()

    async def allow_host(_host):
        return (ResolvedAddress(socket.AF_INET, "8.8.8.8"),)

    monkeypatch.setattr(ssh_terminal, "ensure_ssh_host_allowed", allow_host)
    monkeypatch.setattr(ssh_terminal, "_open_validated_ssh_socket", _open_fake_socket)
    monkeypatch.setattr(ssh_terminal, "_load_asyncssh", lambda: FakeAsyncSSH())

    with pytest.raises(ssh_terminal.TerminalCredentialError, match="callback was not invoked"):
        await ssh_terminal._connect_terminal(credential)

    assert closed == ["close", "wait_closed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("first_error", [OSError("refused"), asyncio.TimeoutError()])
async def test_authenticated_connector_falls_back_before_authentication(
    monkeypatch,
    first_error,
) -> None:
    attempts = []
    auth_events = []
    credential = TerminalCredential(
        "node",
        2,
        "ssh.example.test",
        2222,
        "root",
        _FINGERPRINT,
        password="credential-sentinel",
    )

    class Socket:
        def setblocking(self, _value):
            pass

        def close(self):
            pass

    class Key:
        def get_fingerprint(self, _algorithm="sha256"):
            return _FINGERPRINT

    class FakeAsyncSSH:
        class SSHClient:
            pass

        class Error(Exception):
            pass

        async def connect(self, **kwargs):
            client = kwargs["client_factory"]()
            assert client.validate_host_public_key(credential.host, None, credential.port, Key())
            auth_events.append(kwargs["password"])
            return object()

    async def allow_host(_host):
        return (
            ResolvedAddress(socket.AF_INET, "8.8.8.8"),
            ResolvedAddress(socket.AF_INET, "1.1.1.1"),
        )

    async def sock_connect(_sock, address):
        attempts.append(address)
        if len(attempts) == 1:
            raise first_error

    monkeypatch.setattr(ssh_terminal, "ensure_ssh_host_allowed", allow_host)
    monkeypatch.setattr(ssh_terminal.socket, "socket", lambda *_args: Socket())
    monkeypatch.setattr(asyncio.get_running_loop(), "sock_connect", sock_connect)
    monkeypatch.setattr(ssh_terminal, "_load_asyncssh", lambda: FakeAsyncSSH())

    await ssh_terminal._connect_terminal(credential)

    assert attempts == [("8.8.8.8", 2222), ("1.1.1.1", 2222)]
    assert auth_events == ["credential-sentinel"]


@pytest.mark.asyncio
async def test_pin_failure_does_not_fall_through_to_another_validated_address(
    monkeypatch,
) -> None:
    attempts = []
    credential = TerminalCredential(
        "node",
        2,
        "ssh.example.test",
        2222,
        "root",
        _FINGERPRINT,
        password="credential-sentinel",
    )

    class Socket:
        def setblocking(self, _value):
            pass

        def close(self):
            pass

    class WrongKey:
        def get_fingerprint(self, _algorithm="sha256"):
            return "SHA256:wrong-key"

    class FakeAsyncSSH:
        class SSHClient:
            pass

        class Error(Exception):
            pass

        async def connect(self, **kwargs):
            client = kwargs["client_factory"]()
            assert not client.validate_host_public_key(
                credential.host, None, credential.port, WrongKey()
            )
            raise self.Error("host key rejected")

    async def allow_host(_host):
        return (
            ResolvedAddress(socket.AF_INET, "8.8.8.8"),
            ResolvedAddress(socket.AF_INET, "1.1.1.1"),
        )

    async def sock_connect(_sock, address):
        attempts.append(address)

    monkeypatch.setattr(ssh_terminal, "ensure_ssh_host_allowed", allow_host)
    monkeypatch.setattr(ssh_terminal.socket, "socket", lambda *_args: Socket())
    monkeypatch.setattr(asyncio.get_running_loop(), "sock_connect", sock_connect)
    monkeypatch.setattr(ssh_terminal, "_load_asyncssh", lambda: FakeAsyncSSH())

    with pytest.raises(ssh_terminal.TerminalCredentialError, match="does not match"):
        await ssh_terminal._connect_terminal(credential)

    assert attempts == [("8.8.8.8", 2222)]


@pytest.mark.asyncio
async def test_ssh_socket_connect_uses_complete_scoped_ipv6_sockaddr(monkeypatch) -> None:
    attempts = []
    families = []

    class Socket:
        def __init__(self, family, _kind):
            families.append(family)

        def setblocking(self, _value):
            pass

        def close(self):
            pass

    async def sock_connect(_sock, address):
        attempts.append(address)

    monkeypatch.setattr(ssh_terminal.socket, "socket", Socket)
    monkeypatch.setattr(asyncio.get_running_loop(), "sock_connect", sock_connect)

    await ssh_terminal._open_validated_ssh_socket(
        (ResolvedAddress(socket.AF_INET6, "fe80::1", flowinfo=3, scope_id=7),),
        2222,
    )

    assert families == [socket.AF_INET6]
    assert attempts == [("fe80::1", 2222, 3, 7)]


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

    with websocket_session(auth_test_client, created["websocket_path"]) as websocket:
        websocket.send_json({"type": "auth", "ticket": "wrong"})
        frame = websocket.receive_json()
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()

    assert frame["type"] == "error"
    assert "Invalid SSH terminal ticket" in frame["message"]
    assert closed.value.code == 4001


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

    async def fake_fetch_terminal_credential(session):
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
        "proxbox_api.routes.ssh_terminal._resolve_terminal_credential",
        fake_fetch_terminal_credential,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.ssh_terminal.connect_and_relay",
        fake_connect_and_relay,
    )

    with websocket_session(auth_test_client, created["websocket_path"]) as websocket:
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
