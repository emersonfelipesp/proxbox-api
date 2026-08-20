"""WebSocket endpoints for counters, sync commands, and VM streaming."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from proxbox_api.app import bootstrap
from proxbox_api.auth import check_auth_header
from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.logger import logger
from proxbox_api.routes.extras import CreateCustomFieldsDep
from proxbox_api.routes.proxmox.cluster import ClusterResourcesDep, ClusterStatusDep
from proxbox_api.routes.virtualization.virtual_machines import create_virtual_machines
from proxbox_api.services.auth_lockout import AuthSourceContext, resolve_auth_source_context
from proxbox_api.services.sync.devices import create_proxmox_devices
from proxbox_api.session.proxmox import ProxmoxSessionsDep

websocket_router = APIRouter()

AUTH_MESSAGE_SCHEMA = {"type": "object", "properties": {"api_key": {"type": "string"}}}


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


@websocket_router.websocket("/ws/virtual-machines")
async def websocket_virtual_machines(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket: WebSocket,
) -> None:
    logger.info("WebSocket /ws/virtual-machines connection attempt")

    try:
        await websocket.accept()
    except Exception:  # noqa: BLE001
        return

    try:
        api_key = _parse_api_key_message(await websocket.receive_text())

        client_source = _get_client_source(websocket)
        if not await _do_ws_auth(websocket, api_key, client_source):
            logger.warning("WebSocket /ws/virtual-machines auth failed")
            return

        await websocket.send_text("Connected!")
    except Exception:  # noqa: BLE001
        logger.exception("Error while accepting WebSocket /ws/virtual-machines")
        try:
            await websocket.close(code=1011)
        except Exception as close_err:  # noqa: BLE001
            logger.warning("Error while closing WebSocket after accept failure: %s", close_err)
        return

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
        custom_fields=custom_fields,
        websocket=websocket,
        tag=tag,
        use_css=False,
    )


@websocket_router.websocket("/ws")
async def websocket_sync_commands(  # noqa: C901
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket: WebSocket,
) -> None:
    connection_open = False

    nb = netbox_session

    logger.info("WebSocket /ws connection attempt")

    try:
        await websocket.accept()
    except Exception:  # noqa: BLE001
        try:
            await websocket.close(code=1011)
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        api_key = _parse_api_key_message(await websocket.receive_text())

        client_source = _get_client_source(websocket)
        if not await _do_ws_auth(websocket, api_key, client_source):
            logger.warning("WebSocket /ws auth failed")
            return

        connection_open = True
        await websocket.send_text("Connected!")

        await websocket.send_text("Connected 2!")
    except Exception:  # noqa: BLE001
        logger.exception("Error while accepting WebSocket /ws")
        try:
            await websocket.close(code=1011)
        except Exception as close_err:  # noqa: BLE001
            logger.warning("Error while closing WebSocket after accept failure: %s", close_err)
        return

    if not connection_open:
        return

    try:
        while True:
            try:
                data = await websocket.receive_text()
                logger.debug("WebSocket /ws received: %s", data)
            except Exception as error:  # noqa: BLE001
                logger.warning("Error while receiving WebSocket /ws data: %s", error)
                break

            data = data.strip()
            if data in {"Full Update Sync", "Full Update"}:
                sync_nodes = await create_proxmox_devices(
                    netbox_session=nb,
                    clusters_status=cluster_status,
                    node=None,
                    websocket=websocket,
                    tag=tag,
                    use_websocket=True,
                )

                if sync_nodes:
                    await create_virtual_machines(
                        netbox_session=nb,
                        pxs=pxs,
                        cluster_status=cluster_status,
                        cluster_resources=cluster_resources,
                        custom_fields=custom_fields,
                        websocket=websocket,
                        tag=tag,
                        use_websocket=True,
                    )

            elif data == "Sync Nodes":
                logger.info("WebSocket /ws: Sync Nodes command")
                await websocket.send_text("Sync Nodes")
                await create_proxmox_devices(
                    netbox_session=nb,
                    clusters_status=cluster_status,
                    node=None,
                    websocket=websocket,
                    tag=tag,
                    use_websocket=True,
                )

            elif data == "Sync Virtual Machines":
                await create_virtual_machines(
                    netbox_session=nb,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    cluster_resources=cluster_resources,
                    custom_fields=custom_fields,
                    websocket=websocket,
                    tag=tag,
                    use_websocket=True,
                )

            else:
                await websocket.send_text(f"Invalid command: {data}")
                await websocket.send_text(
                    "Valid commands: 'Sync Nodes', 'Sync Virtual Machines', 'Full Update Sync'"
                )

    except WebSocketDisconnect as error:
        logger.info("WebSocket /ws disconnected: %s", error)
        connection_open = False
    finally:
        if connection_open and websocket.client_state.CONNECTED:
            await websocket.close(code=1000, reason=None)


def register_websocket_routes(app) -> None:
    """Mount WebSocket routes on the root application."""
    app.include_router(websocket_router)
