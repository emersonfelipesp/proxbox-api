"""Proxmox cluster endpoints and cluster response schemas."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from proxbox_api.enum.proxmox import *
from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.schemas.proxmox import *
from proxbox_api.services.proxmox_helpers import (
    get_cluster_resources as get_typed_cluster_resources,
)
from proxbox_api.services.proxmox_helpers import (
    get_cluster_status as get_typed_cluster_status,
)
from proxbox_api.services.sync.backup_routines import sync_all_backup_routines
from proxbox_api.session.netbox import NetBoxSessionDep
from proxbox_api.session.proxmox import ProxmoxSession, ProxmoxSessionsDep
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_stream_generator

router = APIRouter()


class BaseClusterStatusSchema(BaseModel):
    id: str
    name: str
    type: str


class ClusterNodeStatusSchema(BaseClusterStatusSchema):
    ip: str
    level: str | None = None
    local: bool
    nodeid: int
    online: bool


class ClusterStatusSchema(BaseClusterStatusSchema):
    nodes: int
    quorate: bool
    version: int
    mode: str
    node_list: list[ClusterNodeStatusSchema] | None = None


ClusterStatusSchemaList = list[ClusterStatusSchema]


def _cluster_item_defaults(
    item_data: dict[str, object],
    *,
    cluster_name: str,
    node_count: int,
    mode: str,
) -> dict[str, object]:
    """Normalize sparse Proxmox cluster payloads to the route schema."""
    name = str(item_data.get("name") or cluster_name)
    quorate = item_data.get("quorate")
    return {
        "id": item_data.get("id") or f"cluster/{name}",
        "name": name,
        "type": item_data.get("type") or "cluster",
        "nodes": int(item_data.get("nodes") or node_count),
        "quorate": bool(quorate) if quorate is not None else bool(node_count > 0),
        "version": int(item_data.get("version") or 0),
        "mode": mode,
    }


def _node_item_defaults(item_data: dict[str, object]) -> dict[str, object]:
    """Normalize sparse Proxmox node payloads to the route schema."""
    name = str(item_data.get("name") or "")
    return {
        "id": item_data.get("id") or f"node/{name}",
        "name": name,
        "type": item_data.get("type") or "node",
        "ip": item_data.get("ip") or "",
        "level": item_data.get("level"),
        "local": bool(item_data.get("local") or False),
        "nodeid": int(item_data.get("nodeid") or 0),
        "online": bool(item_data.get("online") or False),
    }


# /proxmox/cluster/ API Endpoints
@router.get("/status", response_model=ClusterStatusSchemaList)
async def cluster_status(pxs: ProxmoxSessionsDep) -> ClusterStatusSchemaList:
    """
    ### Retrieve the status of clusters from multiple Proxmox sessions.

    **Args:**
    - **pxs (`ProxmoxSessionsDep`):** A list of Proxmox session dependencies.

    **Returns:**
    - **list (`ClusterStatusSchemaList`):** A list of dictionaries containing the status of each cluster.

    ### Example Response:
    ```json
    [
        'id': 'cluster',
        'name': 'Cluster-Name',
        'type': 'cluster',
        'mode': 'standalone',
        'nodes:' 2,
        'quorate': 1,
        'version': 1,
        'node_list': [
            {
                'id': 'node/node-name',
                'name': 'node-name',
                'type': 'node',
                'ip': '10.0.0.1',
                'level: '',
                'local': 1,
                'nodeid': 1,
                'online': 1
            },
            {
                'id': 'node/node-name2',
                'name': 'node-name2',
                'type': 'node',
                'ip': '10.0.0.2',
                'level: '',
                'local': 1,
                'nodeid': 1,
                'online': 1
            }
        ]
    ]
    ```
    """

    async def parse_cluster_status(
        proxmox_object: ProxmoxSession, data: list
    ) -> ClusterStatusSchema:
        node_list: list[ClusterNodeStatusSchema] = []
        cluster_item: dict[str, object] | None = None

        for item in data:
            item_data = item.model_dump(mode="python", by_alias=True, exclude_none=True)
            item_data["mode"] = proxmox_object.mode

            if item_data.get("type") == "node":
                node_list.append(ClusterNodeStatusSchema(**_node_item_defaults(item_data)))
                continue

            if item_data.get("type") == "cluster":
                cluster_item = item_data

        cluster_payload = _cluster_item_defaults(
            cluster_item or {},
            cluster_name=proxmox_object.name,
            node_count=len(node_list),
            mode=proxmox_object.mode,
        )
        cluster = ClusterStatusSchema(**cluster_payload)
        cluster.node_list = node_list
        return cluster

    return ClusterStatusSchemaList(
        [
            await parse_cluster_status(
                proxmox_object=px,
                data=await get_typed_cluster_status(px),
            )
            for px in pxs
        ]
    )


ClusterStatusDep = Annotated[ClusterStatusSchemaList, Depends(cluster_status)]

# /proxmox/cluster/ API Endpoints


@router.get("/resources", response_model=ClusterResourcesList)
async def cluster_resources(
    pxs: ProxmoxSessionsDep,
    type: Annotated[
        ClusterResourcesType,
        Query(
            title="Proxmox Resource Type",
            description="Type of Proxmox resource to return (ex. 'vm' return QEMU Virtual Machines).",
        ),
    ] = None,
):
    """
    ### Fetches Proxmox cluster resources.

    This asynchronous function retrieves resources from a Proxmox cluster. It supports filtering by resource type.

    **Args:**
    - **pxs (`ProxmoxSessionsDep`):** Dependency injection for Proxmox sessions.
    - **type (`Annotated[ClusterResourcesType, Query]`):** Optional. The type of Proxmox resource to return. If not provided, all resources are returned.

    **Returns:**
    - **list:** A list of dictionaries containing the Proxmox cluster resources.
    """

    resource_type = type if isinstance(type, str) else None

    # Deduplicate resources across sessions that belong to the same Proxmox cluster.
    # When multiple Proxmox endpoints are nodes of the same cluster, each one returns
    # the full cluster resource list.  Track seen resource IDs globally so that the
    # same VM/LXC is never submitted to NetBox twice.
    cluster_resource_map: dict[str, list[dict]] = {}
    seen_resource_ids: set[str] = set()

    for px in pxs:
        resources = await get_typed_cluster_resources(px, resource_type=resource_type)
        cluster_name = px.name
        if cluster_name not in cluster_resource_map:
            cluster_resource_map[cluster_name] = []
        for resource in resources:
            resource_id: str = resource.id
            if resource_id in seen_resource_ids:
                continue
            seen_resource_ids.add(resource_id)
            cluster_resource_map[cluster_name].append(
                resource.model_dump(mode="python", by_alias=True, exclude_none=True)
            )

    return [{name: items} for name, items in cluster_resource_map.items()]


ClusterResourcesDep = Annotated[ClusterResourcesList, Depends(cluster_resources)]


# Backup routines endpoint
@router.get("/backup", response_model=list[dict[str, object]])
async def cluster_backup(pxs: ProxmoxSessionsDep):
    """
    ### Retrieve backup job configurations from multiple Proxmox sessions.

    **Args:**
    - **pxs (`ProxmoxSessionsDep`):** A list of Proxmox session dependencies.

    **Returns:**
    - **list:** A list of backup job configurations from each cluster.
    """
    results = []

    for px in pxs:
        try:
            backup_jobs = await resolve_async(px.session.cluster.backup.get())
            for job in backup_jobs:
                job["cluster_name"] = px.name
                results.append(job)
        except Exception as error:
            logger.exception("Error fetching backup jobs for Proxmox cluster %s", px.name)
            results.append({"cluster_name": px.name, "error": str(error)})

    return results


@router.get("/backup/stream", response_model=None)
async def cluster_backup_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
):
    """Stream backup-routines sync progress and terminal status via SSE."""

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await sync_all_backup_routines(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    bridge=bridge,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        async for frame in sse_stream_generator(bridge, sync_task, "backup-routines"):
            yield frame

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
