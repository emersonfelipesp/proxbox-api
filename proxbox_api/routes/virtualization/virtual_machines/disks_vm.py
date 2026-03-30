"""Virtual disk sync routes."""

# FastAPI Imports
import asyncio

from fastapi import APIRouter
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
from proxbox_api.session.proxmox import ProxmoxSessionsDep  # Sessions
from proxbox_api.utils import (
    sync_process,
)  # Return Status HTML and Sync Process
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_event

router = APIRouter()


@router.get("/virtual-disks/create")
@sync_process("vm-disks")
async def create_virtual_disks(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    websocket=None,
    use_css: bool = False,
    use_websocket: bool = False,
    sync_process=None,
):
    """
    Syncs virtual disks for existing Virtual Machines in NetBox.

    Queries NetBox for VMs with cf_proxmox_vm_id set, fetches their disk
    configuration from Proxmox, and creates/updates Virtual Disk objects.
    """
    result = await sync_virtual_disks(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        tag=tag,
        websocket=websocket,
        use_websocket=use_websocket,
        use_css=use_css,
        sync_process=sync_process,
    )
    return result


@router.get("/virtual-disks/create/stream", response_model=None)
async def create_virtual_disks_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
):
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
                    sync_process=None,
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
                    "message": "Starting virtual disks synchronization.",
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
