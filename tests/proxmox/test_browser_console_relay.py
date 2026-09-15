"""Standalone encrypted browser relay coverage for Proxmox consoles."""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.ext.asyncio.session import AsyncSession
from starlette.websockets import WebSocketDisconnect
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus, SecurityError
from websockets.http11 import Response

from proxbox_api import credentials, database
from proxbox_api.database import BrowserConsoleRelaySession, ProxmoxEndpoint
from proxbox_api.routes.proxmox import console
from proxbox_api.services import console_relay, console_relay_policy

ORIGIN = "https://netbox.example"
TICKET_CANARY = "PVE-ticket-secret-canary"
AUTH_CANARY = "PVEAPIToken=relay@pve!browser=auth-secret-canary"


@pytest.fixture(autouse=True)
def relay_encryption(monkeypatch):
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "browser-relay-test-key")
    credentials.reset_encryption_cache()
    yield
    credentials.reset_encryption_cache()


def _make_endpoint(
    db_engine,
    *,
    enabled: bool = True,
    ip_address: str = "pve.example.test",
) -> int:
    with Session(db_engine) as session:
        endpoint = ProxmoxEndpoint(
            name=f"relay-pve-{time.time_ns()}",
            ip_address=ip_address,
            port=8006,
            username="root@pam",
            verify_ssl=False,
            enabled=enabled,
        )
        session.add(endpoint)
        session.commit()
        session.refresh(endpoint)
        assert endpoint.id is not None
        return endpoint.id


def _private_response(
    console_type: str = "term",
    *,
    ws_url: str | None = None,
) -> console.ConsoleSessionResponse:
    return console.ConsoleSessionResponse(
        ticket=TICKET_CANARY,
        port=5900,
        proxmox_host="pve.example.test",
        proxmox_port=8006,
        ws_url=ws_url
        or (
            "wss://pve.example.test:8006/api2/json/nodes/pve01/qemu/100/"
            f"vncwebsocket?port=5900&vncticket={TICKET_CANARY}"
        ),
        console_type=console_type,
        verify_ssl=False,
        websocket_auth=console.ConsoleWebSocketAuth(
            kind="authorization",
            value=AUTH_CANARY,
        ),
    )


def _request(endpoint_id: int, *, vm_type: str = "qemu", console_type: str = "term") -> dict:
    return {
        "endpoint_id": endpoint_id,
        "vmid": 100,
        "node": "pve01",
        "vm_type": vm_type,
        "console_type": console_type,
        "origin": ORIGIN,
    }


def _browser_protocols(created: dict) -> list[str]:
    return ["binary", f"{console_relay.TOKEN_SUBPROTOCOL_PREFIX}{created['stream_token']}"]


def _relay_payload(**overrides) -> console_relay.ConsoleRelayPayload:
    values = {
        "endpoint_id": 1,
        "vmid": 100,
        "node": "pve01",
        "vm_type": "qemu",
        "console_type": "term",
        "origin": ORIGIN,
        "ws_url": "wss://pve.example/console",
        "ticket": TICKET_CANARY,
        "verify_ssl": True,
        "auth_kind": "authorization",
        "auth_value": AUTH_CANARY,
    }
    values.update(overrides)
    return console_relay.ConsoleRelayPayload(**values)


@asynccontextmanager
async def _sessions(db_engine):
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("vm_type", "console_type"),
    [("qemu", "novnc"), ("qemu", "term"), ("lxc", "term")],
)
def test_browser_create_supports_exact_console_matrix(
    monkeypatch,
    auth_test_client,
    db_engine,
    vm_type,
    console_type,
) -> None:
    endpoint_id = _make_endpoint(db_engine)
    broker = AsyncMock(return_value=_private_response(console_type))
    monkeypatch.setattr(console, "_create_console_session_for_endpoint", broker)

    response = auth_test_client.post(
        "/proxmox/console/browser-sessions",
        json=_request(endpoint_id, vm_type=vm_type, console_type=console_type),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"stream_token", "websocket_path", "expires_at", "console_type"}
    assert len(body["stream_token"]) == console_relay.TOKEN_LENGTH
    assert body["websocket_path"] == "/proxmox/console/browser-stream"
    assert body["stream_token"] not in body["websocket_path"]
    assert body["console_type"] == console_type
    assert TICKET_CANARY not in response.text
    assert AUTH_CANARY not in response.text
    broker.assert_awaited_once()


