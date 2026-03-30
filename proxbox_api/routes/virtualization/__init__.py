"""Virtualization route namespace (stubs reserved for future NetBox object bootstrapping)."""

# FastAPI Imports
from fastapi import APIRouter, HTTPException, status

from proxbox_api.routes.proxmox.cluster import ClusterStatusDep

router = APIRouter()

_NOT_IMPLEMENTED = (
    "Not implemented: use the NetBox UI or REST API to manage cluster types and clusters."
)


@router.get("/cluster-types/create")
async def create_cluster_types():
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_IMPLEMENTED)


@router.get("/clusters/create")
async def create_clusters(_cluster_status: ClusterStatusDep):
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_IMPLEMENTED)
