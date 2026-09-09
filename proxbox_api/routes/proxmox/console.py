"""Proxmox VM/CT console ticket-relay routes.

Security model:
- endpoint_id must resolve to a live ProxmoxEndpoint row; unknown IDs return 404.
- node and vmid are validated by Pydantic (str min-length, int ge=1).
- vm_type is constrained to Literal["qemu", "lxc"].
- console_type is constrained to Literal["novnc", "term"].
- The returned ticket is opaque and one-time; it is not stored server-side.
- No eval, exec, os.system, pickle.loads, innerHTML, dangerouslySetInnerHTML.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.exception import ProxmoxAPIError
from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.session.proxmox import ProxmoxSession
from proxbox_api.session.proxmox_core import ProxmoxWebSocketAuth
from proxbox_api.session.proxmox_providers import _parse_db_endpoint
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

console_router = APIRouter()


class ConsoleSessionRequest(BaseModel):
    """Request body for creating a Proxmox console session ticket."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: int = Field(ge=1, description="proxbox-api ProxmoxEndpoint database ID")
    vmid: int = Field(ge=1, description="Proxmox VM or CT ID")
    node: str = Field(min_length=1, description="Proxmox node name")
    vm_type: Literal["qemu", "lxc"] = Field(
        description="VM type: qemu (QEMU/KVM) or lxc (container)"
    )
    console_type: Literal["novnc", "term"] = Field(
        default="novnc",
        description=(
            "novnc: graphical VNC console (QEMU only); "
            "term: xterm.js terminal console (QEMU and LXC)"
        ),
    )

    @model_validator(mode="after")
    def validate_lxc_no_novnc(self) -> "ConsoleSessionRequest":
        if self.vm_type == "lxc" and self.console_type == "novnc":
            raise ValueError("LXC containers do not support novnc; use console_type='term'")
        return self


class ConsoleWebSocketAuth(BaseModel):
    """Private authentication material for the trusted WebSocket relay."""

    kind: Literal["authorization", "cookie"]
    value: str = Field(min_length=1, repr=False)


class ConsoleSessionResponse(BaseModel):
    """Resolved console session details returned to the trusted relay."""

    ticket: str = Field(description="One-time Proxmox console ticket")
    port: int = Field(description="WebSocket port on the Proxmox host")
    proxmox_host: str = Field(description="Proxmox host FQDN or IP")
    proxmox_port: int = Field(description="Proxmox HTTPS port (default 8006)")
    ws_url: str = Field(description="wss:// URL ready for the browser relay")
    console_type: Literal["novnc", "term"]
    verify_ssl: bool
    websocket_auth: ConsoleWebSocketAuth


def _response_websocket_auth(auth: ProxmoxWebSocketAuth) -> ConsoleWebSocketAuth:
    """Translate the session-layer value into the bounded API contract."""
    return ConsoleWebSocketAuth(kind=auth.kind, value=auth.value)


def _build_ws_url(
    host: str,
    port: int,
    node: str,
    vm_type: str,
    vmid: int,
    ticket: str,
    vnc_port: int,
) -> str:
    """Build the wss:// WebSocket URL for noVNC or xterm.js console.

    Both noVNC and term use the vncwebsocket endpoint with the same URL shape:
      wss://{host}:{port}/api2/json/nodes/{node}/{vm_type}/{vmid}/vncwebsocket
        ?port={vnc_port}&vncticket={encoded_ticket}
    """
    encoded_ticket = quote(ticket, safe="")
    return (
        f"wss://{host}:{port}/api2/json/nodes/{node}/{vm_type}/{vmid}"
        f"/vncwebsocket?port={vnc_port}&vncticket={encoded_ticket}"
    )


async def _open_session(endpoint: ProxmoxEndpoint) -> ProxmoxSession:
    schema = _parse_db_endpoint(endpoint)
    return await ProxmoxSession.create(schema)


