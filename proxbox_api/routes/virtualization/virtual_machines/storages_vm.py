"""Storage sync routes for Proxmox storage definitions."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.runtime_settings import get_int
from proxbox_api.services.sync.storages import create_storages as sync_storages
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_stream_generator

router = APIRouter()


def _resolve_fetch_concurrency() -> int:
    return get_int(
        settings_key="proxbox_fetch_max_concurrency",
        env="PROXBOX_FETCH_MAX_CONCURRENCY",
        default=8,
        minimum=1,
    )


@router.get("/storage/create")
async def create_storages(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    websocket=None,
    use_websocket: bool = False,
    fetch_max_concurrency: int | None = None,
):
    """Sync Proxmox storages into NetBox plugin storage rows."""
    return await sync_storages(
        netbox_session=netbox_session,
        pxs=pxs,
        tag=tag,
        websocket=websocket,
        use_websocket=use_websocket,
        fetch_concurrency=fetch_max_concurrency or _resolve_fetch_concurrency(),
    )


@router.get("/storage/create/stream", response_model=None)
async def create_storages_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    fetch_max_concurrency: int | None = Query(
        default=None,
        title="Max Fetch Concurrency",
        description="Maximum number of concurrent Proxmox fetch operations.",
    ),
):
    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await sync_storages(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    tag=tag,
                    websocket=bridge,
                    use_websocket=True,
                    fetch_concurrency=fetch_max_concurrency or _resolve_fetch_concurrency(),
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        async for frame in sse_stream_generator(bridge, sync_task, "storage"):
            yield frame

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
