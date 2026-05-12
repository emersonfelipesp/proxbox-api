"""Individual cluster sync route."""

import uuid

from fastapi import APIRouter, Query

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.routes.proxbox import ProxboxConfigDep
from proxbox_api.services.run_session import SyncContext
from proxbox_api.services.sync.individual.cluster_sync import sync_cluster_individual
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils.structured_logging import set_operation_id

router = APIRouter()


@router.get("/cluster")
async def sync_cluster(
    nb: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    settings: ProxboxConfigDep,
    cluster_name: str = Query(..., title="Cluster Name", description="Name of the cluster to sync"),
    dry_run: bool = Query(
        default=False, title="Dry Run", description="If true, don't make changes"
    ),
):
    """Sync a single cluster from Proxmox to NetBox."""
    px = resolve_proxmox_session(pxs, cluster_name)
    if px is None:
        return {"error": f"No Proxmox session found for cluster: {cluster_name}"}

    operation_id = str(uuid.uuid4())
    set_operation_id(operation_id)
    ctx = SyncContext(
        nb=nb,
        px_sessions=list(pxs),
        tag=tag,
        settings=settings,
        operation_id=operation_id,
    )
    return await sync_cluster_individual(ctx, cluster_name, dry_run=dry_run)
