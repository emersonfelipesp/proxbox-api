"""VM task history sync routes."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.netbox_rest import nested_tag_payload
from proxbox_api.routes.proxmox.cluster import ClusterStatusDep
from proxbox_api.services.sync.sync_state_writer import reset_sidecar_availability_cache
from proxbox_api.services.sync.task_history import sync_all_virtual_machine_task_histories
from proxbox_api.services.sync.vm_helpers import parse_selected_netbox_vm_ids
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_stream_generator

router = APIRouter()


@router.get(
    "/task-history/create/stream",
    response_model=None,
    dependencies=[Depends(reset_sidecar_availability_cache)],
)
async def create_all_virtual_machine_task_histories_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    tag: ProxboxTagDep,
    netbox_vm_ids: str | None = Query(
        default=None,
        title="NetBox VM IDs",
        description=(
            "Comma-separated positive NetBox VM IDs to sync. Empty or malformed "
            "values are rejected."
        ),
    ),
    fetch_max_concurrency: int | None = Query(
        default=None,
        ge=1,
        title="Max Fetch Concurrency",
        description="Maximum number of concurrent Proxmox fetch operations.",
    ),
):
    tag_refs = nested_tag_payload(tag) if tag else []
    try:
        selected_vm_ids = parse_selected_netbox_vm_ids(netbox_vm_ids)
    except ValueError as error:
        # Validate before constructing the StreamingResponse so clients receive
        # an ordinary HTTP 422 instead of an SSE stream that has already begun.
        raise HTTPException(status_code=422, detail=str(error)) from error

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await sync_all_virtual_machine_task_histories(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    tag_refs=tag_refs,
                    websocket=bridge,
                    use_websocket=True,
                    fetch_max_concurrency=fetch_max_concurrency,
                    netbox_vm_ids=selected_vm_ids,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        async for frame in sse_stream_generator(bridge, sync_task, "task-history"):
            yield frame

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
