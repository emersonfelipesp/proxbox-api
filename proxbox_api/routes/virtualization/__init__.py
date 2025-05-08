# FastAPI Imports
from fastapi import APIRouter, Depends
from typing import Annotated

from proxbox_api.routes.proxmox.cluster import ClusterStatusDep

router = APIRouter()

@router.get('/cluster-types/create')
async def create_cluster_types():
    # TODO
    pass

@router.get('/clusters/create')
async def create_clusters(cluster_status: ClusterStatusDep):
    # TODO
    pass
    