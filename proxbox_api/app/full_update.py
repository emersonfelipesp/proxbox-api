"""Full device + VM synchronization HTTP and SSE endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.routes.extras import CreateCustomFieldsDep
from proxbox_api.routes.proxmox.cluster import ClusterResourcesDep, ClusterStatusDep
from proxbox_api.routes.virtualization.virtual_machines import create_virtual_machines
from proxbox_api.services.sync.devices import create_proxmox_devices
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_event

full_update_router = APIRouter()


@full_update_router.get("/full-update")
async def full_update_sync(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
) -> dict:
    sync_nodes: list = []
    sync_vms: list = []

    try:
        sync_nodes = await create_proxmox_devices(
            netbox_session=netbox_session,
            clusters_status=cluster_status,
            node=None,
            tag=tag,
            use_websocket=False,
        )
    except ProxboxException:
        raise
    except Exception as error:  # noqa: BLE001
        logger.exception("Error while syncing nodes during full-update")
        raise ProxboxException(message="Error while syncing nodes.", python_exception=str(error)) from error

    try:
        sync_vms = await create_virtual_machines(
            netbox_session=netbox_session,
            pxs=pxs,
            cluster_status=cluster_status,
            cluster_resources=cluster_resources,
            custom_fields=custom_fields,
            tag=tag,
            use_websocket=False,
        )
    except ProxboxException:
        raise
    except Exception as error:  # noqa: BLE001
        logger.exception("Error while syncing virtual machines during full-update")
        raise ProxboxException(
            message="Error while syncing virtual machines.",
            python_exception=str(error),
        ) from error

    return {
        "status": "completed",
        "devices": sync_nodes,
        "virtual_machines": sync_vms,
        "devices_count": len(sync_nodes),
        "virtual_machines_count": len(sync_vms),
    }


@full_update_router.get("/full-update/stream", response_model=None)
async def full_update_sync_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
) -> StreamingResponse:
    async def event_stream():
        sync_nodes: list = []
        sync_vms: list = []
        devices_bridge = WebSocketSSEBridge()
        vm_bridge = WebSocketSSEBridge()
        try:
            yield sse_event(
                "step",
                {
                    "step": "stream",
                    "status": "started",
                    "message": "Full update stream connected.",
                },
            )
            yield sse_event(
                "step",
                {
                    "step": "devices",
                    "status": "started",
                    "message": "Starting devices synchronization.",
                },
            )

            async def _run_devices_sync():
                try:
                    return await create_proxmox_devices(
                        netbox_session=netbox_session,
                        clusters_status=cluster_status,
                        node=None,
                        tag=tag,
                        websocket=devices_bridge,
                        use_websocket=True,
                    )
                finally:
                    await devices_bridge.close()

            devices_task = asyncio.create_task(_run_devices_sync())
            async for frame in devices_bridge.iter_sse():
                yield frame
            sync_nodes = await devices_task

            yield sse_event(
                "step",
                {
                    "step": "devices",
                    "status": "completed",
                    "message": "Devices synchronization finished.",
                    "result": {"count": len(sync_nodes)},
                },
            )

            yield sse_event(
                "step",
                {
                    "step": "virtual-machines",
                    "status": "started",
                    "message": "Starting virtual machines synchronization.",
                },
            )

            async def _run_vms_sync():
                try:
                    return await create_virtual_machines(
                        netbox_session=netbox_session,
                        pxs=pxs,
                        cluster_status=cluster_status,
                        cluster_resources=cluster_resources,
                        custom_fields=custom_fields,
                        tag=tag,
                        websocket=vm_bridge,
                        use_websocket=True,
                    )
                finally:
                    await vm_bridge.close()

            vms_task = asyncio.create_task(_run_vms_sync())
            async for frame in vm_bridge.iter_sse():
                yield frame
            sync_vms = await vms_task

            yield sse_event(
                "step",
                {
                    "step": "virtual-machines",
                    "status": "completed",
                    "message": "Virtual machines synchronization finished.",
                    "result": {"count": len(sync_vms)},
                },
            )

            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Full update sync completed.",
                    "result": {
                        "devices": sync_nodes,
                        "virtual_machines": sync_vms,
                        "devices_count": len(sync_nodes),
                        "virtual_machines_count": len(sync_vms),
                    },
                },
            )
        except ProxboxException as error:
            yield sse_event(
                "error",
                {
                    "step": "full-update",
                    "status": "failed",
                    "error": error.message,
                    "detail": error.detail,
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": error.message,
                    "errors": [{"detail": error.detail or error.message}],
                },
            )
        except Exception as error:  # noqa: BLE001
            yield sse_event(
                "error",
                {
                    "step": "full-update",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Full update sync failed.",
                    "errors": [{"detail": str(error)}],
                },
            )
        finally:
            return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def register_full_update_routes(app) -> None:
    """Mount full-update routes on the root application."""
    app.include_router(full_update_router)
