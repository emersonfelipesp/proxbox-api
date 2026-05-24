"""Browser SSH terminal session manager and AsyncSSH relay."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import os
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from fastapi import WebSocket
from netbox_sdk.config import authorization_header_value
from starlette.websockets import WebSocketDisconnect

from proxbox_api.services.hardware_discovery import (
    HardwareDiscoveryError,
    fetch_credential,
)

if TYPE_CHECKING:
    from netbox_sdk.facade import Api


TerminalTargetType = Literal["node", "endpoint"]


class TerminalSessionError(Exception):
    """Base terminal-session failure."""


class TerminalCredentialError(TerminalSessionError):
    """Raised when SSH credential material cannot be resolved."""


@dataclass
class TerminalSession:
    """Short-lived ticket state for a browser terminal session."""

    session_id: str
    ticket_hash: str
    target_type: TerminalTargetType
    endpoint_id: int
    node_id: int | None
    host: str | None
    actor: str | None
    cols: int
    rows: int
    created_at: datetime
    expires_at: datetime
    last_activity_at: datetime
    consumed: bool = False


@dataclass(frozen=True)
class TerminalCredential:
    """Decrypted SSH credential for the relay boundary."""

    target_type: TerminalTargetType
    target_id: int
    host: str
    port: int
    username: str
    known_host_fingerprint: str
    password: str | None = None
    private_key: str | None = None
    display: str = ""


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


class TerminalSessionManager:
    """In-memory one-time ticket store for SSH terminal handoff."""

    def __init__(self) -> None:
        self.ticket_ttl_seconds = _env_int("PROXBOX_SSH_TERMINAL_TICKET_TTL_SECONDS", 60)
        self.idle_timeout_seconds = _env_int("PROXBOX_SSH_TERMINAL_IDLE_TIMEOUT_SECONDS", 900)
        self.max_lifetime_seconds = _env_int("PROXBOX_SSH_TERMINAL_MAX_LIFETIME_SECONDS", 7200)
        self.max_sessions = _env_int("PROXBOX_SSH_TERMINAL_MAX_SESSIONS", 10)
        self._sessions: dict[str, TerminalSession] = {}
        self._lock = asyncio.Lock()

    async def create_session(
        self,
        *,
        target_type: TerminalTargetType,
        endpoint_id: int,
        node_id: int | None,
        host: str | None,
        actor: str | None,
        cols: int,
        rows: int,
    ) -> tuple[TerminalSession, str]:
        """Create a short-lived ticket and return ``(session, plaintext_ticket)``."""
        async with self._lock:
            now = datetime.now(UTC)
            self._cleanup_locked(now)
            if len(self._sessions) >= self.max_sessions:
                raise TerminalSessionError("Maximum SSH terminal session count reached")

            ticket = secrets.token_urlsafe(32)
            session_id = secrets.token_urlsafe(24)
            session = TerminalSession(
                session_id=session_id,
                ticket_hash=self._hash_ticket(ticket),
                target_type=target_type,
                endpoint_id=endpoint_id,
                node_id=node_id,
                host=host.strip() if host else None,
                actor=actor.strip() if actor else None,
                cols=cols,
                rows=rows,
                created_at=now,
                expires_at=now + timedelta(seconds=self.ticket_ttl_seconds),
                last_activity_at=now,
            )
            self._sessions[session.session_id] = session
            return session, ticket

    async def consume_ticket(self, session_id: str, ticket: str) -> TerminalSession:
        """Validate and consume a one-time ticket for WebSocket upgrade."""
        async with self._lock:
            now = datetime.now(UTC)
            self._cleanup_locked(now)
            session = self._sessions.get(session_id)
            if session is None:
                raise TerminalSessionError("SSH terminal session not found")
            if session.consumed:
                raise TerminalSessionError("SSH terminal ticket has already been used")
            if now > session.expires_at:
                self._sessions.pop(session_id, None)
                raise TerminalSessionError("SSH terminal ticket has expired")
            if not secrets.compare_digest(session.ticket_hash, self._hash_ticket(ticket)):
                raise TerminalSessionError("Invalid SSH terminal ticket")
            session.consumed = True
            session.last_activity_at = now
            return session

    async def mark_activity(self, session_id: str) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.last_activity_at = datetime.now(UTC)

    async def release(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)

    def idle_remaining_seconds(self, session: TerminalSession) -> float:
        deadline = session.last_activity_at + timedelta(seconds=self.idle_timeout_seconds)
        return max(0.0, (deadline - datetime.now(UTC)).total_seconds())

    def lifetime_remaining_seconds(self, session: TerminalSession) -> float:
        deadline = session.created_at + timedelta(seconds=self.max_lifetime_seconds)
        return max(0.0, (deadline - datetime.now(UTC)).total_seconds())

    @staticmethod
    def _hash_ticket(ticket: str) -> str:
        return hashlib.sha256(ticket.encode()).hexdigest()

    def _cleanup_locked(self, now: datetime) -> None:
        expired: list[str] = []
        for session_id, session in self._sessions.items():
            lifetime_deadline = session.created_at + timedelta(seconds=self.max_lifetime_seconds)
            if now > session.expires_at and not session.consumed:
                expired.append(session_id)
            elif now > lifetime_deadline:
                expired.append(session_id)
        for session_id in expired:
            self._sessions.pop(session_id, None)


terminal_session_manager = TerminalSessionManager()


def _endpoint_credential_url(base_url: str, endpoint_id: int) -> str:
    return (
        f"{base_url.rstrip('/')}"
        f"/api/plugins/proxbox/ssh-credentials/by-endpoint/{int(endpoint_id)}/credentials/"
    )


def _urlopen_kwargs(config: object, url: str, timeout: float) -> dict[str, object]:
    parsed = urllib.parse.urlsplit(url)
    kwargs: dict[str, object] = {"timeout": timeout}
    if parsed.scheme.lower() == "https" and getattr(config, "ssl_verify", True) is False:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["context"] = ctx
    return kwargs


def _coerce_endpoint_credential(
    endpoint_id: int,
    requested_host: str | None,
    payload: dict[str, Any],
) -> TerminalCredential:
    host = str(payload.get("host") or requested_host or "").strip()
    username = str(payload.get("username") or payload.get("ssh_username") or "").strip()
    fingerprint = str(
        payload.get("known_host_fingerprint") or payload.get("ssh_known_host_fingerprint") or ""
    ).strip()
    if not host:
        raise TerminalCredentialError(f"SSH credential for endpoint {endpoint_id} is missing host")
    if not username:
        raise TerminalCredentialError(
            f"SSH credential for endpoint {endpoint_id} is missing username"
        )
    if not fingerprint:
        raise TerminalCredentialError(
            f"SSH credential for endpoint {endpoint_id} is missing known_host_fingerprint"
        )
    try:
        port = int(payload.get("port") or payload.get("ssh_port") or 22)
    except (TypeError, ValueError) as exc:
        raise TerminalCredentialError(
            f"SSH credential for endpoint {endpoint_id} has invalid port"
        ) from exc
    password = payload.get("password") or payload.get("ssh_password") or None
    private_key = payload.get("private_key") or payload.get("ssh_private_key") or None
    return TerminalCredential(
        target_type="endpoint",
        target_id=int(payload.get("endpoint_id") or endpoint_id),
        host=host,
        port=port,
        username=username,
        known_host_fingerprint=fingerprint,
        password=password,
        private_key=private_key,
        display=f"{username}@{host}:{port}",
    )


def _fetch_endpoint_credential(
    netbox_session: "Api",
    endpoint_id: int,
    host: str | None,
    *,
    timeout: float = 10.0,
) -> TerminalCredential:
    config = netbox_session.client.config
    base_url = (config.base_url or "").rstrip("/")
    if not base_url:
        raise TerminalCredentialError("NetBox base_url is not configured")

    auth = authorization_header_value(config)
    if not auth:
        raise TerminalCredentialError("NetBox auth header could not be built")

    url = _endpoint_credential_url(base_url, endpoint_id)
    req = urllib.request.Request(
        url,
        headers={"Authorization": auth, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, **_urlopen_kwargs(config, url, timeout)) as resp:
            if resp.status != 200:
                raise TerminalCredentialError(
                    f"Endpoint SSH credential fetch returned HTTP {resp.status}"
                )
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise TerminalCredentialError(
                f"No SSH credential registered for endpoint {endpoint_id}"
            ) from exc
        raise TerminalCredentialError(
            f"Endpoint SSH credential fetch failed: HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise TerminalCredentialError(
            f"Endpoint SSH credential fetch failed: {exc.reason!s}"
        ) from exc

    try:
        payload = json.loads(body.decode())
    except json.JSONDecodeError as exc:
        raise TerminalCredentialError(
            f"Endpoint SSH credential fetch for {endpoint_id} returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise TerminalCredentialError(
            f"Endpoint SSH credential fetch for {endpoint_id} returned non-object payload"
        )
    return _coerce_endpoint_credential(endpoint_id, host, payload)


async def fetch_terminal_credential(
    netbox_session: "Api",
    session: TerminalSession,
) -> TerminalCredential:
    """Resolve node or endpoint SSH credential material from netbox-proxbox."""
    if session.target_type == "node":
        if session.node_id is None or not session.host:
            raise TerminalCredentialError("Node terminal target requires node_id and host")
        try:
            cred = await asyncio.to_thread(
                fetch_credential,
                netbox_session,
                session.node_id,
                session.host,
            )
        except HardwareDiscoveryError as exc:
            raise TerminalCredentialError(str(exc)) from exc
        return TerminalCredential(
            target_type="node",
            target_id=cred.node_id,
            host=cred.host,
            port=cred.port,
            username=cred.username,
            known_host_fingerprint=cred.known_host_fingerprint,
            password=cred.password,
            private_key=cred.private_key,
            display=f"{cred.username}@{cred.host}:{cred.port}",
        )

    return await asyncio.to_thread(
        _fetch_endpoint_credential,
        netbox_session,
        session.endpoint_id,
        session.host,
    )


def _canonical_fingerprint(value: str) -> str:
    text = value.strip()
    if text.lower().startswith("sha256:"):
        text = text.split(":", 1)[1]
    return f"SHA256:{text.rstrip('=')}"


def _fingerprint_from_key(key: object) -> str:
    getter = getattr(key, "get_fingerprint", None)
    if not callable(getter):
        raise TerminalCredentialError("SSH server key does not expose a fingerprint")
    for algorithm in ("sha256", "SHA256"):
        try:
            value = getter(algorithm)
        except TypeError:
            continue
        if value:
            return _canonical_fingerprint(str(value))
    value = getter()
    if not value:
        raise TerminalCredentialError("SSH server key fingerprint is empty")
    return _canonical_fingerprint(str(value))


def _load_asyncssh() -> Any:
    return importlib.import_module("asyncssh")


def _pinned_client_factory(asyncssh_module: Any, expected_fingerprint: str) -> object:
    expected = _canonical_fingerprint(expected_fingerprint)

    class PinnedHostKeyClient(asyncssh_module.SSHClient):  # type: ignore[misc]
        def validate_host_public_key(self, host, addr, port, key):  # noqa: ANN001
            actual = _fingerprint_from_key(key)
            return secrets.compare_digest(expected, actual)

    return PinnedHostKeyClient


async def _maybe_drain(writer: object) -> None:
    drain = getattr(writer, "drain", None)
    if not callable(drain):
        return
    result = drain()
    if inspect.isawaitable(result):
        await result


async def _send_json_safe(websocket: WebSocket, payload: dict[str, object]) -> None:
    try:
        await websocket.send_json(payload)
    except Exception:  # noqa: BLE001
        return


async def _pump_process_output(
    websocket: WebSocket,
    process: object,
    session: TerminalSession,
    manager: TerminalSessionManager,
) -> None:
    stdout = getattr(process, "stdout", None)
    if stdout is None:
        return
    while True:
        chunk = await stdout.read(4096)
        if not chunk:
            return
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8", errors="replace")
        await manager.mark_activity(session.session_id)
        await _send_json_safe(websocket, {"type": "output", "data": str(chunk)})


async def _resize_process(process: object, cols: int, rows: int) -> None:
    resize = getattr(process, "change_terminal_size", None)
    if not callable(resize):
        return
    result = resize(cols, rows, 0, 0)
    if inspect.isawaitable(result):
        await result


async def _read_terminal_message(
    websocket: WebSocket,
    session: TerminalSession,
    manager: TerminalSessionManager,
) -> dict | None:
    timeout = min(
        manager.idle_remaining_seconds(session),
        manager.lifetime_remaining_seconds(session),
    )
    if timeout <= 0:
        await _send_json_safe(
            websocket,
            {"type": "error", "message": "SSH terminal session timed out"},
        )
        return None
    try:
        message = await asyncio.wait_for(websocket.receive_json(), timeout=timeout)
    except TimeoutError:
        await _send_json_safe(
            websocket,
            {"type": "error", "message": "SSH terminal session timed out"},
        )
        return None
    except WebSocketDisconnect:
        return None
    if not isinstance(message, dict):
        await _send_json_safe(
            websocket,
            {"type": "error", "message": "Invalid SSH terminal message"},
        )
        return {}
    return message


async def _handle_resize_message(
    websocket: WebSocket,
    process: object,
    session: TerminalSession,
    message: dict,
) -> None:
    try:
        cols = max(20, min(400, int(message.get("cols") or session.cols)))
        rows = max(5, min(200, int(message.get("rows") or session.rows)))
    except (TypeError, ValueError):
        await _send_json_safe(
            websocket,
            {"type": "error", "message": "Invalid terminal size"},
        )
        return
    session.cols = cols
    session.rows = rows
    await _resize_process(process, cols, rows)


async def _handle_terminal_message(
    websocket: WebSocket,
    process: object,
    session: TerminalSession,
    message: dict,
) -> bool:
    message_type = message.get("type")
    if message_type == "input":
        stdin = getattr(process, "stdin", None)
        if stdin is not None:
            stdin.write(str(message.get("data", "")))
            await _maybe_drain(stdin)
        return True
    if message_type == "resize":
        await _handle_resize_message(websocket, process, session, message)
        return True
    if message_type == "close":
        return False
    await _send_json_safe(
        websocket,
        {"type": "error", "message": f"Unsupported message type: {message_type}"},
    )
    return True


async def _pump_websocket_input(
    websocket: WebSocket,
    process: object,
    session: TerminalSession,
    manager: TerminalSessionManager,
) -> None:
    while True:
        message = await _read_terminal_message(websocket, session, manager)
        if message is None:
            return
        if not message:
            continue
        await manager.mark_activity(session.session_id)
        keep_running = await _handle_terminal_message(
            websocket,
            process,
            session,
            message,
        )
        if not keep_running:
            return


async def _close_process(process: object) -> int | None:
    terminate = getattr(process, "terminate", None)
    if callable(terminate):
        terminate()
    wait = getattr(process, "wait", None)
    if not callable(wait):
        return None
    try:
        status = await asyncio.wait_for(wait(), timeout=5)
    except TimeoutError:
        kill = getattr(process, "kill", None)
        if callable(kill):
            kill()
        return None
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


async def connect_and_relay(
    websocket: WebSocket,
    session: TerminalSession,
    credential: TerminalCredential,
    *,
    manager: TerminalSessionManager = terminal_session_manager,
) -> None:
    """Open an AsyncSSH PTY and relay terminal frames until either side closes."""
    if not (credential.password or credential.private_key):
        raise TerminalCredentialError("SSH credential has no password or private key")

    asyncssh = _load_asyncssh()
    client_keys = None
    if credential.private_key:
        try:
            client_keys = [asyncssh.import_private_key(credential.private_key)]
        except Exception as exc:  # noqa: BLE001
            raise TerminalCredentialError("SSH private key could not be loaded") from exc

    conn = await asyncssh.connect(
        credential.host,
        port=credential.port,
        username=credential.username,
        password=credential.password or None,
        client_keys=client_keys,
        known_hosts=b"",
        server_host_key_algs="default",
        client_factory=_pinned_client_factory(
            asyncssh,
            credential.known_host_fingerprint,
        ),
    )

    async with conn:
        process = await conn.create_process(
            term_type="xterm-256color",
            term_size=(session.cols, session.rows, 0, 0),
            encoding="utf-8",
            errors="replace",
        )
        await _send_json_safe(
            websocket,
            {
                "type": "ready",
                "session_id": session.session_id,
                "target": credential.display,
            },
        )
        output_task = asyncio.create_task(
            _pump_process_output(websocket, process, session, manager)
        )
        input_task = asyncio.create_task(
            _pump_websocket_input(websocket, process, session, manager)
        )
        done, pending = await asyncio.wait(
            {output_task, input_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            error = task.exception()
            if error is not None:
                raise error
        status = await _close_process(process)
        await _send_json_safe(websocket, {"type": "exit", "status": status})