async def _load_endpoint(req: ConsoleSessionRequest, db_session: SessionDep) -> ProxmoxEndpoint:
    endpoint = await _maybe_await(db_session.get(ProxmoxEndpoint, req.endpoint_id))
    if endpoint is None:
        raise HTTPException(
            status_code=404,
            detail=f"No ProxmoxEndpoint with id={req.endpoint_id}.",
        )
    return endpoint


async def _connect_endpoint(endpoint: ProxmoxEndpoint) -> ProxmoxSession:
    try:
        return await _open_session(endpoint)
    except Exception as exc:
        logger.warning(
            "console: failed to open Proxmox session for endpoint %s: %s",
            endpoint.id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=502, detail="Unable to connect to Proxmox endpoint."
        ) from exc


async def _request_console_proxy(px: ProxmoxSession, req: ConsoleSessionRequest) -> object:
    guest = px.session.nodes(req.node)
    guest = guest.qemu(req.vmid) if req.vm_type == "qemu" else guest.lxc(req.vmid)
    try:
        if req.console_type == "novnc":
            return await resolve_async(guest.vncproxy.post(websocket=1))
        return await resolve_async(guest.termproxy.post())
    except ProxmoxAPIError as exc:
        logger.warning(
            "console: Proxmox %s/%s/%s/%s failed: %s",
            req.node,
            req.vm_type,
            req.vmid,
            req.console_type,
            exc,
        )
        raise HTTPException(status_code=502, detail=f"Proxmox console error: {exc}") from exc
    except Exception as exc:
        logger.warning(
            "console: unexpected error for %s/%s/%s: %s",
            req.node,
            req.vm_type,
            req.vmid,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Proxmox console request failed.") from exc


def _console_ticket(raw: object, req: ConsoleSessionRequest) -> tuple[str, int]:
    if hasattr(raw, "model_dump"):
        data = raw.model_dump(mode="python", exclude_none=True)
    elif hasattr(raw, "dict"):
        data = raw.dict(exclude_none=True)
    elif isinstance(raw, dict):
        data = raw
    else:
        data = {}
    if "data" in data and isinstance(data["data"], dict):
        data = data["data"]
    ticket = data.get("ticket")
    port = data.get("port")
    if isinstance(ticket, str) and ticket and isinstance(port, int):
        return ticket, port
    logger.error(
        "console: unexpected Proxmox response for %s/%s/%s",
        req.node,
        req.vm_type,
        req.vmid,
    )
    raise HTTPException(status_code=502, detail="Proxmox did not return a ticket/port.")


async def _console_websocket_auth(px: ProxmoxSession, endpoint_id: int) -> ConsoleWebSocketAuth:
    try:
        return _response_websocket_auth(await px.get_websocket_auth())
    except Exception as exc:
        logger.warning(
            "console: failed to prepare WebSocket authentication for endpoint %s: %s",
            endpoint_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=502, detail="Unable to authenticate the Proxmox console stream."
        ) from exc


@console_router.post("/sessions", response_model=ConsoleSessionResponse)
async def create_console_session(
    req: ConsoleSessionRequest,
    db_session: SessionDep,
) -> ConsoleSessionResponse:
    """Create a one-time session for the trusted nms-backend relay."""
    endpoint = await _load_endpoint(req, db_session)
    px = await _connect_endpoint(endpoint)
    raw = await _request_console_proxy(px, req)
    ticket, vnc_port = _console_ticket(raw, req)
    websocket_auth = await _console_websocket_auth(px, req.endpoint_id)
    host = endpoint.host
    proxmox_port = endpoint.port

    ws_url = _build_ws_url(
        host=host,
        port=proxmox_port,
        node=req.node,
        vm_type=req.vm_type,
        vmid=req.vmid,
        ticket=ticket,
        vnc_port=int(vnc_port),
    )

    return ConsoleSessionResponse(
        ticket=ticket,
        port=vnc_port,
        proxmox_host=host,
        proxmox_port=proxmox_port,
        ws_url=ws_url,
        console_type=req.console_type,
        verify_ssl=endpoint.verify_ssl,
        websocket_auth=websocket_auth,
    )
