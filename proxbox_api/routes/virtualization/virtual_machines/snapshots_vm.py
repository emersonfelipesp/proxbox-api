"""VM snapshot sync routes."""

# FastAPI Imports
import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import (
    NetBoxSessionDep,  # NetBox Session
    ProxboxTagDep,  # Proxbox Tag
)
from proxbox_api.exception import ProxboxException  # Proxbox Exception

# NetBox compatibility wrappers
from proxbox_api.routes.proxmox.cluster import (
    ClusterResourcesDep,
    ClusterStatusDep,
)  # Cluster Status and Resources
from proxbox_api.services.sync.snapshots import (
    create_virtual_machine_snapshots as sync_snapshots,
)
from proxbox_api.services.sync.vm_helpers import parse_selected_netbox_vm_ids
from proxbox_api.session.proxmox import ProxmoxSessionsDep  # Sessions
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_stream_generator

router = APIRouter()


async def _create_all_virtual_machine_snapshots(
    netbox_session,
    pxs,
    cluster_status,
    cluster_resources,
    tag,
    fetch_max_concurrency: int | None = None,
    websocket=None,
    use_websocket=False,
    vmid_filter: int | list[int] | None = None,
    netbox_vm_ids: list[int] | None = None,
    delete_nonexistent_snapshot: bool = False,
):
    """Internal function that handles snapshot sync with optional websocket support.

    When ``vmid_filter`` is provided only snapshots for that Proxmox VMID are synced.
    """
    nb = netbox_session
    created_count = 0

    try:
        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "snapshots",
                    "status": "started",
                    "message": "Starting snapshot synchronization.",
                }
            )

        result = await sync_snapshots(
            netbox_session=nb,
            pxs=pxs,
            cluster_status=cluster_status,
            cluster_resources=cluster_resources,
            tag=tag,
            websocket=websocket,
            use_websocket=use_websocket,
            use_css=False,
            fetch_max_concurrency=fetch_max_concurrency,
            vmid=vmid_filter,
            netbox_vm_ids=netbox_vm_ids,
            delete_nonexistent_snapshot=delete_nonexistent_snapshot,
        )

        if result:
            created_count = result.get("created", 0)

        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "snapshots",
                    "status": "completed",
                    "message": f"Snapshot synchronization finished. Created/updated: {created_count}",
                    "count": created_count,
                }
            )

        return result

    except ProxboxException as error:
        error_msg = f"Error during snapshot sync: {str(error)}"
        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "snapshots",
                    "status": "failed",
                    "message": error_msg,
                }
            )
        raise
    except Exception as error:
        error_msg = f"Error during snapshot sync: {str(error)}"
        if use_websocket and websocket:
            await websocket.send_json(
                {
                    "step": "snapshots",
                    "status": "failed",
                    "message": error_msg,
                }
            )
        raise ProxboxException(message=error_msg)


@router.get("/snapshots/create")
async def create_virtual_machine_snapshots(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    vmid: Annotated[
        int | None,
        Query(title="VM ID", description="The ID of the VM to retrieve snapshots for."),
    ] = None,
    node: Annotated[
        str | None,
        Query(title="Node", description="The name of the node."),
    ] = None,
    fetch_max_concurrency: Annotated[
        int | None,
        Query(
            title="Fetch max concurrency",
            description="Maximum parallel Proxmox fetch operations for snapshot discovery.",
            ge=1,
        ),
    ] = None,
):
    return await sync_snapshots(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        tag=tag,
        vmid=vmid,
        node=node,
        fetch_max_concurrency=fetch_max_concurrency,
    )


