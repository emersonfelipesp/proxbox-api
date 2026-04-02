"""Individual IP address sync route."""

from typing import Literal

from fastapi import APIRouter, Query

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.services.sync.individual.interface_sync import sync_interface_individual
from proxbox_api.services.sync.individual.ip_sync import sync_ip_individual
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


@router.get("/ip")
async def sync_ip(
    nb: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    node: str = Query(..., title="Node", description="Proxmox node name"),
    type: Literal["qemu", "lxc"] = Query(..., title="Type", description="VM type"),
    vmid: int = Query(..., title="VM ID", description="Proxmox VM ID"),
    ip_address: str = Query(
        ..., title="IP Address", description="IP address to sync (e.g., 192.168.1.1/24)"
    ),
    interface_name: str | None = Query(
        None, title="Interface Name", description="Optional interface name"
    ),
    dry_run: bool = Query(default=False, title="Dry Run"),
):
    """Sync a single IP address from Proxmox to NetBox."""
    from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session

    cluster_name = getattr(pxs[0], "name", "unknown") if pxs else "unknown"
    px = resolve_proxmox_session(pxs, cluster_name)
    if px is None:
        return {"error": f"No Proxmox session found for cluster: {cluster_name}"}
    return await sync_ip_individual(
        nb, px, tag, node, type, vmid, ip_address, interface_name, dry_run=dry_run
    )


@router.get("/interface")
async def sync_interface(
    nb: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    node: str = Query(..., title="Node", description="Proxmox node name"),
    type: Literal["qemu", "lxc"] = Query(..., title="Type", description="VM type"),
    vmid: int = Query(..., title="VM ID", description="Proxmox VM ID"),
    interface_name: str = Query(
        ..., title="Interface Name", description="Interface name (e.g., net0)"
    ),
    dry_run: bool = Query(default=False, title="Dry Run"),
):
    """Sync a single interface from Proxmox to NetBox."""
    from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session

    cluster_name = getattr(pxs[0], "name", "unknown") if pxs else "unknown"
    px = resolve_proxmox_session(pxs, cluster_name)
    if px is None:
        return {"error": f"No Proxmox session found for cluster: {cluster_name}"}
    return await sync_interface_individual(
        nb, px, tag, node, type, vmid, interface_name, dry_run=dry_run
    )
