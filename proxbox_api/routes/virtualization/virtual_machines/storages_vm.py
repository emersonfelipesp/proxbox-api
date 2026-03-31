"""Storage sync routes for Proxmox storage definitions."""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.services.sync.storages import create_storages as sync_storages
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_event

router = APIRouter()
_DEFAULT_FETCH_CONCURRENCY = max(1, int(os.getenv("PROXBOX_FETCH_MAX_CONCURRENCY", "8")))


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
        fetch_concurrency=fetch_max_concurrency or _DEFAULT_FETCH_CONCURRENCY,
    )


@router.get("/storage/create/stream", response_model=None)
async def create_storages_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
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
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        try:
            yield sse_event(
                "step",
                {
                    "step": "storage",
                    "status": "started",
                    "message": "Starting storage synchronization.",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame

            result = await sync_task
            yield sse_event(
                "step",
                {
                    "step": "storage",
                    "status": "completed",
                    "message": "Storage synchronization finished.",
                    "result": {"count": len(result)},
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Storage sync completed.",
                    "result": {"count": len(result)},
                },
            )
        except Exception as error:
            yield sse_event(
                "error",
                {
                    "step": "storage",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Storage sync failed.",
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