@pytest.mark.parametrize(
    "mutation",
    [
        {"vm_type": "openvz"},
        {"vm_type": "lxc", "console_type": "novnc"},
        {"origin": "http://netbox.example"},
        {"origin": "https://netbox.example/path"},
        {"origin": "https://user@netbox.example"},
        {"origin": "https://netbox.example/"},
        {"node": "pve01/../../access"},
    ],
)
def test_browser_create_rejects_invalid_type_and_origin(
    auth_test_client,
    db_engine,
    mutation,
) -> None:
    payload = _request(_make_endpoint(db_engine))
    payload.update(mutation)

    response = auth_test_client.post("/proxmox/console/browser-sessions", json=payload)

    assert response.status_code == 422
    assert TICKET_CANARY not in response.text


def test_browser_create_fails_closed_without_encryption(
    monkeypatch,
    auth_test_client,
    db_engine,
) -> None:
    endpoint_id = _make_endpoint(db_engine)
    broker = AsyncMock(return_value=_private_response())
    monkeypatch.setattr(console, "_create_console_session_for_endpoint", broker)
    monkeypatch.setattr(console_relay, "is_encryption_enabled", lambda: False)

    response = auth_test_client.post(
        "/proxmox/console/browser-sessions", json=_request(endpoint_id)
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Browser console relay is unavailable."}
    broker.assert_not_awaited()


@pytest.mark.parametrize("failure", ["encrypt", "decrypt", "wrong_plaintext"])
def test_browser_encryption_preflight_failures_are_sanitized_before_broker(
    monkeypatch,
    caplog,
    auth_test_client,
    db_engine,
    failure,
) -> None:
    endpoint_id = _make_endpoint(db_engine)
    canary = f"{failure}-preflight-secret-canary"
    broker = AsyncMock(return_value=_private_response())
    monkeypatch.setattr(console, "_create_console_session_for_endpoint", broker)
    if failure == "encrypt":
        monkeypatch.setattr(
            console_relay,
            "encrypt_value",
            Mock(side_effect=RuntimeError(canary)),
        )
    else:
        monkeypatch.setattr(console_relay, "encrypt_value", Mock(return_value="enc:opaque"))
        result = (
            Mock(side_effect=RuntimeError(canary))
            if failure == "decrypt"
            else Mock(return_value=canary)
        )
        monkeypatch.setattr(console_relay, "decrypt_value", result)

    response = auth_test_client.post(
        "/proxmox/console/browser-sessions",
        json=_request(endpoint_id),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Browser console relay is unavailable."}
    broker.assert_not_awaited()
    assert canary not in response.text
    assert canary not in caplog.text


def test_browser_session_keeps_bracketed_ipv6_upstream_url_encrypted(
    monkeypatch,
    auth_test_client,
    db_engine,
) -> None:
    endpoint_id = _make_endpoint(db_engine, ip_address="2001:db8::8")
    private_url = console._build_ws_url(
        "2001:db8::8",
        8006,
        "pve.ipv6-01",
        "qemu",
        100,
        TICKET_CANARY,
        5900,
    )
    monkeypatch.setattr(
        console,
        "_create_console_session_for_endpoint",
        AsyncMock(return_value=_private_response(ws_url=private_url)),
    )

    response = auth_test_client.post(
        "/proxmox/console/browser-sessions",
        json={**_request(endpoint_id), "node": "pve.ipv6-01"},
    )

    assert response.status_code == 201, response.text
    assert "2001:db8" not in response.text
    with Session(db_engine) as session:
        row = session.exec(select(BrowserConsoleRelaySession)).one()
    decrypted = credentials.decrypt_value(row.encrypted_payload)
    assert decrypted is not None
    assert "wss://[2001:db8::8]:8006/" in decrypted


def test_browser_create_rejects_disabled_endpoint_but_private_route_is_unchanged(
    monkeypatch,
    auth_test_client,
    db_engine,
) -> None:
    endpoint_id = _make_endpoint(db_engine, enabled=False)
    broker = AsyncMock(return_value=_private_response())
    monkeypatch.setattr(console, "_create_console_session_for_endpoint", broker)

    browser = auth_test_client.post("/proxmox/console/browser-sessions", json=_request(endpoint_id))
    private = auth_test_client.post(
        "/proxmox/console/sessions",
        json={key: value for key, value in _request(endpoint_id).items() if key != "origin"},
    )

    assert browser.status_code == 403
    assert private.status_code == 200
    assert broker.await_count == 1


def test_private_material_is_encrypted_and_absent_from_browser_database_and_logs(
    monkeypatch,
    caplog,
    auth_test_client,
    db_engine,
) -> None:
    endpoint_id = _make_endpoint(db_engine)
    monkeypatch.setattr(
        console, "_create_console_session_for_endpoint", AsyncMock(return_value=_private_response())
    )

    response = auth_test_client.post(
        "/proxmox/console/browser-sessions", json=_request(endpoint_id)
    )

    assert response.status_code == 201
    with Session(db_engine) as session:
        row = session.exec(select(BrowserConsoleRelaySession)).one()
        assert row.encrypted_payload.startswith("enc:")
        assert TICKET_CANARY not in row.encrypted_payload
        assert AUTH_CANARY not in row.encrypted_payload
    combined = response.text + caplog.text
    assert TICKET_CANARY not in combined
    assert AUTH_CANARY not in combined


def test_browser_create_sanitizes_upstream_failure(
    monkeypatch,
    caplog,
    auth_test_client,
    db_engine,
) -> None:
    endpoint_id = _make_endpoint(db_engine)
    secret = "upstream-error-secret-canary"
    monkeypatch.setattr(
        console,
        "_create_console_session_for_endpoint",
        AsyncMock(side_effect=HTTPException(status_code=502, detail=secret)),
    )

    response = auth_test_client.post(
        "/proxmox/console/browser-sessions", json=_request(endpoint_id)
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "Browser console session is unavailable."}
    assert secret not in response.text
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_atomic_one_use_consume_across_independent_sessions(db_engine) -> None:
    payload = _relay_payload()
    async with _sessions(db_engine) as factory:
        async with factory() as creator:
            token, _ = await console_relay.create_relay_session(creator, payload)

        async def consume_once():
            async with factory() as candidate:
                return await console_relay.consume_relay_session(candidate, token)

        results = await asyncio.gather(consume_once(), consume_once(), return_exceptions=True)

    assert sum(isinstance(result, console_relay.ConsoleRelayPayload) for result in results) == 1
    assert sum(isinstance(result, console_relay.ConsoleRelayRejected) for result in results) == 1


@pytest.mark.asyncio
async def test_concurrent_nonexistent_tokens_never_acquire_write_transaction(db_engine) -> None:
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    statements: list[str] = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record_statement)
    tokens = [console_relay._new_stream_token() for _ in range(16)]
    try:

        async def consume_missing(token: str):
            async with factory() as candidate:
                return await console_relay.consume_relay_session(candidate, token)

        results = await asyncio.gather(
            *(consume_missing(token) for token in tokens),
            return_exceptions=True,
        )
    finally:
        await engine.dispose()

    assert all(isinstance(result, console_relay.ConsoleRelayRejected) for result in results)
    assert any("SELECT" in statement.upper() for statement in statements)
    assert all("BEGIN IMMEDIATE" not in statement.upper() for statement in statements)


@pytest.mark.asyncio
async def test_expiry_replay_and_malformed_ciphertext_payload(db_engine) -> None:
    payload = _relay_payload(auth_kind="cookie", auth_value="PVEAuthCookie=secret")
    async with _sessions(db_engine) as factory:
        async with factory() as creator:
            replay_token, _ = await console_relay.create_relay_session(creator, payload, now=10)
        async with factory() as consumer:
            await console_relay.consume_relay_session(consumer, replay_token, now=11)
        async with factory() as replay:
            with pytest.raises(console_relay.ConsoleRelayRejected):
                await console_relay.consume_relay_session(replay, replay_token, now=12)

        async with factory() as creator:
            expired_token, _ = await console_relay.create_relay_session(creator, payload, now=20)
        async with factory() as expired:
            with pytest.raises(console_relay.ConsoleRelayRejected):
                await console_relay.consume_relay_session(
                    expired,
                    expired_token,
                    now=20 + console_relay.TOKEN_TTL_SECONDS,
                )

        malformed_token = console_relay._new_stream_token()
        malformed = credentials.encrypt_value("{not-json")
        assert malformed is not None
        async with factory() as creator:
            creator.add(
                BrowserConsoleRelaySession(
                    token_digest=console_relay._token_digest(malformed_token),
                    encrypted_payload=malformed,
                    created_at=30,
                    expires_at=60,
                )
            )
            await creator.commit()
        async with factory() as consumer:
            with pytest.raises(console_relay.ConsoleRelayRejected):
                await console_relay.consume_relay_session(consumer, malformed_token, now=31)


@pytest.mark.asyncio
async def test_token_count_and_length_bounds(db_engine, monkeypatch) -> None:
    monkeypatch.setattr(console_relay, "MAX_ACTIVE_TOKENS", 1)
    async with _sessions(db_engine) as factory:
        async with factory() as creator:
            token, _ = await console_relay.create_relay_session(creator, _relay_payload(), now=10)
        assert len(token) == console_relay.TOKEN_LENGTH
        async with factory() as creator:
            with pytest.raises(console_relay.ConsoleRelayUnavailable):
                await console_relay.create_relay_session(creator, _relay_payload(), now=11)
        async with factory() as consumer:
            with pytest.raises(console_relay.ConsoleRelayRejected):
                await console_relay.consume_relay_session(consumer, "short", now=11)


def test_relay_table_is_discovered_and_exact_schema_is_validated(db_engine, tmp_path) -> None:
    assert BrowserConsoleRelaySession.__tablename__ in SQLModel.metadata.tables
    assert inspect(db_engine).has_table(BrowserConsoleRelaySession.__tablename__)
    database.validate_browser_console_relay_schema(db_engine)

    malformed_engine = create_engine(f"sqlite:///{tmp_path / 'malformed-relay.db'}")
    try:
        with malformed_engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE browser_console_relay_session (token_digest VARCHAR PRIMARY KEY)"
                )
            )
        with pytest.raises(database.DatabaseStartupError):
            database.validate_browser_console_relay_schema(malformed_engine)
    finally:
        malformed_engine.dispose()

    missing_index_engine = create_engine(f"sqlite:///{tmp_path / 'missing-index.db'}")
    try:
        with missing_index_engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE browser_console_relay_session ("
                    "token_digest VARCHAR(64) NOT NULL PRIMARY KEY, "
                    "encrypted_payload VARCHAR(16384) NOT NULL, "
                    "created_at FLOAT NOT NULL, expires_at FLOAT NOT NULL)"
                )
            )
        with pytest.raises(database.DatabaseStartupError, match="expiry index"):
            database.validate_browser_console_relay_schema(missing_index_engine)
    finally:
        missing_index_engine.dispose()


