"""Individual storage sync route."""

from fastapi import APIRouter, Query

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session
from proxbox_api.services.sync.individual.storage_sync import sync_storage_individual
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


@router.get("/storage")
async def sync_storage(
    nb: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    cluster_name: str = Query(..., title="Cluster Name", description="Name of the cluster"),
    storage_name: str = Query(..., title="Storage Name", description="Name of the storage"),
    dry_run: bool = Query(default=False, title="Dry Run"),
):
    """Sync a single storage from Proxmox to NetBox."""
    px = resolve_proxmox_session(pxs, cluster_name)
    if px is None:
        return {"error": f"No Proxmox session found for cluster: {cluster_name}"}
    return await sync_storage_individual(nb, px, tag, cluster_name, storage_name, dry_run)
