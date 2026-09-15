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

import re
from datetime import datetime, timezone
from ipaddress import AddressValueError, IPv6Address
from typing import Literal
from urllib.parse import SplitResult, quote, urlsplit

from fastapi import APIRouter, HTTPException, WebSocket
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.exception import ProxmoxAPIError
from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.services import console_relay, console_relay_policy
from proxbox_api.session.proxmox import ProxmoxSession
from proxbox_api.session.proxmox_core import ProxmoxWebSocketAuth
from proxbox_api.session.proxmox_providers import _parse_db_endpoint
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

console_router = APIRouter()
_NODE_NAME_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,253}[A-Za-z0-9])?")


class ConsoleSessionRequest(BaseModel):
    """Request body for creating a Proxmox console session ticket."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: int = Field(ge=1, description="proxbox-api ProxmoxEndpoint database ID")
    vmid: int = Field(ge=1, description="Proxmox VM or CT ID")
    node: str = Field(min_length=1, max_length=255, description="Proxmox node name")
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

    @field_validator("node")
    @classmethod
    def validate_node_path_segment(cls, value: str) -> str:
        if not value.isascii() or _NODE_NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("node must be a safe Proxmox node name")
        return value


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


class BrowserConsoleSessionRequest(ConsoleSessionRequest):
    """Browser relay request bound to one exact serialized HTTPS Origin."""

    origin: str = Field(min_length=1, max_length=console_relay.MAX_ORIGIN_LENGTH)

    @field_validator("origin")
    @classmethod
    def validate_https_origin(cls, value: str) -> str:
        _validate_origin_text(value)
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise ValueError("origin must be a valid HTTPS origin") from exc
        _validate_origin_authority(parsed)
        _validate_origin_shape(value, parsed)
        return value


def _validate_origin_text(value: str) -> None:
    if value != value.strip() or not value.isascii():
        raise ValueError("origin must be an ASCII HTTPS origin")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("origin must be an ASCII HTTPS origin")


def _validate_origin_authority(parsed: SplitResult) -> None:
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("origin must be a valid HTTPS origin") from exc
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("origin must be a valid HTTPS origin")
    if any(character.isspace() for character in parsed.hostname):
        raise ValueError("origin must be a valid HTTPS origin")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("origin must be a valid HTTPS origin")


def _validate_origin_shape(value: str, parsed: SplitResult) -> None:
    if parsed.scheme != "https" or value != f"https://{parsed.netloc}":
        raise ValueError("origin must be a valid HTTPS origin")
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError("origin must be a valid HTTPS origin")


class BrowserConsoleSessionResponse(BaseModel):
    """Only browser-safe routing data; no Proxmox transport material."""

    stream_token: str
    websocket_path: str
    expires_at: datetime
    console_type: Literal["novnc", "term"]


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
    authority_host = _websocket_authority_host(host)
    encoded_node = quote(node, safe="")
    encoded_ticket = quote(ticket, safe="")
    return (
        f"wss://{authority_host}:{port}/api2/json/nodes/{encoded_node}/{vm_type}/{vmid}"
        f"/vncwebsocket?port={vnc_port}&vncticket={encoded_ticket}"
    )


def _websocket_authority_host(host: str) -> str:
    bracketed = host.startswith("[") and host.endswith("]")
    raw_host = host[1:-1] if bracketed else host
    if ":" not in raw_host:
        if bracketed:
            raise HTTPException(status_code=502, detail="Invalid Proxmox console host.")
        return raw_host
    try:
        address = IPv6Address(raw_host)
    except AddressValueError as exc:
        raise HTTPException(status_code=502, detail="Invalid Proxmox console host.") from exc
    if address.scope_id is not None:
        raise HTTPException(status_code=502, detail="Invalid Proxmox console host.")
    return f"[{address.compressed}]"


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
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="Proxmox console error.") from exc
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
    port = _console_port(data.get("port"))
    if isinstance(ticket, str) and ticket and port is not None:
        return ticket, port
    logger.error(
        "console: unexpected Proxmox response for %s/%s/%s",
        req.node,
        req.vm_type,
        req.vmid,
    )
    raise HTTPException(status_code=502, detail="Proxmox did not return a ticket/port.")


def _console_port(value: object) -> int | None:
    """Normalize Proxmox's integer or decimal-string console port."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        port = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        port = int(value)
    else:
        return None
    return port if 1 <= port <= 65535 else None


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
    return await _create_console_session_for_endpoint(req, endpoint)


async def _create_console_session_for_endpoint(
    req: ConsoleSessionRequest,
    endpoint: ProxmoxEndpoint,
) -> ConsoleSessionResponse:
    """Broker private Proxmox state for one already resolved endpoint."""

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


