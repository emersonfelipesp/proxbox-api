"""SSH terminal ticket and WebSocket routes."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket
from pydantic import BaseModel, ConfigDict, Field, model_validator

from proxbox_api.dependencies import NetBoxSessionDep
from proxbox_api.logger import logger
from proxbox_api.services.ssh_terminal import (
    HostKeyScanError,
    OneShotTerminalCredential,
    TerminalCredentialError,
    TerminalSessionError,
    connect_and_relay,
    fetch_terminal_credential,
    scan_host_key_fingerprint,
    terminal_session_manager,
)

router = APIRouter()


class HostKeyFingerprintPublic(BaseModel):
    """Scanned SSH host-key fingerprint for pinned-fingerprint auto-fill."""

    host: str
    port: int
    fingerprint: str
    key_type: str


@router.get("/host-key-fingerprint", response_model=HostKeyFingerprintPublic)
async def get_host_key_fingerprint(
    host: str = Query(
        ...,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9._:\-\[\]]+$",
        description="Hostname or IP of the SSH host to fingerprint.",
    ),
    port: int = Query(22, ge=1, le=65535),
) -> HostKeyFingerprintPublic:
    """Return the host's canonical ``SHA256:<base64>`` SSH host-key fingerprint.

    The handshake mirrors the browser-terminal connection exactly, so the value
    returned here is what the pinned-fingerprint check verifies on a real
    session. It authenticates nothing; only the public host key is read.
    """
    try:
        fingerprint, key_type = await scan_host_key_fingerprint(host, port)
    except HostKeyScanError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return HostKeyFingerprintPublic(
        host=host, port=port, fingerprint=fingerprint, key_type=key_type
    )


class OneShotCredentialInput(BaseModel):
    """Inline SSH credential for a single, unstored terminal session.

    Supplied by the NetBox plugin when an operator opts to connect "just once"
    without persisting a ``NodeSSHCredential``. The material lives only in the
    in-memory ``TerminalSession`` for the ticket lifetime and is never written to
    disk or logged. A pinned host-key fingerprint is mandatory because
    ``connect_and_relay`` refuses to connect without one.
    """

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    port: int = Field(default=22, ge=1, le=65535)
    known_host_fingerprint: str = Field(min_length=1, max_length=128)
    password: str | None = None
    private_key: str | None = None

    @model_validator(mode="after")
    def validate_secret(self) -> "OneShotCredentialInput":
        if not ((self.password or "").strip() or (self.private_key or "").strip()):
            raise ValueError("one_shot_credential requires a password or private_key")
        if not self.known_host_fingerprint.strip():
            raise ValueError("one_shot_credential requires known_host_fingerprint")
        return self


class TerminalSessionCreate(BaseModel):
    """HTTP request for creating a one-time browser SSH session ticket."""

    model_config = ConfigDict(extra="forbid")

    target_type: Literal["node", "endpoint"]
    endpoint_id: int = Field(ge=1)
    node_id: int | None = Field(default=None, ge=1)
    host: str | None = None
    actor: str | None = None
    cols: int = Field(default=120, ge=20, le=400)
    rows: int = Field(default=32, ge=5, le=200)
    one_shot_credential: OneShotCredentialInput | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "TerminalSessionCreate":
        if self.target_type == "node":
            if self.node_id is None:
                raise ValueError("node_id is required for node SSH terminal sessions")
            if not (self.host or "").strip():
                raise ValueError("host is required for node SSH terminal sessions")
        # A one-shot session bypasses the stored-credential fetch, so the host
        # must be supplied inline for every target type (node already requires
        # it above; enforce it for endpoint too).
        if self.one_shot_credential is not None and not (self.host or "").strip():
            raise ValueError("host is required for one-shot SSH terminal sessions")
        return self


class TerminalSessionPublic(BaseModel):
    """Created terminal session details returned to NetBox."""

    session_id: str
    ticket: str
    websocket_path: str
    expires_at: str
    target_type: Literal["node", "endpoint"]


@router.post("/sessions", response_model=TerminalSessionPublic, status_code=201)
async def create_terminal_session(
    payload: TerminalSessionCreate,
    request: Request,
) -> TerminalSessionPublic:
    # NOTE: the per-endpoint SSH access-method gate (access_methods="api_ssh")
    # for the browser terminal is enforced on the netbox-proxbox side, where the
    # endpoint_id is native and the SSH credentials live. proxbox-api's SQLite
    # ProxmoxEndpoint table uses an independent id space, so it is NOT the
    # authority for terminal SSH access. proxbox-api enforces access_methods only
    # for its own SQLite-id SSH paths (Cloud Image Build Pipeline / Azure VHD
    # import). See routes/proxmox/access_gate.py and the netbox-proxbox plugin.
    actor = payload.actor or request.headers.get("X-Proxbox-Actor")
    one_shot = None
    if payload.one_shot_credential is not None:
        osc = payload.one_shot_credential
        one_shot = OneShotTerminalCredential(
            username=osc.username.strip(),
            port=osc.port,
            known_host_fingerprint=osc.known_host_fingerprint.strip(),
            password=(osc.password or None),
            private_key=(osc.private_key or None),
        )
    try:
        session, ticket = await terminal_session_manager.create_session(
            target_type=payload.target_type,
            endpoint_id=payload.endpoint_id,
            node_id=payload.node_id,
            host=payload.host,
            actor=actor,
            cols=payload.cols,
            rows=payload.rows,
            one_shot_credential=one_shot,
        )
    except TerminalSessionError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    return TerminalSessionPublic(
        session_id=session.session_id,
        ticket=ticket,
        websocket_path=f"/ssh/sessions/{session.session_id}/ws",
        expires_at=session.expires_at.isoformat(),
        target_type=session.target_type,
    )


async def _receive_auth_message(websocket: WebSocket) -> dict:
    try:
        message = await asyncio.wait_for(websocket.receive_json(), timeout=10)
    except TimeoutError as exc:
        raise TerminalSessionError("SSH terminal authentication timed out") from exc
    if not isinstance(message, dict):
        raise TerminalSessionError("Invalid SSH terminal authentication frame")
    if message.get("type") != "auth":
        raise TerminalSessionError("First SSH terminal frame must be type=auth")
    ticket = str(message.get("ticket") or "").strip()
    if not ticket:
        raise TerminalSessionError("SSH terminal ticket is required")
    return {"ticket": ticket}


@router.websocket("/sessions/{session_id}/ws")
async def ssh_terminal_websocket(
    websocket: WebSocket,
    session_id: str,
    netbox_session: NetBoxSessionDep,
) -> None:
    """Authenticate a ticket, resolve SSH credentials, and bridge the PTY."""
    await websocket.accept()
    session = None
    try:
        auth = await _receive_auth_message(websocket)
        session = await terminal_session_manager.consume_ticket(
            session_id,
            str(auth["ticket"]),
        )
    except TerminalSessionError as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close(code=4001)
        return

    try:
        credential = await fetch_terminal_credential(netbox_session, session)
        await connect_and_relay(websocket, session, credential)
    except TerminalCredentialError as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
    except Exception:  # noqa: BLE001
        logger.exception(
            "SSH terminal session failed",
            extra={
                "session_id": session.session_id,
                "target_type": session.target_type,
                "endpoint_id": session.endpoint_id,
                "node_id": session.node_id,
            },
        )
        await websocket.send_json({"type": "error", "message": "SSH terminal failed"})
    finally:
        if session is not None:
            await terminal_session_manager.release(session.session_id)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass
