"""Individual replication sync route."""

from fastapi import APIRouter, Query

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session
from proxbox_api.services.sync.individual.replication_sync import sync_replication_individual
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


@router.api_route("/replication", methods=["GET", "POST"])
async def sync_replication(
    nb: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    cluster_name: str = Query(..., title="Cluster Name", description="Name of the cluster"),
    replication_id: str = Query(
        ..., title="Replication ID", description="Replication job ID (e.g., '100-1')"
    ),
    auto_create_vm: bool = Query(default=True, title="Auto Create VM"),
    dry_run: bool = Query(default=False, title="Dry Run"),
):
    """Sync a single replication from Proxmox to NetBox."""
    px = resolve_proxmox_session(pxs, cluster_name)
    if px is None:
        return {"error": f"No Proxmox session found for cluster: {cluster_name}"}
    return await sync_replication_individual(
        nb,
        px,
        tag,
        replication_id,
        auto_create_vm=auto_create_vm,
        dry_run=dry_run,
    )
