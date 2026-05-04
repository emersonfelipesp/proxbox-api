"""Individual VM sync routes."""

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session
from proxbox_api.services.sync.individual.vm_sync import sync_vm_individual
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


@router.get("/vm")
async def sync_vm(
    nb: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    cluster_name: str = Query(..., title="Cluster Name", description="Name of the cluster"),
    node: str = Query(..., title="Node", description="Proxmox node name"),
    type: Literal["qemu", "lxc"] = Query(..., title="Type", description="VM type"),
    vmid: int = Query(..., title="VM ID", description="Proxmox VM ID"),
    dry_run: bool = Query(
        default=False, title="Dry Run", description="If true, don't make changes"
    ),
    overwrite_flags: Annotated[SyncOverwriteFlags, Query()] = SyncOverwriteFlags(),
):
    """Sync a single virtual machine from Proxmox to NetBox."""
    px = resolve_proxmox_session(pxs, cluster_name)
    if px is None:
        return {"error": f"No Proxmox session found for cluster: {cluster_name}"}
    return await sync_vm_individual(
        nb, px, tag, cluster_name, node, type, vmid, dry_run, overwrite_flags=overwrite_flags
    )


@router.get("/vm/{cluster_name}/{node}/{type}/{vmid}")
async def sync_vm_by_path(
    nb: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    cluster_name: str,
    node: str,
    type: Literal["qemu", "lxc"],
    vmid: int,
    dry_run: bool = Query(default=False),
    overwrite_flags: Annotated[SyncOverwriteFlags, Query()] = SyncOverwriteFlags(),
):
    """Sync a single VM using path parameters."""
    px = resolve_proxmox_session(pxs, cluster_name)
    if px is None:
        return {"error": f"No Proxmox session found for cluster: {cluster_name}"}
    return await sync_vm_individual(
        nb, px, tag, cluster_name, node, type, vmid, dry_run, overwrite_flags=overwrite_flags
    )
