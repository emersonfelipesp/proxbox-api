"""Virtual disk sync routes."""

# FastAPI Imports
import asyncio

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import (
    NetBoxSessionDep,  # NetBox Session
    ProxboxTagDep,  # Proxbox Tag
)

# NetBox compatibility wrappers
from proxbox_api.routes.proxmox.cluster import (
    ClusterResourcesDep,
    ClusterStatusDep,
)  # Cluster Status and Resources
from proxbox_api.services.sync.virtual_disks import (
    create_virtual_disks as sync_virtual_disks,
)
from proxbox_api.services.sync.vm_helpers import parse_comma_separated_ints
from proxbox_api.session.proxmox import ProxmoxSessionsDep  # Sessions
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_event

router = APIRouter()


@router.get("/virtual-disks/create")
async def create_virtual_disks(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    websocket=None,
    use_css: bool = False,
    use_websocket: bool = False,
    netbox_vm_ids: str | None = Query(
        default=None,
        title="NetBox VM IDs",
        description="Comma-separated list of NetBox VM IDs to sync. When provided, only these VMs will be synced.",
    ),
):
    """
    Syncs virtual disks for existing Virtual Machines in NetBox.

    Queries NetBox for VMs with cf_proxmox_vm_id set, fetches their disk
    configuration from Proxmox, and creates/updates Virtual Disk objects.
    """
    netbox_vm_id_list = None
    vm_ids = parse_comma_separated_ints(netbox_vm_ids)
    if vm_ids:
        netbox_vm_id_list = vm_ids

    result = await sync_virtual_disks(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        tag=tag,
        websocket=websocket,
        use_websocket=use_websocket,
        use_css=use_css,
        netbox_vm_ids=netbox_vm_id_list,
    )
    return result


@router.get("/virtual-disks/create/stream", response_model=None)
async def create_virtual_disks_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    netbox_vm_ids: str | None = Query(
        default=None,
        title="NetBox VM IDs",
        description="Comma-separated list of NetBox VM IDs to sync. When provided, only these VMs will be synced.",
    ),
):
    netbox_vm_id_list = None
    vm_ids = parse_comma_separated_ints(netbox_vm_ids)
    if vm_ids:
        netbox_vm_id_list = vm_ids

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await sync_virtual_disks(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    cluster_resources=cluster_resources,
                    tag=tag,
                    websocket=bridge,
                    use_websocket=True,
                    use_css=False,
                    netbox_vm_ids=netbox_vm_id_list,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        try:
            yield sse_event(
                "step",
                {
                    "step": "virtual-disks",
                    "status": "started",
                    "message": "Starting virtual disks synchronization."
                    if not netbox_vm_id_list
                    else f"Starting virtual disks synchronization for {len(netbox_vm_id_list)} VM(s).",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame

            result = await sync_task
            yield sse_event(
                "step",
                {
                    "step": "virtual-disks",
                    "status": "completed",
                    "message": "Virtual disks synchronization finished.",
                    "result": {
                        "count": result.get("count", 0),
                        "created": result.get("created", 0),
                        "updated": result.get("updated", 0),
                        "skipped": result.get("skipped", 0),
                    },
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Virtual disks sync completed.",
                    "result": result,
                },
            )
        except Exception as error:
            if not sync_task.done():
                sync_task.cancel()
                try:
                    await sync_task
                except asyncio.CancelledError:
                    pass
            if not sync_task.done():
                sync_task.cancel()
                try:
                    await sync_task
                except asyncio.CancelledError:
                    pass
            yield sse_event(
                "error",
                {
                    "step": "virtual-disks",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Virtual disks sync failed.",
                    "errors": [{"detail": str(error)}],
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{netbox_vm_id}/virtual-disks/create/stream", response_model=None)
async def create_virtual_disks_for_vm_stream(
    netbox_vm_id: int,
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
):
    """Sync virtual disks for a single NetBox VM identified by its primary key."""
    vm_record = await asyncio.to_thread(
        lambda: netbox_session.virtualization.virtual_machines.get(id=netbox_vm_id)
    )
    if vm_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Virtual machine id={netbox_vm_id} was not found in NetBox.",
        )

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await sync_virtual_disks(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    cluster_resources=cluster_resources,
                    tag=tag,
                    websocket=bridge,
                    use_websocket=True,
                    use_css=False,
                    netbox_vm_id=netbox_vm_id,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        try:
            yield sse_event(
                "step",
                {
                    "step": "virtual-disks",
                    "status": "started",
                    "message": f"Starting virtual disks sync for VM id={netbox_vm_id}.",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame

            result = await sync_task
            yield sse_event(
                "step",
                {
                    "step": "virtual-disks",
                    "status": "completed",
                    "message": "Virtual disks synchronization finished.",
                    "result": {
                        "count": result.get("count", 0),
                        "created": result.get("created", 0),
                        "updated": result.get("updated", 0),
                        "skipped": result.get("skipped", 0),
                    },
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Virtual disks sync completed.",
                    "result": result,
                },
            )
        except Exception as error:
            yield sse_event(
                "error",
                {
                    "step": "virtual-disks",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Virtual disks sync failed.",
                    "errors": [{"detail": str(error)}],
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
