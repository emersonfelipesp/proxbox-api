"""Individual backup sync route."""

from fastapi import APIRouter, Query

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.services.sync.individual.backup_sync import sync_backup_individual
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


@router.api_route("/backup", methods=["GET", "POST"])
async def sync_backup(
    nb: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    cluster_name: str = Query(..., title="Cluster Name", description="Name of the cluster"),
    node: str = Query(..., title="Node", description="Proxmox node name"),
    storage: str = Query(..., title="Storage", description="Proxmox storage name"),
    vmid: int = Query(..., title="VM ID", description="Proxmox VM ID"),
    volid: str = Query(..., title="Volume ID", description="Backup volume ID"),
    auto_create_vm: bool = Query(default=True, title="Auto Create VM"),
    auto_create_storage: bool = Query(default=True, title="Auto Create Storage"),
    dry_run: bool = Query(default=False, title="Dry Run"),
):
    """Sync a single backup from Proxmox to NetBox."""
    px = resolve_proxmox_session(pxs, cluster_name)
    if px is None:
        return {"error": f"No Proxmox session found for cluster: {cluster_name}"}
    return await sync_backup_individual(
        nb,
        px,
        tag,
        node,
        storage,
        vmid,
        volid,
        auto_create_vm=auto_create_vm,
        auto_create_storage=auto_create_storage,
        dry_run=dry_run,
    )