@router.get("/snapshots/all/create")
async def create_all_virtual_machine_snapshots(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    fetch_max_concurrency: Annotated[
        int | None,
        Query(
            title="Fetch max concurrency",
            description="Maximum parallel Proxmox fetch operations for snapshot discovery.",
            ge=1,
        ),
    ] = None,
    netbox_vm_ids: str | None = Query(
        default=None,
        title="NetBox VM IDs",
        description="Comma-separated list of NetBox VM IDs to sync. When provided, only these VMs will be synced.",
    ),
    delete_nonexistent_snapshot: Annotated[
        bool,
        Query(
            title="Delete Nonexistent Snapshot",
            description="If true, deletes snapshots that exist in NetBox but not in Proxmox.",
        ),
    ] = False,
):
    try:
        vm_ids = parse_selected_netbox_vm_ids(netbox_vm_ids)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return await _create_all_virtual_machine_snapshots(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        tag=tag,
        fetch_max_concurrency=fetch_max_concurrency,
        netbox_vm_ids=vm_ids,
        delete_nonexistent_snapshot=delete_nonexistent_snapshot,
    )


@router.get("/snapshots/all/create/stream", response_model=None)
async def create_all_virtual_machine_snapshots_stream(  # noqa: C901
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    fetch_max_concurrency: Annotated[
        int | None,
        Query(
            title="Fetch max concurrency",
            description="Maximum parallel Proxmox fetch operations for snapshot discovery.",
            ge=1,
        ),
    ] = None,
    netbox_vm_ids: str | None = Query(
        default=None,
        title="NetBox VM IDs",
        description="Comma-separated list of NetBox VM IDs to sync. When provided, only these VMs will be synced.",
    ),
    delete_nonexistent_snapshot: Annotated[
        bool,
        Query(
            title="Delete Nonexistent Snapshot",
            description="If true, deletes snapshots that exist in NetBox but not in Proxmox.",
        ),
    ] = False,
):
    try:
        vm_ids = parse_selected_netbox_vm_ids(netbox_vm_ids)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await _create_all_virtual_machine_snapshots(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    cluster_resources=cluster_resources,
                    tag=tag,
                    fetch_max_concurrency=fetch_max_concurrency,
                    netbox_vm_ids=vm_ids,
                    websocket=bridge,
                    use_websocket=True,
                    delete_nonexistent_snapshot=delete_nonexistent_snapshot,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        async for frame in sse_stream_generator(bridge, sync_task, "snapshots"):
            yield frame

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{netbox_vm_id}/snapshots/create/stream", response_model=None)
async def create_virtual_machine_snapshots_by_id_stream(
    netbox_vm_id: int,
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    tag: ProxboxTagDep,
    fetch_max_concurrency: Annotated[
        int | None,
        Query(
            title="Fetch max concurrency",
            description="Maximum parallel Proxmox fetch operations for snapshot discovery.",
            ge=1,
        ),
    ] = None,
):
    """Sync snapshots for a single NetBox VM identified by its primary key."""
    vm_record = await netbox_session.virtualization.virtual_machines.get(id=netbox_vm_id)
    if vm_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Virtual machine id={netbox_vm_id} was not found in NetBox.",
        )

    vm_data = (
        vm_record
        if isinstance(vm_record, dict)
        else (vm_record.serialize() if hasattr(vm_record, "serialize") else dict(vm_record))
    )
    cf = vm_data.get("custom_fields") or {}
    raw_vmid = cf.get("proxmox_vm_id")
    proxmox_vmid: int | None = None
    if raw_vmid is not None:
        try:
            proxmox_vmid = int(str(raw_vmid).strip())
        except (TypeError, ValueError):
            pass

    if proxmox_vmid is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Virtual machine id={netbox_vm_id} has no proxmox_vm_id custom field set; "
                "cannot filter snapshots."
            ),
        )

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await _create_all_virtual_machine_snapshots(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    cluster_resources=cluster_resources,
                    tag=tag,
                    fetch_max_concurrency=fetch_max_concurrency,
                    websocket=bridge,
                    use_websocket=True,
                    netbox_vm_ids=[netbox_vm_id],
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        async for frame in sse_stream_generator(
            bridge,
            sync_task,
            "snapshots",
            started_message=f"Starting snapshot sync for VM id={netbox_vm_id}.",
        ):
            yield frame

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
