"""Read-only tiered ZFS storage inventory routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query

from proxbox_api.schemas.zfs import ZfsPoolDetailResponse, ZfsPoolsResponse
from proxbox_api.services.zfs import get_zfs_pool_detail, list_zfs_pools
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


@router.get("/storage/zfs/pools", response_model=ZfsPoolsResponse)
async def zfs_pools(
    pxs: ProxmoxSessionsDep,
    node: Annotated[
        str | None,
        Query(description="Optional Proxmox node name to scope the ZFS pool query."),
    ] = None,
) -> ZfsPoolsResponse:
    """List ZFS pool health and capacity across configured Proxmox clusters."""
    return await list_zfs_pools(pxs, node=node)


@router.get("/storage/zfs/pools/{name}", response_model=ZfsPoolDetailResponse)
async def zfs_pool_detail(
    pxs: ProxmoxSessionsDep,
    name: Annotated[str, Path(description="ZFS pool name.")],
    node: Annotated[
        str | None,
        Query(description="Optional Proxmox node name to scope the ZFS pool detail query."),
    ] = None,
) -> ZfsPoolDetailResponse:
    """Get ZFS pool detail, scrub status, errors, and vdev topology."""
    return await get_zfs_pool_detail(pxs, name=name, node=node)
