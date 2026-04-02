"""Individual node/device sync route."""

from fastapi import APIRouter, Query

from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.services.sync.individual.device_sync import sync_node_individual
from proxbox_api.services.sync.individual.helpers import resolve_proxmox_session
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


@router.get("/node")
async def sync_node(
    nb: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    tag: ProxboxTagDep,
    cluster_name: str = Query(..., title="Cluster Name", description="Name of the cluster"),
    node_name: str = Query(..., title="Node Name", description="Name of the node to sync"),
    dry_run: bool = Query(
        default=False, title="Dry Run", description="If true, don't make changes"
    ),
):
    """Sync a single node (device) from Proxmox to NetBox."""
    px = resolve_proxmox_session(pxs, cluster_name)
    if px is None:
        return {"error": f"No Proxmox session found for cluster: {cluster_name}"}
    return await sync_node_individual(nb, px, tag, cluster_name, node_name, dry_run)
