"""WebSocket endpoints for counters, sync commands, and VM streaming."""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from proxbox_api.app import bootstrap
from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.netbox_rest import rest_create
from proxbox_api.routes.extras import CreateCustomFieldsDep
from proxbox_api.routes.proxmox.cluster import ClusterResourcesDep, ClusterStatusDep
from proxbox_api.routes.virtualization.virtual_machines import create_virtual_machines
from proxbox_api.services.sync.devices import create_proxmox_devices
from proxbox_api.session.proxmox import ProxmoxSessionsDep

websocket_router = APIRouter()


@websocket_router.websocket("/")
async def base_websocket(websocket: WebSocket) -> None:
    count = 0

    await websocket.accept()
    try:
        while True:
            count = count + 1
            await websocket.send_text(f"Message: {count}")
            await asyncio.sleep(2)

    except WebSocketDisconnect:
        print("WebSocket connection closed")


@websocket_router.websocket("/ws/virtual-machines")
async def websocket_virtual_machines(
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket: WebSocket,
) -> None:
    print("route ws/virtual-machines reached")

    try:
        await websocket.accept()
        await websocket.send_text("Connected!")
    except Exception as error:  # noqa: BLE001
        print(f"Error while accepting WebSocket connection: {error}")
        try:
            await websocket.close()
        except Exception as close_err:  # noqa: BLE001
            print(f"Error while closing WebSocket connection: {close_err}")

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
async def websocket_sync_commands(
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

    print("route ws reached")
    try:
        await websocket.accept()
        connection_open = True

        await websocket.send_text("Connected!")
    except Exception as error:  # noqa: BLE001
        print(f"Error while accepting WebSocket connection: {error}")
        try:
            await websocket.close()
        except Exception as close_err:  # noqa: BLE001
            print(f"Error while closing WebSocket connection: {close_err}")

    await websocket.send_text("Connected 2!")

    try:
        while True:
            try:
                data = await websocket.receive_text()
                print(f"Received message: {data}")
                await websocket.send_text(f"Received message: {data}")
            except Exception as error:  # noqa: BLE001
                print(f"Error while receiving data from WebSocket: {error}")
                break

            if data in {"Full Update Sync", "Full Update"}:
                sync_process = None

                try:
                    sync_process = rest_create(
                        nb,
                        "/api/plugins/proxbox/sync-processes/",
                        {
                            "name": f"sync-process-{datetime.now()}",
                            "sync_type": "all",
                            "status": "not-started",
                            "started_at": str(datetime.now()),
                        },
                    )
                except Exception as error:  # noqa: BLE001
                    print(error)

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

                if sync_process:
                    sync_process.status = "completed"
                    sync_process.completed_at = str(datetime.now())
                    sync_process.save()

            elif data == "Sync Nodes":
                print("Sync Nodes")
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
        print(f"WebSocket Disconnected: {error}")
        connection_open = False
    finally:
        if connection_open and websocket.client_state.CONNECTED:
            await websocket.close(code=1000, reason=None)


def register_websocket_routes(app) -> None:
    """Mount WebSocket routes on the root application."""
    app.include_router(websocket_router)
