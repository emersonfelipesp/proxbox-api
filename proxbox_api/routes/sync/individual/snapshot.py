"""Individual snapshot sync route."""

from typing import Literal

from fastapi import APIRouter, Query

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session
from proxbox_api.services.sync.individual.snapshot_sync import sync_snapshot_individual
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


@router.api_route("/snapshot", methods=["GET", "POST"])
async def sync_snapshot(
    nb: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    cluster_name: str = Query(..., title="Cluster Name", description="Name of the cluster"),
    node: str = Query(..., title="Node", description="Proxmox node name"),
    type: Literal["qemu", "lxc"] = Query(..., title="Type", description="VM type"),
    vmid: int = Query(..., title="VM ID", description="Proxmox VM ID"),
    snapshot_name: str = Query(..., title="Snapshot Name", description="Name of the snapshot"),
    auto_create_vm: bool = Query(default=True, title="Auto Create VM"),
    auto_create_storage: bool = Query(default=True, title="Auto Create Storage"),
    dry_run: bool = Query(default=False, title="Dry Run"),
):
    """Sync a single snapshot from Proxmox to NetBox."""
    px = resolve_proxmox_session(pxs, cluster_name)
    if px is None:
        return {"error": f"No Proxmox session found for cluster: {cluster_name}"}
    return await sync_snapshot_individual(
        nb,
        px,
        tag,
        node,
        type,
        vmid,
        snapshot_name,
        auto_create_vm=auto_create_vm,
        auto_create_storage=auto_create_storage,
        cluster_name=cluster_name,
        dry_run=dry_run,
    )