@pytest.mark.parametrize(
    "index_columns",
    ["expires_at", "expires_at, created_at"],
)
def test_relay_schema_rejects_unique_or_composite_expiry_index(
    tmp_path,
    index_columns,
) -> None:
    suffix = index_columns.replace(", ", "-")
    candidate = create_engine(f"sqlite:///{tmp_path / f'invalid-index-{suffix}.db'}")
    try:
        with candidate.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE browser_console_relay_session ("
                    "token_digest VARCHAR(64) NOT NULL PRIMARY KEY, "
                    "encrypted_payload VARCHAR(16384) NOT NULL, "
                    "created_at FLOAT NOT NULL, expires_at FLOAT NOT NULL)"
                )
            )
            index_kind = "UNIQUE " if index_columns == "expires_at" else ""
            connection.execute(
                text(
                    f"CREATE {index_kind}INDEX invalid_expiry_index "
                    f"ON browser_console_relay_session ({index_columns})"
                )
            )
        with pytest.raises(database.DatabaseStartupError, match="expiry index"):
            database.validate_browser_console_relay_schema(candidate)
    finally:
        candidate.dispose()


@pytest.mark.parametrize(
    "overrides",
    [
        {"ticket": "x" * (console_relay.MAX_TICKET_LENGTH + 1)},
        {"auth_value": "x" * (console_relay.MAX_AUTH_HEADER_LENGTH + 1)},
        {"auth_value": "valid\r\ninjected: value"},
        {"ws_url": "ws://pve.example/console"},
    ],
)
def test_private_payload_lengths_headers_and_wss_are_bounded(overrides) -> None:
    with pytest.raises(ValueError):
        _relay_payload(**overrides)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "binary",
        f"{console_relay.TOKEN_SUBPROTOCOL_PREFIX}{'a' * console_relay.TOKEN_LENGTH}",
        f"binary, {console_relay.TOKEN_SUBPROTOCOL_PREFIX}short",
        f"binary, {console_relay.TOKEN_SUBPROTOCOL_PREFIX}{'a' * 42}.",
        (
            f"binary, {console_relay.TOKEN_SUBPROTOCOL_PREFIX}{'a' * console_relay.TOKEN_LENGTH}, "
            f"{console_relay.TOKEN_SUBPROTOCOL_PREFIX}{'b' * console_relay.TOKEN_LENGTH}"
        ),
        (
            f"binary, binary, "
            f"{console_relay.TOKEN_SUBPROTOCOL_PREFIX}{'a' * console_relay.TOKEN_LENGTH}"
        ),
        (
            f"binary, {console_relay.TOKEN_SUBPROTOCOL_PREFIX}"
            f"{'a' * console_relay.TOKEN_LENGTH}, unexpected"
        ),
    ],
)
def test_browser_subprotocol_parser_rejects_missing_duplicate_malformed_and_extra(
    header,
) -> None:
    with pytest.raises(console_relay.ConsoleRelayRejected):
        console_relay.parse_browser_subprotocols(header)


