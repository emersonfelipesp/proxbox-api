"""Proxmox cluster replication endpoints and response schemas."""

import asyncio

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from proxbox_api.dependencies import NetBoxSessionDep
from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.services.sync.replications import sync_all_replications
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils.streaming import sse_event

router = APIRouter()


class ReplicationJobSchema(BaseModel):
    cluster_name: str | None = None
    status: str = "ok"
    error: str | None = None
    comment: str | None = None
    disable: bool | None = None
    guest: int | None = None
    id: str | None = None
    jobnum: int | None = None
    rate: float | None = None
    remove_job: str | None = None
    schedule: str | None = None
    source: str | None = None
    target: str | None = None
    type: str | None = None


ReplicationJobSchemaList = list[ReplicationJobSchema]


@router.get("/replication", response_model=ReplicationJobSchemaList)
async def cluster_replication(pxs: ProxmoxSessionsDep):
    """
    ### Retrieve the replication jobs from multiple Proxmox sessions.

    **Args:**
    - **pxs (`ProxmoxSessionsDep`):** A list of Proxmox session dependencies.

    **Returns:**
    - **list:** A list of dictionaries containing the replication jobs from each cluster.

    ### Example Response:
    ```json
    [
        {
            "comment": "My replication job",
            "disable": false,
            "guest": 100,
            "id": "100-1",
            "jobnum": 1,
            "rate": 10.5,
            "remove_job": null,
            "schedule": "*/15",
            "source": "proxmox-node-1",
            "target": "proxmox-node-2",
            "type": "local"
        }
    ]
    ```
    """
    results = []

    for px in pxs:
        try:
            replications = await resolve_async(px.session.cluster.replication.get())
        except Exception as error:
            logger.exception("Error fetching replication jobs for Proxmox cluster %s", px.name)
            results.append(
                ReplicationJobSchema(
                    cluster_name=px.name,
                    status="error",
                    error=str(error),
                )
            )
            continue

        try:
            for rep in replications:
                results.append(
                    ReplicationJobSchema(
                        cluster_name=px.name,
                        status="ok",
                        comment=rep.get("comment"),
                        disable=rep.get("disable"),
                        guest=rep.get("guest"),
                        id=rep.get("id"),
                        jobnum=rep.get("jobnum"),
                        rate=rep.get("rate"),
                        remove_job=rep.get("remove_job"),
                        schedule=rep.get("schedule"),
                        source=rep.get("source"),
                        target=rep.get("target"),
                        type=rep.get("type"),
                    )
                )
        except Exception as error:
            logger.exception("Unexpected replication payload for Proxmox cluster %s", px.name)
            results.append(
                ReplicationJobSchema(
                    cluster_name=px.name,
                    status="error",
                    error=str(error),
                )
            )

    return results


@router.get("/replication/stream", response_model=None)
async def cluster_replication_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
):
    """Stream replication sync progress and terminal status via SSE."""

    async def event_stream():
        try:
            yield sse_event(
                "step",
                {
                    "step": "replications",
                    "status": "started",
                    "message": "Starting replications synchronization.",
                },
            )
            result = await sync_all_replications(netbox_session=netbox_session, pxs=pxs)
            yield sse_event(
                "step",
                {
                    "step": "replications",
                    "status": "completed",
                    "message": "Replications synchronization finished.",
                    "result": result,
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Replications sync completed.",
                    "result": result,
                },
            )
        except asyncio.CancelledError:
            yield sse_event(
                "error",
                {
                    "step": "replications",
                    "status": "failed",
                    "error": "Server shutdown or request cancelled.",
                    "detail": "Server shutdown or request cancelled.",
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Replications sync cancelled.",
                    "errors": [{"detail": "Server shutdown or request cancelled."}],
                },
            )
        except Exception as error:  # noqa: BLE001
            yield sse_event(
                "error",
                {
                    "step": "replications",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Replications sync failed.",
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
