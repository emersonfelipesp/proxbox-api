"""Individual task history sync route."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session_for_request
from proxbox_api.services.sync.individual.task_history_sync import sync_task_history_individual
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


@router.get("/task-history")
async def sync_task_history(
    nb: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    cluster_name: Annotated[
        str | None,
        Query(
            title="Cluster Name",
            description="Optional cluster name; required when multiple Proxmox sessions are configured.",
        ),
    ] = None,
    node: str = Query(..., title="Node", description="Proxmox node name"),
    type: Literal["qemu", "lxc"] = Query(..., title="Type", description="VM type"),
    vmid: int = Query(..., title="VM ID", description="Proxmox VM ID"),
    upid: str | None = Query(None, title="Task UPID", description="Specific task UPID to sync"),
    dry_run: bool = Query(default=False, title="Dry Run"),
):
    """Sync a single task history record from Proxmox to NetBox."""
    px = resolve_proxmox_session_for_request(
        pxs,
        cluster_name,
        resource_name="task history",
    )
    selected_cluster_name = cluster_name or getattr(px, "name", "unknown")
    return await sync_task_history_individual(
        nb,
        px,
        tag,
        node,
        type,
        vmid,
        upid,
        cluster_name=selected_cluster_name,
        dry_run=dry_run,
    )