@console_router.post(
    "/browser-sessions",
    response_model=BrowserConsoleSessionResponse,
    status_code=201,
)
async def create_browser_console_session(
    req: BrowserConsoleSessionRequest,
    db_session: SessionDep,
) -> BrowserConsoleSessionResponse:
    """Create encrypted one-use state for the standalone browser relay."""

    endpoint = await _load_endpoint(req, db_session)
    try:
        console_relay_policy.require_console_relay_endpoint_enabled(endpoint)
        console_relay_policy.require_console_relay_policy(endpoint, stage="create")
    except console_relay_policy.ConsoleRelayPolicyDenied as exc:
        raise HTTPException(status_code=403, detail="Browser console access is denied.") from exc
    try:
        console_relay.require_relay_encryption()
    except console_relay.ConsoleRelayUnavailable as exc:
        logger.warning("console relay: encryption preflight refused: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="Browser console relay is unavailable.",
        ) from exc
    try:
        private = await _create_console_session_for_endpoint(req, endpoint)
    except HTTPException as exc:
        raise HTTPException(
            status_code=502,
            detail="Browser console session is unavailable.",
        ) from exc

    try:
        payload = console_relay.ConsoleRelayPayload(
            endpoint_id=req.endpoint_id,
            vmid=req.vmid,
            node=req.node,
            vm_type=req.vm_type,
            console_type=req.console_type,
            origin=req.origin,
            ws_url=private.ws_url,
            ticket=private.ticket,
            verify_ssl=private.verify_ssl,
            auth_kind=private.websocket_auth.kind,
            auth_value=private.websocket_auth.value,
        )
        token, expires_at = await console_relay.create_relay_session(db_session, payload)
    except (ValidationError, console_relay.ConsoleRelayUnavailable) as exc:
        logger.warning("console relay: create refused: %s", type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="Browser console relay is unavailable.",
        ) from exc
    return BrowserConsoleSessionResponse(
        stream_token=token,
        websocket_path="/proxmox/console/browser-stream",
        expires_at=datetime.fromtimestamp(expires_at, tz=timezone.utc),
        console_type=req.console_type,
    )


async def _close_browser_socket(websocket: WebSocket, *, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        return


def _browser_stream_token(websocket: WebSocket) -> str:
    if websocket.scope.get("query_string"):
        raise console_relay.ConsoleRelayRejected("Browser console session is invalid.")
    return console_relay.parse_browser_subprotocols(websocket.headers.get("sec-websocket-protocol"))


@console_router.websocket("/browser-stream")
async def browser_console_stream(
    websocket: WebSocket,
    db_session: SessionDep,
) -> None:
    """Consume one Origin-bound token and relay the selected Proxmox console."""

    upstream: console_relay.RelayUpstream | None = None
    accepted = False
    try:
        token = _browser_stream_token(websocket)
        payload = await console_relay.consume_relay_session(db_session, token)
        console_relay.validate_origin_binding(payload.origin, websocket.headers.get("origin"))
        endpoint = await db_session.get(ProxmoxEndpoint, payload.endpoint_id)
        if endpoint is None:
            raise console_relay.ConsoleRelayRejected("Browser console session is invalid.")
        try:
            console_relay_policy.require_console_relay_endpoint_enabled(endpoint)
            console_relay_policy.require_console_relay_policy(endpoint, stage="consume")
        except console_relay_policy.ConsoleRelayPolicyDenied as exc:
            raise console_relay.ConsoleRelayRejected("Browser console session is invalid.") from exc

        upstream = await console_relay.open_upstream(payload)
        if upstream.subprotocol != "binary":
            raise console_relay.ConsoleRelayProtocolError("Console stream failed.")
        await websocket.accept(subprotocol="binary")
        accepted = True
        if payload.console_type == "novnc":
            await console_relay.mediate_rfb_auth(websocket, upstream, payload.ticket)
        await console_relay.relay_frames(websocket, upstream)
    except console_relay.ConsoleRelayRejected as exc:
        logger.warning("console relay: browser session rejected: %s", type(exc).__name__)
        await _close_browser_socket(
            websocket,
            code=1008,
            reason="Browser console session rejected.",
        )
    except Exception as exc:
        logger.warning("console relay: stream failed: %s", type(exc).__name__)
        await _close_browser_socket(
            websocket,
            code=1011,
            reason="Console stream unavailable.",
        )
    else:
        if accepted:
            await _close_browser_socket(websocket, code=1000, reason="Console relay closed.")
    finally:
        if upstream is not None:
            await console_relay.close_upstream(upstream)
