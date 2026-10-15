"""WebSocket endpoints for counters, sync commands, and VM streaming."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, WebSocketException

from proxbox_api.app import bootstrap
from proxbox_api.auth import check_auth_header
from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.logger import logger
from proxbox_api.routes.proxmox.cluster import ClusterResourcesDep, ClusterStatusDep
from proxbox_api.routes.virtualization.virtual_machines import create_virtual_machines
from proxbox_api.services.auth_lockout import AuthSourceContext, resolve_auth_source_context
from proxbox_api.services.interactive_policy import require_interactive
from proxbox_api.services.sync.devices import create_proxmox_devices
from proxbox_api.session.proxmox import ProxmoxSessionsDep

websocket_router = APIRouter()

AUTH_MESSAGE_SCHEMA = {"type": "object", "properties": {"api_key": {"type": "string"}}}
_SYNC_COMMANDS = frozenset(
    {"Full Update Sync", "Full Update", "Sync Nodes", "Sync Virtual Machines"}
)
_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,64}")
_MAX_SYNC_COMMAND_ENVELOPE_BYTES = 4096


@dataclass(frozen=True, slots=True)
class _ParsedSyncCommand:
    command: str | None
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class _SyncCommandContext:
    netbox_session: object
    pxs: object
    cluster_status: object
    cluster_resources: object
    tag: object


class _RunCorrelatedWebSocket:
    """Add one command's run identifier to every emitted command frame."""

    def __init__(self, websocket: WebSocket, run_id: str) -> None:
        self._websocket = websocket
        self.run_id = run_id

    async def send_json(self, payload: dict[str, object], mode: str = "text") -> None:
        await self._websocket.send_json({**payload, "run_id": self.run_id}, mode=mode)

    async def send_text(self, data: str) -> None:
        await self.send_json({"message": data})

    def __getattr__(self, name: str) -> Any:
        return getattr(self._websocket, name)


def _parse_api_key_message(message: str) -> str | None:
    """Return only a string API key from one JSON-object auth frame."""

    try:
        payload = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    api_key = payload.get("api_key")
    return api_key if isinstance(api_key, str) else None


async def _do_ws_auth(
    websocket: WebSocket,
    api_key: str | None,
    client_source: AuthSourceContext,
) -> bool:
    authorized, error_message = await asyncio.to_thread(check_auth_header, api_key, client_source)
    if not authorized:
        close_code = 1013 if "verification capacity" in (error_message or "") else 4001
        await websocket.close(code=close_code, reason=error_message or "Authentication failed")
        return False
    return True


def _get_client_source(websocket: WebSocket) -> AuthSourceContext:
    # Reuse the request-side resolver for trusted-proxy semantics.
    from proxbox_api.app.factory import _TRUSTED_PROXIES

    peer_ip = websocket.client.host if websocket.client else None
    return resolve_auth_source_context(
        peer_ip,
        websocket.headers.get("x-forwarded-for"),
        _TRUSTED_PROXIES,
    )


@websocket_router.websocket("/")
async def base_websocket(websocket: WebSocket) -> None:
    count = 0
    authenticated = False

    try:
        await websocket.accept()
    except Exception:  # noqa: BLE001
        return

    try:
        api_key = _parse_api_key_message(await websocket.receive_text())

        client_source = _get_client_source(websocket)

        if not await _do_ws_auth(websocket, api_key, client_source):
            logger.warning("WebSocket / auth failed")
            return

        authenticated = True
    except Exception:  # noqa: BLE001
        logger.exception("Error in WebSocket / auth")
        return

    try:
        while True:
            count = count + 1
            await websocket.send_text(f"Message: {count}")
            await asyncio.sleep(2)

    except WebSocketDisconnect:
        logger.info("WebSocket / connection closed (authenticated: %s)", authenticated)


async def _authorize_sync_websocket(websocket: WebSocket) -> None:
    """Authenticate before any credential, collector, or tag dependency runs.

    The pinned FastAPI solver traverses route-level dependencies before the
    handler's provider graph. Mounted ASGI ordering tests are a mandatory
    dependency-upgrade gate; do not move this into an auth/client wrapper.
    """
    require_interactive()
    await websocket.accept()
    try:
        message = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        authorized, error = await asyncio.to_thread(
            check_auth_header, _parse_api_key_message(message), _get_client_source(websocket)
        )
    except Exception as exc:
        raise WebSocketException(code=4001, reason="Authentication failed") from exc
    if not authorized:
        code = 1013 if "verification capacity" in (error or "") else 4001
        raise WebSocketException(code=code, reason="Authentication failed")
    require_interactive()


