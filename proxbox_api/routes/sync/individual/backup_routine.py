"""Individual backup routines sync route."""

from fastapi import APIRouter, Query

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.services.sync.individual.backup_routine_sync import (
    sync_backup_routine_individual,
)
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


@router.api_route("/backup-routines", methods=["GET", "POST"])
async def sync_backup_routine(
    nb: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    cluster_name: str = Query(..., title="Cluster Name", description="Name of the cluster"),
    job_id: str = Query(..., title="Job ID", description="Backup job ID (e.g., 'backup:weekly')"),
    dry_run: bool = Query(default=False, title="Dry Run"),
):
    """Sync a single backup routine from Proxmox to NetBox."""
    px = resolve_proxmox_session(pxs, cluster_name)
    if px is None:
        return {"error": f"No Proxmox session found for cluster: {cluster_name}"}
    return await sync_backup_routine_individual(
        nb,
        px,
        tag,
        job_id,
        dry_run=dry_run,
    )
