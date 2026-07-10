"""Managed cloud customer-network helper routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.services.cloud_network import (
    peek_available_ips,
    resolve_cloud_network,
    validate_cloud_network_configured,
)
from proxbox_api.session.netbox import get_netbox_async_session

router = APIRouter()


class CloudNetworkAvailableIP(BaseModel):
    address: str


class CloudNetworkAvailableIPsResponse(BaseModel):
    prefix: int
    gateway: str
    bridge: str
    vlan_tag: int | None = None
    lock_enabled: bool
    available: list[CloudNetworkAvailableIP]


def _cloud_network_not_configured(error: ValueError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=str(error),
    )


@router.get("/network/available-ips", response_model=CloudNetworkAvailableIPsResponse)
async def cloud_network_available_ips(
    session: SessionDep,
    limit: int = Query(default=10, ge=1, le=256),
) -> CloudNetworkAvailableIPsResponse:
    """List available IPs from the configured customer prefix without occupying them."""
    cloud_network = resolve_cloud_network()
    try:
        validate_cloud_network_configured(cloud_network)
    except ValueError as error:
        raise _cloud_network_not_configured(error) from error
    if cloud_network.prefix_id is None:
        raise _cloud_network_not_configured(ValueError("cloud network not configured"))

    nb = await get_netbox_async_session(database_session=session)
    available = await peek_available_ips(
        cloud_network.prefix_id,
        limit,
        netbox_session=nb,
    )

    return CloudNetworkAvailableIPsResponse(
        prefix=cloud_network.prefix_id,
        gateway=cloud_network.gateway,
        bridge=cloud_network.bridge,
        vlan_tag=cloud_network.vlan_tag,
        lock_enabled=cloud_network.lock_enabled,
        available=[CloudNetworkAvailableIP(address=item.address) for item in available],
    )