@websocket_router.websocket(
    "/ws/virtual-machines", dependencies=[Depends(_authorize_sync_websocket)]
)
async def websocket_virtual_machines(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    websocket: WebSocket,
) -> None:
    require_interactive()
    await websocket.send_text("Connected!")

    if bootstrap.netbox_session is None:
        msg = (
            "Error: NetBox session is not available. "
            "Check database connectivity and NetBox endpoint configuration."
        )
        try:
            await websocket.send_text(msg)
            await websocket.close(code=1011)
        except Exception as send_err:  # noqa: BLE001
            logger.warning("Could not notify client about missing NetBox session: %s", send_err)
        return

    await create_virtual_machines(
        netbox_session=bootstrap.netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        websocket=websocket,
        tag=tag,
        use_css=False,
    )


def _parse_sync_command_frame(frame: str) -> _ParsedSyncCommand:
    """Parse a legacy command or a closed run-correlated command envelope."""
    stripped = frame.strip()
    if stripped in _SYNC_COMMANDS:
        return _ParsedSyncCommand(command=stripped)
    if not stripped.startswith("{"):
        return _ParsedSyncCommand(command=stripped)
    if len(frame) > _MAX_SYNC_COMMAND_ENVELOPE_BYTES:
        return _ParsedSyncCommand(command=None)
    if len(frame.encode("utf-8")) > _MAX_SYNC_COMMAND_ENVELOPE_BYTES:
        return _ParsedSyncCommand(command=None)
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError, RecursionError):
        return _ParsedSyncCommand(command=None)
    if not isinstance(payload, dict):
        return _ParsedSyncCommand(command=stripped)

    raw_run_id = payload.get("run_id")
    run_id = (
        raw_run_id
        if isinstance(raw_run_id, str) and _RUN_ID_PATTERN.fullmatch(raw_run_id)
        else None
    )
    if set(payload) != {"command", "run_id"} or not isinstance(payload.get("command"), str):
        return _ParsedSyncCommand(command=None, run_id=run_id)
    if run_id is None:
        return _ParsedSyncCommand(command=None)
    return _ParsedSyncCommand(command=payload["command"].strip(), run_id=run_id)


async def _send_invalid_sync_command(websocket: object) -> None:
    await websocket.send_text("Invalid sync command")
    await websocket.send_text(
        "Valid commands: 'Sync Nodes', 'Sync Virtual Machines', 'Full Update Sync'"
    )


async def _sync_devices(context: _SyncCommandContext, websocket: object) -> list:
    return await create_proxmox_devices(
        netbox_session=context.netbox_session,
        clusters_status=context.cluster_status,
        node=None,
        websocket=websocket,
        tag=context.tag,
        use_websocket=True,
    )


async def _sync_virtual_machines(
    context: _SyncCommandContext,
    websocket: object,
    run_id: str | None,
) -> object:
    return await create_virtual_machines(
        netbox_session=context.netbox_session,
        pxs=context.pxs,
        cluster_status=context.cluster_status,
        cluster_resources=context.cluster_resources,
        websocket=websocket,
        tag=context.tag,
        use_websocket=True,
        run_id=run_id,
    )


async def _send_sync_failure(websocket: object, stage: str) -> None:
    await websocket.send_json(
        {
            "event": "error_detail",
            "phase": stage,
            "category": "internal",
            "message": f"{stage} synchronization failed.",
        }
    )


def _sync_result_failed(result: object) -> bool:
    failed_count = (
        result.get("failed_count", 0)
        if isinstance(result, dict)
        else getattr(result, "failed_count", 0)
    )
    return isinstance(failed_count, int) and failed_count > 0


async def _correlated_stage_outcome(
    websocket: object,
    stage: str,
    result: object = None,
    *,
    failed: bool = False,
) -> str:
    stage_failed = failed or _sync_result_failed(result)
    if stage_failed:
        await _send_sync_failure(websocket, stage)
    return "failed" if stage_failed else "completed"


