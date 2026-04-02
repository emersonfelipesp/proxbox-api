"""Individual snapshot sync route."""

from typing import Literal

from fastapi import APIRouter, Query

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session
from proxbox_api.services.sync.individual.snapshot_sync import sync_snapshot_individual
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


@router.get("/snapshot")
async def sync_snapshot(
    nb: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    node: str = Query(..., title="Node", description="Proxmox node name"),
    type: Literal["qemu", "lxc"] = Query(..., title="Type", description="VM type"),
    vmid: int = Query(..., title="VM ID", description="Proxmox VM ID"),
    snapshot_name: str = Query(..., title="Snapshot Name", description="Name of the snapshot"),
    dry_run: bool = Query(default=False, title="Dry Run"),
):
    """Sync a single snapshot from Proxmox to NetBox."""
    cluster_name = getattr(pxs[0], "name", "unknown") if pxs else "unknown"
    px = resolve_proxmox_session(pxs, cluster_name)
    if px is None:
        return {"error": f"No Proxmox session found for cluster: {cluster_name}"}
    return await sync_snapshot_individual(
        nb, px, tag, node, type, vmid, snapshot_name, dry_run=dry_run
    )