def test_browser_subprotocol_parser_accepts_exact_binary_and_token_offer() -> None:
    token = console_relay._new_stream_token()

    parsed = console_relay.parse_browser_subprotocols(
        f"{console_relay.TOKEN_SUBPROTOCOL_PREFIX}{token}, binary"
    )

    assert parsed == token


def _redirect_error(status: int, location: str) -> InvalidStatus:
    return InvalidStatus(Response(status, "Redirect", Headers(Location=location)))


@pytest.mark.parametrize("status", [300, 301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    "location",
    ["/same-origin", "wss://hostile.example/credential-capture"],
)
def test_upstream_connector_refuses_all_redirects(status, location) -> None:
    connector = console_relay._NoRedirectConnect("wss://pve.example/console")

    result = connector.process_redirect(_redirect_error(status, location))

    assert isinstance(result, SecurityError)
    assert str(result) == "Upstream WebSocket redirects are disabled."
    assert location not in str(result)


class _RedirectingConnection:
    def __init__(self, error: InvalidStatus) -> None:
        self.error = error
        self.transport = Mock()
        self.seen_headers = None

    async def handshake(self, additional_headers, _user_agent_header) -> None:
        self.seen_headers = additional_headers
        raise self.error


@pytest.mark.asyncio
async def test_cross_origin_redirect_never_dials_target_or_replays_credentials(
    monkeypatch,
) -> None:
    redirect_target = "wss://hostile.example/credential-capture"
    source = _RedirectingConnection(_redirect_error(302, redirect_target))
    hostile = Mock()
    connector = console_relay._NoRedirectConnect(
        "wss://pve.example/console",
        additional_headers={"Authorization": AUTH_CANARY},
        proxy=None,
    )
    dial = AsyncMock(side_effect=[source, hostile])
    monkeypatch.setattr(connector, "create_connection", dial)

    with pytest.raises(SecurityError, match="Upstream WebSocket redirects are disabled"):
        await connector

    assert dial.await_count == 1
    assert source.seen_headers == {"Authorization": AUTH_CANARY}
    source.transport.abort.assert_called_once_with()
    assert AUTH_CANARY not in str(hostile.mock_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("verify_ssl", [True, False])
async def test_upstream_negotiates_binary_and_preserves_tls_policy(
    monkeypatch,
    verify_ssl,
) -> None:
    upstream = _FakeUpstream()
    connector = AsyncMock(return_value=upstream)
    monkeypatch.setattr(console_relay, "_NoRedirectConnect", connector)

    result = await console_relay.open_upstream(_relay_payload(verify_ssl=verify_ssl))

    assert result is upstream
    kwargs = connector.await_args.kwargs
    assert kwargs["subprotocols"] == ["binary"]
    assert kwargs["proxy"] is None
    assert kwargs["compression"] is None
    assert kwargs["max_size"] == console_relay.MAX_RELAY_FRAME_SIZE
    assert kwargs["ssl"].check_hostname is verify_ssl
    assert kwargs["ssl"].verify_mode == (ssl.CERT_REQUIRED if verify_ssl else ssl.CERT_NONE)


class _FakeUpstream:
    def __init__(self, messages: list[str | bytes] | None = None) -> None:
        self.subprotocol = "binary"
        self.messages: asyncio.Queue[str | bytes] = asyncio.Queue()
        for message in messages or []:
            self.messages.put_nowait(message)
        self.sent: list[str | bytes] = []
        self.closed = False
        self.recv_cancelled = False
        self.recv_started = asyncio.Event()

    async def recv(self) -> str | bytes:
        self.recv_started.set()
        try:
            return await self.messages.get()
        except asyncio.CancelledError:
            self.recv_cancelled = True
            raise

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True


class _HandshakeBrowser:
    def __init__(self, messages: list[bytes]) -> None:
        self.messages = list(messages)
        self.sent: list[bytes] = []

    async def receive(self) -> dict:
        return {"type": "websocket.receive", "bytes": self.messages.pop(0)}

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_rfb_38_auth_is_mediated_server_side() -> None:
    challenge = bytes(range(16))
    upstream = _FakeUpstream([b"RFB 003.008\n", b"\x01\x02", challenge, b"\x00\x00\x00\x00"])
    browser = _HandshakeBrowser([b"RFB 003.008\n", b"\x01"])

    await console_relay.mediate_rfb_auth(browser, upstream, TICKET_CANARY)

    assert browser.sent == [b"RFB 003.008\n", b"\x01\x01", b"\x00\x00\x00\x00"]
    assert upstream.sent[0:2] == [b"RFB 003.008\n", b"\x02"]
    assert len(upstream.sent[2]) == 16
    assert TICKET_CANARY.encode() not in upstream.sent


@pytest.mark.asyncio
async def test_rfb_handshake_rejects_oversized_frame() -> None:
    upstream = _FakeUpstream([b"x" * (console_relay.MAX_HANDSHAKE_FRAME_SIZE + 1)])
    browser = _HandshakeBrowser([])

    with pytest.raises(console_relay.ConsoleRelayProtocolError):
        await console_relay.mediate_rfb_auth(browser, upstream, TICKET_CANARY)


class _RelayBrowser:
    def __init__(self) -> None:
        self.sent: list[str | bytes] = []
        self.upstream_frames_done = asyncio.Event()
        self._received = 0

    async def receive(self) -> dict:
        self._received += 1
        if self._received == 1:
            return {"type": "websocket.receive", "bytes": b"client-binary"}
        if self._received == 2:
            return {"type": "websocket.receive", "text": "client-text"}
        await self.upstream_frames_done.wait()
        return {"type": "websocket.disconnect"}

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)
        if len(self.sent) == 2:
            self.upstream_frames_done.set()

    async def send_text(self, data: str) -> None:
        self.sent.append(data)
        if len(self.sent) == 2:
            self.upstream_frames_done.set()


@pytest.mark.asyncio
async def test_bidirectional_frames_and_peer_task_cleanup() -> None:
    browser = _RelayBrowser()
    upstream = _FakeUpstream([b"server-binary", "server-text"])

    await console_relay.relay_frames(browser, upstream)

    assert upstream.sent == [b"client-binary", "client-text"]
    assert browser.sent == [b"server-binary", "server-text"]
    assert upstream.recv_cancelled is True


class _BlockingRelayBrowser:
    def __init__(self) -> None:
        self.receive_cancelled = False
        self.receive_started = asyncio.Event()

    async def receive(self) -> dict:
        self.receive_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.receive_cancelled = True
            raise
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_relay_cancellation_awaits_both_peer_tasks() -> None:
    browser = _BlockingRelayBrowser()
    upstream = _FakeUpstream()
    relay = asyncio.create_task(console_relay.relay_frames(browser, upstream))
    await asyncio.sleep(0)

    relay.cancel()
    with pytest.raises(asyncio.CancelledError):
        await relay

    assert browser.receive_cancelled is True
    assert upstream.recv_cancelled is True


class _OutputOnlyBrowser(_BlockingRelayBrowser):
    def __init__(self, expected_frames: int) -> None:
        super().__init__()
        self.expected_frames = expected_frames
        self.sent: list[str | bytes] = []
        self.complete = asyncio.Event()

    async def receive(self) -> dict:
        await self.complete.wait()
        return {"type": "websocket.disconnect"}

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(data)
        if len(self.sent) == self.expected_frames:
            self.complete.set()

    async def send_text(self, data: str) -> None:
        self.sent.append(data)
        if len(self.sent) == self.expected_frames:
            self.complete.set()


class _ControlledRelayActivity:
    def __init__(self) -> None:
        self.touches = 0
        self.idle = asyncio.Event()

    def touch(self) -> None:
        self.touches += 1

    async def wait_until_idle(self) -> None:
        await self.idle.wait()


@pytest.mark.asyncio
async def test_output_only_activity_refreshes_shared_idle_deadline(monkeypatch) -> None:
    activity = _ControlledRelayActivity()
    monkeypatch.setattr(console_relay, "_RelayActivity", lambda _timeout: activity)
    browser = _OutputOnlyBrowser(expected_frames=4)
    upstream = _FakeUpstream([b"one", "two", b"three", "four"])

    await console_relay.relay_frames(browser, upstream)

    assert browser.sent == [b"one", "two", b"three", "four"]
    assert activity.touches == 4


class _InputOnlyBrowser:
    def __init__(self) -> None:
        self.frames = iter([b"one", "two", b"three", "four"])

    async def receive(self) -> dict:
        try:
            frame = next(self.frames)
        except StopIteration:
            return {"type": "websocket.disconnect"}
        key = "bytes" if isinstance(frame, bytes) else "text"
        return {"type": "websocket.receive", key: frame}


@pytest.mark.asyncio
async def test_input_only_activity_refreshes_shared_idle_deadline(monkeypatch) -> None:
    activity = _ControlledRelayActivity()
    monkeypatch.setattr(console_relay, "_RelayActivity", lambda _timeout: activity)
    browser = _InputOnlyBrowser()
    upstream = _FakeUpstream()

    await console_relay.relay_frames(browser, upstream)

    assert upstream.sent == [b"one", "two", b"three", "four"]
    assert upstream.recv_cancelled is True
    assert activity.touches == 4


@pytest.mark.asyncio
async def test_bidirectional_inactivity_ends_and_settles_both_pumps(monkeypatch) -> None:
    activity = _ControlledRelayActivity()
    monkeypatch.setattr(console_relay, "_RelayActivity", lambda _timeout: activity)
    browser = _BlockingRelayBrowser()
    upstream = _FakeUpstream()

    relay = asyncio.create_task(console_relay.relay_frames(browser, upstream))
    await browser.receive_started.wait()
    await upstream.recv_started.wait()
    activity.idle.set()
    await relay

    assert browser.receive_cancelled is True
    assert upstream.recv_cancelled is True


@pytest.mark.asyncio
async def test_shared_idle_deadline_refresh_is_deterministic(monkeypatch) -> None:
    now = [0.0]
    waits: list[float] = []
    activity = console_relay._RelayActivity(300, clock=lambda: now[0])

    async def controlled_wait(timeout: float) -> bool:
        waits.append(timeout)
        if len(waits) == 1:
            now[0] = 250
            activity.touch()
            return True
        now[0] = 550
        return False

    monkeypatch.setattr(activity, "_wait_for_change", controlled_wait)

    await activity.wait_until_idle()

    assert waits == [300, 300]


def _create_browser_ticket(monkeypatch, auth_test_client, db_engine, *, console_type="term"):
    endpoint_id = _make_endpoint(db_engine)
    monkeypatch.setattr(
        console,
        "_create_console_session_for_endpoint",
        AsyncMock(return_value=_private_response(console_type)),
    )
    response = auth_test_client.post(
        "/proxmox/console/browser-sessions",
        json=_request(endpoint_id, console_type=console_type),
    )
    assert response.status_code == 201
    return response.json()


def test_mounted_websocket_keeps_token_out_of_uri_and_access_log(
    monkeypatch,
    caplog,
    auth_test_client,
    db_engine,
) -> None:
    created = _create_browser_ticket(monkeypatch, auth_test_client, db_engine)
    upstream = _FakeUpstream()
    observed_targets: list[str] = []
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    monkeypatch.setattr(console_relay, "open_upstream", AsyncMock(return_value=upstream))

    async def mounted_relay(websocket, _upstream):
        target = websocket.scope["path"]
        query = websocket.scope["query_string"]
        if query:
            target = f"{target}?{query.decode('ascii')}"
        observed_targets.append(target)
        logging.getLogger("uvicorn.access").info('WebSocket "%s"', target)
        await websocket.send_bytes(b"binary-frame")
        await websocket.send_text("text-frame")

    monkeypatch.setattr(console_relay, "relay_frames", mounted_relay)
    with auth_test_client.websocket_connect(
        created["websocket_path"],
        headers={"origin": ORIGIN},
        subprotocols=_browser_protocols(created),
    ) as websocket:
        assert websocket.accepted_subprotocol == "binary"
        assert websocket.receive_bytes() == b"binary-frame"
        assert websocket.receive_text() == "text-frame"
    assert upstream.closed is True
    assert observed_targets == ["/proxmox/console/browser-stream"]
    assert created["stream_token"] not in created["websocket_path"]
    assert created["stream_token"] not in caplog.text

    with pytest.raises(WebSocketDisconnect) as replay:
        with auth_test_client.websocket_connect(
            created["websocket_path"],
            headers={"origin": ORIGIN},
            subprotocols=_browser_protocols(created),
        ):
            pass
    assert replay.value.code == 1008


def test_mounted_query_token_form_is_rejected_before_upstream_access(
    monkeypatch,
    auth_test_client,
    db_engine,
) -> None:
    created = _create_browser_ticket(monkeypatch, auth_test_client, db_engine)
    consumer = AsyncMock(wraps=console_relay.consume_relay_session)
    opener = AsyncMock()
    monkeypatch.setattr(console_relay, "consume_relay_session", consumer)
    monkeypatch.setattr(console_relay, "open_upstream", opener)

    with pytest.raises(WebSocketDisconnect) as rejected:
        with auth_test_client.websocket_connect(
            f"{created['websocket_path']}?token={created['stream_token']}",
            headers={"origin": ORIGIN},
            subprotocols=_browser_protocols(created),
        ):
            pass

    assert rejected.value.code == 1008
    consumer.assert_not_awaited()
    opener.assert_not_awaited()

    upstream = _FakeUpstream()
    opener.return_value = upstream
    monkeypatch.setattr(console_relay, "relay_frames", AsyncMock())
    with auth_test_client.websocket_connect(
        created["websocket_path"],
        headers={"origin": ORIGIN},
        subprotocols=_browser_protocols(created),
    ) as websocket:
        assert websocket.accepted_subprotocol == "binary"
    consumer.assert_awaited_once()
    opener.assert_awaited_once()


def test_mounted_invalid_protocol_offers_stop_before_consume_or_upstream(
    monkeypatch,
    auth_test_client,
) -> None:
    token = console_relay._new_stream_token()
    token_protocol = f"{console_relay.TOKEN_SUBPROTOCOL_PREFIX}{token}"
    other_token_protocol = (
        f"{console_relay.TOKEN_SUBPROTOCOL_PREFIX}{console_relay._new_stream_token()}"
    )
    offers = [
        [],
        ["binary", token_protocol, token_protocol],
        ["binary", f"{console_relay.TOKEN_SUBPROTOCOL_PREFIX}malformed"],
        ["binary", token_protocol, other_token_protocol],
        ["binary", token_protocol, "unexpected"],
    ]
    consumer = AsyncMock()
    opener = AsyncMock()
    monkeypatch.setattr(console_relay, "consume_relay_session", consumer)
    monkeypatch.setattr(console_relay, "open_upstream", opener)

    for subprotocols in offers:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with auth_test_client.websocket_connect(
                "/proxmox/console/browser-stream",
                headers={"origin": ORIGIN},
                subprotocols=subprotocols,
            ):
                pass
        assert rejected.value.code == 1008

    consumer.assert_not_awaited()
    opener.assert_not_awaited()


@pytest.mark.parametrize(
    ("headers", "protocol_case"),
    [({"origin": "https://other.example"}, "valid"), ({"origin": ORIGIN}, "none"), ({}, "valid")],
)
def test_mounted_websocket_requires_exact_origin_and_binary_subprotocol(
    monkeypatch,
    auth_test_client,
    db_engine,
    headers,
    protocol_case,
) -> None:
    created = _create_browser_ticket(monkeypatch, auth_test_client, db_engine)
    subprotocols = _browser_protocols(created) if protocol_case == "valid" else []
    opener = AsyncMock()
    monkeypatch.setattr(console_relay, "open_upstream", opener)

    with pytest.raises(WebSocketDisconnect) as rejected:
        with auth_test_client.websocket_connect(
            created["websocket_path"], headers=headers, subprotocols=subprotocols
        ):
            pass

    assert rejected.value.code == 1008
    opener.assert_not_awaited()


def test_mounted_qemu_novnc_invokes_rfb_mediation(
    monkeypatch,
    auth_test_client,
    db_engine,
) -> None:
    created = _create_browser_ticket(monkeypatch, auth_test_client, db_engine, console_type="novnc")
    upstream = _FakeUpstream()
    mediated = AsyncMock()
    monkeypatch.setattr(console_relay, "open_upstream", AsyncMock(return_value=upstream))
    monkeypatch.setattr(console_relay, "mediate_rfb_auth", mediated)
    monkeypatch.setattr(console_relay, "relay_frames", AsyncMock())

    with auth_test_client.websocket_connect(
        created["websocket_path"],
        headers={"origin": ORIGIN},
        subprotocols=_browser_protocols(created),
    ):
        pass

    mediated.assert_awaited_once()
    assert mediated.await_args.args[2] == TICKET_CANARY
    assert upstream.closed is True


def test_policy_seam_can_deny_create_and_consume(
    monkeypatch,
    auth_test_client,
    db_engine,
) -> None:
    endpoint_id = _make_endpoint(db_engine)
    monkeypatch.setattr(
        console, "_create_console_session_for_endpoint", AsyncMock(return_value=_private_response())
    )

    def deny_create(_endpoint, *, stage):
        assert stage == "create"
        raise console_relay_policy.ConsoleRelayPolicyDenied

    monkeypatch.setattr(console_relay_policy, "require_console_relay_policy", deny_create)
    denied = auth_test_client.post("/proxmox/console/browser-sessions", json=_request(endpoint_id))
    assert denied.status_code == 403

    monkeypatch.setattr(
        console_relay_policy, "require_console_relay_policy", lambda *_args, **_kwargs: None
    )
    created = auth_test_client.post(
        "/proxmox/console/browser-sessions", json=_request(endpoint_id)
    ).json()

    def deny_consume(_endpoint, *, stage):
        assert stage == "consume"
        raise console_relay_policy.ConsoleRelayPolicyDenied

    monkeypatch.setattr(console_relay_policy, "require_console_relay_policy", deny_consume)
    with pytest.raises(WebSocketDisconnect) as rejected:
        with auth_test_client.websocket_connect(
            created["websocket_path"],
            headers={"origin": ORIGIN},
            subprotocols=_browser_protocols(created),
        ):
            pass
    assert rejected.value.code == 1008


def test_mounted_consume_rechecks_disabled_endpoint(
    monkeypatch,
    auth_test_client,
    db_engine,
) -> None:
    created = _create_browser_ticket(monkeypatch, auth_test_client, db_engine)
    with Session(db_engine) as session:
        endpoint = session.exec(select(ProxmoxEndpoint)).one()
        endpoint.enabled = False
        session.add(endpoint)
        session.commit()
    opener = AsyncMock()
    monkeypatch.setattr(console_relay, "open_upstream", opener)

    with pytest.raises(WebSocketDisconnect) as rejected:
        with auth_test_client.websocket_connect(
            created["websocket_path"],
            headers={"origin": ORIGIN},
            subprotocols=_browser_protocols(created),
        ):
            pass

    assert rejected.value.code == 1008
    opener.assert_not_awaited()