async def _run_correlated_full_update(
    context: _SyncCommandContext,
    websocket: _RunCorrelatedWebSocket,
) -> None:
    stages = {"devices": "skipped", "virtual-machines": "skipped"}
    try:
        sync_nodes = await _sync_devices(context, websocket)
    except Exception:  # noqa: BLE001
        logger.exception("WebSocket /ws devices stage failed")
        stages["devices"] = await _correlated_stage_outcome(websocket, "devices", failed=True)
    else:
        stages["devices"] = await _correlated_stage_outcome(websocket, "devices", sync_nodes)
        if sync_nodes:
            try:
                vm_result = await _sync_virtual_machines(context, websocket, websocket.run_id)
            except Exception:  # noqa: BLE001
                logger.exception("WebSocket /ws virtual-machines stage failed")
                stages["virtual-machines"] = await _correlated_stage_outcome(
                    websocket, "virtual-machines", failed=True
                )
            else:
                stages["virtual-machines"] = await _correlated_stage_outcome(
                    websocket, "virtual-machines", vm_result
                )

    await websocket.send_json(
        {
            "object": "full-update",
            "end": True,
            "status": "failed" if "failed" in stages.values() else "completed",
            "stages": stages,
        }
    )


async def _run_correlated_stage(
    command: str,
    context: _SyncCommandContext,
    websocket: _RunCorrelatedWebSocket,
) -> None:
    stage = "devices" if command == "Sync Nodes" else "virtual-machines"
    try:
        if command == "Sync Nodes":
            logger.info("WebSocket /ws: Sync Nodes command")
            await websocket.send_text("Sync Nodes")
            result = await _sync_devices(context, websocket)
        else:
            result = await _sync_virtual_machines(context, websocket, websocket.run_id)
    except Exception:  # noqa: BLE001
        logger.exception("WebSocket /ws %s stage failed", stage)
        await _correlated_stage_outcome(websocket, stage, failed=True)
    else:
        await _correlated_stage_outcome(websocket, stage, result)


async def _run_legacy_command(
    command: str,
    context: _SyncCommandContext,
    websocket: WebSocket,
) -> None:
    if command in {"Full Update Sync", "Full Update"}:
        sync_nodes = await _sync_devices(context, websocket)
        if sync_nodes:
            await _sync_virtual_machines(context, websocket, None)
    elif command == "Sync Nodes":
        logger.info("WebSocket /ws: Sync Nodes command")
        await websocket.send_text("Sync Nodes")
        await _sync_devices(context, websocket)
    elif command == "Sync Virtual Machines":
        await _sync_virtual_machines(context, websocket, None)


async def _handle_sync_command(
    parsed: _ParsedSyncCommand,
    context: _SyncCommandContext,
    websocket: WebSocket,
) -> None:
    correlated = (
        _RunCorrelatedWebSocket(websocket, parsed.run_id) if parsed.run_id is not None else None
    )
    response_socket = correlated or websocket
    if parsed.command not in _SYNC_COMMANDS:
        await _send_invalid_sync_command(response_socket)
    elif correlated is None:
        await _run_legacy_command(parsed.command, context, websocket)
    elif parsed.command in {"Full Update Sync", "Full Update"}:
        await _run_correlated_full_update(context, correlated)
    else:
        await _run_correlated_stage(parsed.command, context, correlated)


@websocket_router.websocket("/ws", dependencies=[Depends(_authorize_sync_websocket)])
async def websocket_sync_commands(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    websocket: WebSocket,
) -> None:
    require_interactive()
    context = _SyncCommandContext(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        tag=tag,
    )
    await websocket.send_text("Connected!")
    await websocket.send_text("Connected 2!")

    try:
        while True:
            try:
                data = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            require_interactive()
            await _handle_sync_command(_parse_sync_command_frame(data), context, websocket)

    except WebSocketDisconnect:
        logger.info("WebSocket /ws disconnected")
    finally:
        try:
            await websocket.close(code=1000, reason=None)
        except RuntimeError:
            pass


def register_websocket_routes(app) -> None:
    """Mount WebSocket routes on the root application."""
    app.include_router(websocket_router)
