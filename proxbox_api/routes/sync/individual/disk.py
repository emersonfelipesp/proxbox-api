"""Individual disk sync route."""

from typing import Literal

from fastapi import APIRouter, Query

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session
from proxbox_api.services.sync.individual.virtual_disk_sync import sync_virtual_disk_individual
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


@router.get("/disk")
async def sync_disk(
    nb: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    node: str = Query(..., title="Node", description="Proxmox node name"),
    type: Literal["qemu", "lxc"] = Query(..., title="Type", description="VM type"),
    vmid: int = Query(..., title="VM ID", description="Proxmox VM ID"),
    disk_name: str = Query(
        ..., title="Disk Name", description="Disk name (e.g., scsi0, virtio0, rootfs)"
    ),
    dry_run: bool = Query(default=False, title="Dry Run"),
):
    """Sync a single virtual disk from Proxmox to NetBox."""
    cluster_name = getattr(pxs[0], "name", "unknown") if pxs else "unknown"
    px = resolve_proxmox_session(pxs, cluster_name)
    if px is None:
        return {"error": f"No Proxmox session found for cluster: {cluster_name}"}
    return await sync_virtual_disk_individual(
        nb, px, tag, node, type, vmid, disk_name, dry_run=dry_run
    )
