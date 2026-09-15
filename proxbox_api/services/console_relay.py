"""Durable one-use state and browser-safe Proxmox console transport relay."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import secrets
import ssl
import time
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol, cast

from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from fastapi import WebSocket
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedOK, InvalidStatus, SecurityError
from websockets.typing import Subprotocol

from proxbox_api.credentials import decrypt_value, encrypt_value, is_encryption_enabled
from proxbox_api.database import BrowserConsoleRelaySession

TOKEN_BYTES = 32
TOKEN_LENGTH = 43
TOKEN_SUBPROTOCOL_PREFIX = "proxbox-token."
TOKEN_TTL_SECONDS = 30
MAX_ACTIVE_TOKENS = 1024
MAX_ORIGIN_LENGTH = 2048
MAX_TICKET_LENGTH = 2048
MAX_AUTH_HEADER_LENGTH = 4096
MAX_UPSTREAM_URL_LENGTH = 4096
MAX_ENCRYPTED_PAYLOAD_LENGTH = 16384
MAX_HANDSHAKE_FRAME_SIZE = 4096
MAX_RELAY_FRAME_SIZE = 1024 * 1024
UPSTREAM_OPEN_TIMEOUT_SECONDS = 10
HANDSHAKE_TIMEOUT_SECONDS = 10
RELAY_IDLE_TIMEOUT_SECONDS = 300
CLOSE_TIMEOUT_SECONDS = 5
_REDIRECT_STATUS_CODES = frozenset({300, 301, 302, 303, 307, 308})
_ENCRYPTION_PREFLIGHT_VALUE = "proxbox-console-relay-preflight"


class ConsoleRelayError(RuntimeError):
    """A browser-safe console relay failure."""


class ConsoleRelayUnavailable(ConsoleRelayError):
    """The relay cannot safely create durable session state."""


class ConsoleRelayRejected(ConsoleRelayError):
    """The supplied token or browser binding cannot be accepted."""


class ConsoleRelayProtocolError(ConsoleRelayError):
    """A bounded upstream or downstream protocol check failed."""


class ConsoleRelayPayload(BaseModel):
    """Private session material stored only as one Fernet ciphertext."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: int = Field(ge=1)
    vmid: int = Field(ge=1)
    node: str = Field(min_length=1, max_length=255)
    vm_type: Literal["qemu", "lxc"]
    console_type: Literal["novnc", "term"]
    origin: str = Field(min_length=1, max_length=MAX_ORIGIN_LENGTH)
    ws_url: str = Field(min_length=1, max_length=MAX_UPSTREAM_URL_LENGTH)
    ticket: str = Field(min_length=1, max_length=MAX_TICKET_LENGTH, repr=False)
    verify_ssl: bool
    auth_kind: Literal["authorization", "cookie"]
    auth_value: str = Field(min_length=1, max_length=MAX_AUTH_HEADER_LENGTH, repr=False)

    @field_validator("auth_value")
    @classmethod
    def validate_auth_value(cls, value: str) -> str:
        if not value.isascii() or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in value
        ):
            raise ValueError("WebSocket authentication contains control characters")
        return value

    @field_validator("ws_url")
    @classmethod
    def validate_upstream_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Invalid upstream WebSocket URL") from exc
        if (
            parsed.scheme != "wss"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError("Invalid upstream WebSocket URL")
        return value


class RelayUpstream(Protocol):
    """Small WebSocket client surface used by the relay and its tests."""

    subprotocol: str | None

    async def recv(self) -> str | bytes: ...

    async def send(self, message: str | bytes) -> None: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _new_stream_token() -> str:
    token = secrets.token_urlsafe(TOKEN_BYTES)
    if len(token) != TOKEN_LENGTH:  # pragma: no cover - stdlib contract guard
        raise ConsoleRelayUnavailable("Browser console relay is unavailable.")
    return token


def require_relay_encryption() -> None:
    """Prove Fernet encryption and decryption work before acquiring a ticket."""

    if not is_encryption_enabled():
        raise ConsoleRelayUnavailable("Browser console relay encryption is not configured.")
    try:
        encrypted = encrypt_value(_ENCRYPTION_PREFLIGHT_VALUE)
        decrypted = decrypt_value(encrypted)
    except Exception as exc:
        raise ConsoleRelayUnavailable("Browser console relay encryption failed.") from exc
    if (
        not encrypted
        or not encrypted.startswith("enc:")
        or decrypted != _ENCRYPTION_PREFLIGHT_VALUE
    ):
        raise ConsoleRelayUnavailable("Browser console relay encryption failed.")


async def create_relay_session(
    session: AsyncSession,
    payload: ConsoleRelayPayload,
    *,
    now: float | None = None,
) -> tuple[str, float]:
    """Encrypt and persist a bounded one-use token under an atomic count cap."""

    encrypted = _encrypt_relay_payload(payload)
    timestamp = time.time() if now is None else now
    return await _store_relay_session(session, encrypted, timestamp)


def _encrypt_relay_payload(payload: ConsoleRelayPayload) -> str:
    require_relay_encryption()
    try:
        encrypted = encrypt_value(payload.model_dump_json())
    except Exception as exc:
        raise ConsoleRelayUnavailable("Browser console relay encryption failed.") from exc
    if not encrypted or not encrypted.startswith("enc:"):
        raise ConsoleRelayUnavailable("Browser console relay encryption is not configured.")
    if len(encrypted) > MAX_ENCRYPTED_PAYLOAD_LENGTH:
        raise ConsoleRelayUnavailable("Browser console relay session is too large.")
    return encrypted


async def _store_relay_session(
    session: AsyncSession,
    encrypted: str,
    timestamp: float,
) -> tuple[str, float]:
    expires_at = timestamp + TOKEN_TTL_SECONDS

    await session.rollback()
    connection = await session.connection()
    await connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        expired = await session.exec(
            select(BrowserConsoleRelaySession).where(
                BrowserConsoleRelaySession.expires_at <= timestamp
            )
        )
        for row in expired.all():
            await session.delete(row)
        count_result = await session.exec(
            select(func.count(BrowserConsoleRelaySession.token_digest))
        )
        if int(count_result.one()) >= MAX_ACTIVE_TOKENS:
            raise ConsoleRelayUnavailable("Browser console relay capacity is exhausted.")

        for _ in range(3):
            token = _new_stream_token()
            if await session.get(BrowserConsoleRelaySession, _token_digest(token)) is None:
                break
        else:  # pragma: no cover - cryptographic collision guard
            raise ConsoleRelayUnavailable("Browser console relay is unavailable.")

        session.add(
            BrowserConsoleRelaySession(
                token_digest=_token_digest(token),
                encrypted_payload=encrypted,
                created_at=timestamp,
                expires_at=expires_at,
            )
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return token, expires_at


async def consume_relay_session(
    session: AsyncSession,
    token: str,
    *,
    now: float | None = None,
) -> ConsoleRelayPayload:
    """Atomically remove, decrypt, and validate one shared SQLite relay row."""

    if len(token) != TOKEN_LENGTH or not token.isascii():
        raise ConsoleRelayRejected("Browser console session is invalid.")
    timestamp = time.time() if now is None else now
    row = await _consume_relay_row(session, token)
    return _decrypt_relay_row(row, timestamp)


async def _consume_relay_row(
    session: AsyncSession,
    token: str,
) -> BrowserConsoleRelaySession:
    await session.rollback()
    token_digest = _token_digest(token)
    digest_result = await session.exec(
        select(BrowserConsoleRelaySession.token_digest).where(
            BrowserConsoleRelaySession.token_digest == token_digest
        )
    )
    if digest_result.first() is None:
        await session.rollback()
        raise ConsoleRelayRejected("Browser console session is invalid.")
    await session.rollback()
    connection = await session.connection()
    await connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        row = await session.get(BrowserConsoleRelaySession, token_digest)
        if row is None:
            raise ConsoleRelayRejected("Browser console session is invalid.")
        await session.delete(row)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return row


def _decrypt_relay_row(
    row: BrowserConsoleRelaySession,
    timestamp: float,
) -> ConsoleRelayPayload:
    if not math.isfinite(row.expires_at) or row.expires_at <= timestamp:
        raise ConsoleRelayRejected("Browser console session is invalid.")
    if (
        not is_encryption_enabled()
        or len(row.encrypted_payload) > MAX_ENCRYPTED_PAYLOAD_LENGTH
        or not row.encrypted_payload.startswith("enc:")
    ):
        raise ConsoleRelayRejected("Browser console session is invalid.")
    try:
        decrypted = decrypt_value(row.encrypted_payload)
        if not decrypted:
            raise ValueError("empty relay payload")
        raw = json.loads(decrypted)
        return ConsoleRelayPayload.model_validate(raw)
    except (ValueError, TypeError, ValidationError) as exc:
        raise ConsoleRelayRejected("Browser console session is invalid.") from exc
    except Exception as exc:
        raise ConsoleRelayRejected("Browser console session is invalid.") from exc


def validate_origin_binding(expected: str, observed: str | None) -> None:
    """Require the browser's serialized Origin to match the create request exactly."""

    if observed is None or len(observed) > MAX_ORIGIN_LENGTH:
        raise ConsoleRelayRejected("Browser console session is invalid.")
    if not hmac.compare_digest(expected.encode(), observed.encode()):
        raise ConsoleRelayRejected("Browser console session is invalid.")


def parse_browser_subprotocols(header: str | None) -> str:
    """Return one bearer token from the exact two-protocol browser offer."""

    if header is None or len(header) > 256:
        raise ConsoleRelayRejected("Browser console session is invalid.")
    offered = [part.strip() for part in header.split(",")]
    token_protocols = [
        protocol for protocol in offered if protocol.startswith(TOKEN_SUBPROTOCOL_PREFIX)
    ]
    _validate_browser_protocol_offer(offered, token_protocols)
    token = token_protocols[0].removeprefix(TOKEN_SUBPROTOCOL_PREFIX)
    if len(token) != TOKEN_LENGTH or not _is_base64url_token(token):
        raise ConsoleRelayRejected("Browser console session is invalid.")
    return token


def _validate_browser_protocol_offer(offered: list[str], token_protocols: list[str]) -> None:
    if len(offered) != 2 or offered.count("binary") != 1:
        raise ConsoleRelayRejected("Browser console session is invalid.")
    if len(token_protocols) != 1:
        raise ConsoleRelayRejected("Browser console session is invalid.")


def _is_base64url_token(token: str) -> bool:
    return token.isascii() and all(character.isalnum() or character in "-_" for character in token)


def _upstream_headers(payload: ConsoleRelayPayload) -> dict[str, str]:
    header = "Authorization" if payload.auth_kind == "authorization" else "Cookie"
    return {header: payload.auth_value}


def _ssl_context(verify_ssl: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify_ssl:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


async def open_upstream(payload: ConsoleRelayPayload) -> RelayUpstream:
    """Open a bounded, proxy-free Proxmox WebSocket using private server data."""

    connection = await _NoRedirectConnect(
        payload.ws_url,
        additional_headers=_upstream_headers(payload),
        subprotocols=[Subprotocol("binary")],
        ssl=_ssl_context(payload.verify_ssl),
        proxy=None,
        compression=None,
        open_timeout=UPSTREAM_OPEN_TIMEOUT_SECONDS,
        close_timeout=CLOSE_TIMEOUT_SECONDS,
        max_size=MAX_RELAY_FRAME_SIZE,
        max_queue=16,
    )
    return cast(RelayUpstream, connection)


class _NoRedirectConnect(connect):
    """websockets 16 connector that never performs a redirected second dial."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        if isinstance(exc, InvalidStatus) and exc.response.status_code in _REDIRECT_STATUS_CODES:
            return SecurityError("Upstream WebSocket redirects are disabled.")
        return exc


class _BinaryFrameReader:
    """Read exact RFB fields across bounded binary WebSocket messages."""

    def __init__(self, receive: Callable[[], Awaitable[str | bytes]]) -> None:
        self._receive = receive
        self._buffer = bytearray()

    async def read_exactly(self, size: int) -> bytes:
        while len(self._buffer) < size:
            message = await self._receive()
            if not isinstance(message, bytes) or len(message) > MAX_HANDSHAKE_FRAME_SIZE:
                raise ConsoleRelayProtocolError("Console handshake failed.")
            self._buffer.extend(message)
            if len(self._buffer) > MAX_HANDSHAKE_FRAME_SIZE:
                raise ConsoleRelayProtocolError("Console handshake failed.")
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result

    def take_buffered(self) -> bytes:
        result = bytes(self._buffer)
        self._buffer.clear()
        return result


def _reverse_byte_bits(value: int) -> int:
    return int(f"{value:08b}"[::-1], 2)


def _vnc_challenge_response(ticket: str, challenge: bytes) -> bytes:
    """Return the RFB VNC-auth DES response without exposing the ticket."""

    if len(challenge) != 16:
        raise ConsoleRelayProtocolError("Console handshake failed.")
    password = ticket.encode("utf-8")[:8].ljust(8, b"\0")
    key = bytes(_reverse_byte_bits(value) for value in password)
    # Three identical DES keys preserve the RFB single-DES operation while
    # avoiding cryptography's deprecated 8-byte TripleDES key form.
    encryptor = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
    return encryptor.update(challenge) + encryptor.finalize()


async def _browser_handshake_receive(websocket: WebSocket) -> str | bytes:
    message = await websocket.receive()
    if message.get("type") != "websocket.receive":
        raise ConsoleRelayProtocolError("Console handshake failed.")
    if message.get("bytes") is not None:
        return cast(bytes, message["bytes"])
    if message.get("text") is not None:
        return cast(str, message["text"])
    raise ConsoleRelayProtocolError("Console handshake failed.")


async def mediate_rfb_auth(
    websocket: WebSocket,
    upstream: RelayUpstream,
    ticket: str,
) -> None:
    """Authenticate RFB 3.8 to Proxmox and advertise no-auth to the browser."""

    upstream_reader = _BinaryFrameReader(upstream.recv)
    browser_reader = _BinaryFrameReader(lambda: _browser_handshake_receive(websocket))
    async with asyncio.timeout(HANDSHAKE_TIMEOUT_SECONDS):
        version = await upstream_reader.read_exactly(12)
        if version != b"RFB 003.008\n":
            raise ConsoleRelayProtocolError("Console handshake failed.")
        await websocket.send_bytes(version)
        if await browser_reader.read_exactly(12) != version:
            raise ConsoleRelayProtocolError("Console handshake failed.")
        await upstream.send(version)

        security_count = (await upstream_reader.read_exactly(1))[0]
        if security_count == 0 or security_count > 32:
            raise ConsoleRelayProtocolError("Console handshake failed.")
        security_types = await upstream_reader.read_exactly(security_count)
        if 2 not in security_types:
            raise ConsoleRelayProtocolError("Console handshake failed.")
        await upstream.send(b"\x02")
        challenge = await upstream_reader.read_exactly(16)
        await upstream.send(_vnc_challenge_response(ticket, challenge))
        if await upstream_reader.read_exactly(4) != b"\x00\x00\x00\x00":
            raise ConsoleRelayProtocolError("Console handshake failed.")

        await websocket.send_bytes(b"\x01\x01")
        if await browser_reader.read_exactly(1) != b"\x01":
            raise ConsoleRelayProtocolError("Console handshake failed.")
        await websocket.send_bytes(b"\x00\x00\x00\x00")

        buffered_browser = browser_reader.take_buffered()
        if buffered_browser:
            await upstream.send(buffered_browser)
        buffered_upstream = upstream_reader.take_buffered()
        if buffered_upstream:
            await websocket.send_bytes(buffered_upstream)


class _RelayActivity:
    """Track one idle deadline shared by both relay directions."""

    def __init__(
        self,
        timeout: float,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._timeout = timeout
        self._clock = clock or asyncio.get_running_loop().time
        self._deadline = self._clock() + timeout
        self._changed = asyncio.Event()

    def touch(self) -> None:
        self._deadline = self._clock() + self._timeout
        self._changed.set()

    async def _wait_for_change(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._changed.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def wait_until_idle(self) -> None:
        while True:
            deadline = self._deadline
            self._changed.clear()
            remaining = deadline - self._clock()
            if remaining <= 0:
                return
            changed = await self._wait_for_change(remaining)
            if not changed and deadline == self._deadline:
                return


async def _relay_browser_to_upstream(
    websocket: WebSocket,
    upstream: RelayUpstream,
    activity: _RelayActivity,
) -> None:
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return
        if message.get("type") != "websocket.receive":
            raise ConsoleRelayProtocolError("Console stream failed.")
        data = message.get("bytes") if message.get("bytes") is not None else message.get("text")
        if not isinstance(data, (bytes, str)) or _frame_size(data) > MAX_RELAY_FRAME_SIZE:
            raise ConsoleRelayProtocolError("Console stream failed.")
        activity.touch()
        await asyncio.wait_for(upstream.send(data), timeout=RELAY_IDLE_TIMEOUT_SECONDS)


async def _relay_upstream_to_browser(
    websocket: WebSocket,
    upstream: RelayUpstream,
    activity: _RelayActivity,
) -> None:
    while True:
        try:
            data = await upstream.recv()
        except ConnectionClosedOK:
            return
        if _frame_size(data) > MAX_RELAY_FRAME_SIZE:
            raise ConsoleRelayProtocolError("Console stream failed.")
        activity.touch()
        if isinstance(data, bytes):
            await asyncio.wait_for(websocket.send_bytes(data), timeout=RELAY_IDLE_TIMEOUT_SECONDS)
        else:
            await asyncio.wait_for(websocket.send_text(data), timeout=RELAY_IDLE_TIMEOUT_SECONDS)


def _frame_size(data: str | bytes) -> int:
    return len(data) if isinstance(data, bytes) else len(data.encode("utf-8"))


async def relay_frames(websocket: WebSocket, upstream: RelayUpstream) -> None:
    """Relay both frame kinds and deterministically settle both peer tasks."""

    activity = _RelayActivity(RELAY_IDLE_TIMEOUT_SECONDS)
    tasks = [
        asyncio.create_task(_relay_browser_to_upstream(websocket, upstream, activity)),
        asyncio.create_task(_relay_upstream_to_browser(websocket, upstream, activity)),
        asyncio.create_task(activity.wait_until_idle()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException) and not isinstance(
            result, (asyncio.CancelledError, StopAsyncIteration)
        ):
            raise result


async def close_upstream(upstream: RelayUpstream) -> None:
    """Close the upstream under a final timeout without leaking its reason."""

    try:
        await asyncio.wait_for(
            upstream.close(code=1000, reason="Console relay closed."),
            timeout=CLOSE_TIMEOUT_SECONDS,
        )
    except Exception:
        return
