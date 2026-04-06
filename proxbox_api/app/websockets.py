"""WebSocket endpoints for counters, sync commands, and VM streaming."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from proxbox_api.app import bootstrap
from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.logger import logger
from proxbox_api.routes.extras import CreateCustomFieldsDep
from proxbox_api.routes.proxmox.cluster import ClusterResourcesDep, ClusterStatusDep
from proxbox_api.routes.virtualization.virtual_machines import create_virtual_machines
from proxbox_api.services.sync.devices import create_proxmox_devices
from proxbox_api.session.proxmox import ProxmoxSessionsDep

websocket_router = APIRouter()


def _verify_ws_api_key(api_key: str | None) -> bool:
    """Verify WebSocket API key against configured key."""
    raw_key = os.environ.get("PROXBOX_API_KEY", "").strip()
    if not raw_key:
        return True
    if not api_key:
        return False
    stored_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    provided_hash = hashlib.sha256(api_key.encode()).hexdigest()
    return secrets.compare_digest(provided_hash, stored_hash)


async def _authenticate_websocket(websocket: WebSocket, api_key: str | None) -> bool:
    """Authenticate a WebSocket connection. Returns True if authenticated."""
    raw_key = os.environ.get("PROXBOX_API_KEY", "").strip()
    if not raw_key:
        return True
    if not _verify_ws_api_key(api_key):
        await websocket.close(code=4001, reason="Invalid or missing API key")
        return False
    return True


@websocket_router.websocket("/")
async def base_websocket(websocket: WebSocket) -> None:
    count = 0

    try:
        await websocket.accept()
    except Exception:  # noqa: BLE001
        return

    try:
        while True:
            count = count + 1
            await websocket.send_text(f"Message: {count}")
            await asyncio.sleep(2)

    except WebSocketDisconnect:
        logger.info("WebSocket / connection closed")


@websocket_router.websocket("/ws/virtual-machines")
async def websocket_virtual_machines(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket: WebSocket,
    api_key: str | None = None,
) -> None:
    logger.info("WebSocket /ws/virtual-machines connection attempt")

    if not await _authenticate_websocket(websocket, api_key):
        logger.warning("WebSocket /ws/virtual-machines auth failed")
        try:
            await websocket.close(code=4001, reason="Authentication failed")
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        await websocket.accept()
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
    api_key: str | None = None,
) -> None:
    connection_open = False

    nb = netbox_session

    logger.info("WebSocket /ws connection attempt")

    if not await _authenticate_websocket(websocket, api_key):
        logger.warning("WebSocket /ws auth failed")
        try:
            await websocket.close(code=4001, reason="Authentication failed")
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        await websocket.accept()
        connection_open = True

        await websocket.send_text("Connected!")
    except Exception:  # noqa: BLE001
        logger.exception("Error while accepting WebSocket /ws")
        try:
            await websocket.close(code=1011)
        except Exception as close_err:  # noqa: BLE001
            logger.warning("Error while closing WebSocket after accept failure: %s", close_err)

    if not connection_open:
        return

    try:
        await websocket.send_text("Connected 2!")
    except Exception as error:  # noqa: BLE001
        logger.warning("Could not send secondary WebSocket greeting: %s", error)
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
