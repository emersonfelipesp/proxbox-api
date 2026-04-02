"""Individual interface sync route."""

from typing import Literal

from fastapi import APIRouter, Query

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session
from proxbox_api.services.sync.individual.interface_sync import sync_interface_individual
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


@router.api_route("/interface", methods=["GET", "POST"])
async def sync_interface(
    nb: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    cluster_name: str = Query(..., title="Cluster Name", description="Name of the cluster"),
    node: str = Query(..., title="Node", description="Proxmox node name"),
    type: Literal["qemu", "lxc"] = Query(..., title="Type", description="VM type"),
    vmid: int = Query(..., title="VM ID", description="Proxmox VM ID"),
    interface_name: str = Query(
        ..., title="Interface Name", description="Interface name (e.g., net0)"
    ),
    dry_run: bool = Query(default=False, title="Dry Run"),
):
    """Sync a single interface from Proxmox to NetBox."""
    px = resolve_proxmox_session(pxs, cluster_name)
    if px is None:
        return {"error": f"No Proxmox session found for cluster: {cluster_name}"}
    return await sync_interface_individual(
        nb, px, tag, node, type, vmid, interface_name, dry_run=dry_run
    )
