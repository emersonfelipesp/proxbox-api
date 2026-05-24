"""SSH terminal ticket and WebSocket routes."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, WebSocket
from pydantic import BaseModel, ConfigDict, Field, model_validator

from proxbox_api.dependencies import NetBoxSessionDep
from proxbox_api.logger import logger
from proxbox_api.services.ssh_terminal import (
    TerminalCredentialError,
    TerminalSessionError,
    connect_and_relay,
    fetch_terminal_credential,
    terminal_session_manager,
)

router = APIRouter()


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

    @model_validator(mode="after")
    def validate_target(self) -> "TerminalSessionCreate":
        if self.target_type == "node":
            if self.node_id is None:
                raise ValueError("node_id is required for node SSH terminal sessions")
            if not (self.host or "").strip():
                raise ValueError("host is required for node SSH terminal sessions")
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
    actor = payload.actor or request.headers.get("X-Proxbox-Actor")
    try:
        session, ticket = await terminal_session_manager.create_session(
            target_type=payload.target_type,
            endpoint_id=payload.endpoint_id,
            node_id=payload.node_id,
            host=payload.host,
            actor=actor,
            cols=payload.cols,
            rows=payload.rows,
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
