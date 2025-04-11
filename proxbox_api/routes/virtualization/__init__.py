# FastAPI Imports
from fastapi import APIRouter, Depends
from typing import Annotated

from proxbox_api.routes.proxmox.cluster import ClusterStatusDep
from proxbox_api.routes.virtualization.virtual_machines import router as virtual_machines_router


router = APIRouter()
router.include_router(virtual_machines_router, prefix="/virtual-machines", tags=["virtualization / virtual-machines"])


@router.get('/cluster-types/create')
async def create_cluster_types():
    # TODO
    pass

@router.get('/clusters/create')
async def create_clusters(cluster_status: ClusterStatusDep):
    # TOOD
    pass
    