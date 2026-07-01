"""Proxmox SDN read-only endpoints and sync stream."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from proxmox_sdk.sdk.exceptions import ResourceException

from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.services.sync.sdn import (
    SdnControllerSchema,
    SdnFabricSchema,
    SdnNodeStatusSchema,
    SdnPrefixListSchema,
    SdnRouteMapSchema,
    SdnSubnetSchema,
    SdnVNetSchema,
    SdnZoneSchema,
    _to_fabric,
    _to_prefix_list,
    _to_route_map,
    collect_sdn_inventory,
    sync_sdn_to_netbox,
)
from proxbox_api.session.netbox import NetBoxSessionDep
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_stream_generator

router = APIRouter()


def _rows(raw: object) -> list[object]:
    if isinstance(raw, list):
        return raw
    if hasattr(raw, "root"):
        root = getattr(raw, "root")
        return root if isinstance(root, list) else [root]
    return [raw]


async def _collect_legacy_sdn_path(
    pxs: list[object],
    *,
    path: str,
    mapper,
    error_factory,
) -> list[object]:
    results: list[object] = []
    for px in pxs:
        cluster_name = getattr(px, "name", None)
        try:
            raw = await resolve_async(px.session(path).get())
            for row in _rows(raw):
                results.append(mapper(cluster_name, row))
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, ResourceException) and exc.status_code == 501:
                logger.warning(
                    "Cluster %s does not support /%s (PVE < 9.2) — skipping",
                    cluster_name,
                    path,
                )
                continue
            logger.exception(
                "Error fetching SDN path /%s for Proxmox cluster %s", path, cluster_name
            )
            results.append(error_factory(cluster_name, str(exc)))
    return results


@router.get("/sdn/controllers", response_model=list[SdnControllerSchema])
async def sdn_controllers(pxs: ProxmoxSessionsDep) -> list[SdnControllerSchema]:
    """List SDN controllers across configured Proxmox endpoints."""

    inventories = await collect_sdn_inventory(pxs, include_node_runtime=False)
    return [item for inventory in inventories for item in inventory.controllers]


@router.get("/sdn/zones", response_model=list[SdnZoneSchema])
async def sdn_zones(pxs: ProxmoxSessionsDep) -> list[SdnZoneSchema]:
    """List SDN zones across configured Proxmox endpoints."""

    inventories = await collect_sdn_inventory(pxs, include_node_runtime=False)
    return [item for inventory in inventories for item in inventory.zones]


@router.get("/sdn/vnets", response_model=list[SdnVNetSchema])
async def sdn_vnets(pxs: ProxmoxSessionsDep) -> list[SdnVNetSchema]:
    """List SDN VNets across configured Proxmox endpoints."""

    inventories = await collect_sdn_inventory(pxs, include_node_runtime=False)
    return [item for inventory in inventories for item in inventory.vnets]


@router.get("/sdn/subnets", response_model=list[SdnSubnetSchema])
async def sdn_subnets(pxs: ProxmoxSessionsDep) -> list[SdnSubnetSchema]:
    """List SDN VNet subnets across configured Proxmox endpoints."""

    inventories = await collect_sdn_inventory(pxs, include_node_runtime=False)
    return [item for inventory in inventories for item in inventory.subnets]


@router.get("/sdn/fabrics", response_model=list[SdnFabricSchema])
async def sdn_fabrics(pxs: ProxmoxSessionsDep) -> list[SdnFabricSchema]:
    """List SDN fabrics across configured Proxmox endpoints."""

    return await _collect_legacy_sdn_path(
        pxs,
        path="cluster/sdn/fabrics",
        mapper=_to_fabric,
        error_factory=lambda cluster, error: SdnFabricSchema(
            cluster_name=cluster,
            status="error",
            error=error,
        ),
    )


@router.get("/sdn/fabrics/all", response_model=list[SdnFabricSchema])
async def sdn_fabrics_all(pxs: ProxmoxSessionsDep) -> list[SdnFabricSchema]:
    """List all SDN fabrics across configured Proxmox endpoints."""

    return await _collect_legacy_sdn_path(
        pxs,
        path="cluster/sdn/fabrics/all",
        mapper=_to_fabric,
        error_factory=lambda cluster, error: SdnFabricSchema(
            cluster_name=cluster,
            status="error",
            error=error,
        ),
    )


@router.get("/sdn/route-maps", response_model=list[SdnRouteMapSchema])
async def sdn_route_maps(pxs: ProxmoxSessionsDep) -> list[SdnRouteMapSchema]:
    """List SDN route-map objects across configured Proxmox endpoints."""

    return await _collect_legacy_sdn_path(
        pxs,
        path="cluster/sdn/route-maps",
        mapper=_to_route_map,
        error_factory=lambda cluster, error: SdnRouteMapSchema(
            cluster_name=cluster,
            status="error",
            error=error,
        ),
    )


@router.get("/sdn/prefix-lists", response_model=list[SdnPrefixListSchema])
async def sdn_prefix_lists(pxs: ProxmoxSessionsDep) -> list[SdnPrefixListSchema]:
    """List SDN prefix-list objects across configured Proxmox endpoints."""

    return await _collect_legacy_sdn_path(
        pxs,
        path="cluster/sdn/prefix-lists",
        mapper=_to_prefix_list,
        error_factory=lambda cluster, error: SdnPrefixListSchema(
            cluster_name=cluster,
            status="error",
            error=error,
        ),
    )


@router.get("/sdn/node-status", response_model=list[SdnNodeStatusSchema])
async def sdn_node_status(pxs: ProxmoxSessionsDep) -> list[SdnNodeStatusSchema]:
    """List node-local SDN runtime content, bridges, MAC-VRF, and IP-VRF rows."""

    inventories = await collect_sdn_inventory(pxs, include_node_runtime=True)
    return [item for inventory in inventories for item in inventory.node_status]


@router.get("/sdn/create/stream", response_model=None)
async def create_sdn_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    sync_mode_sdn_bgp: Annotated[
        str,
        Query(
            description=(
                "Controls optional netbox-bgp projection inside the SDN stream. "
                "'disabled' skips it; 'always' and 'bootstrap_only' run it."
            ),
        ),
    ] = "disabled",
):
    """Stream read-only Proxmox SDN sync progress into NetBox."""

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await sync_sdn_to_netbox(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    websocket=bridge,
                    use_websocket=True,
                    sync_mode_sdn_bgp=sync_mode_sdn_bgp,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        async for frame in sse_stream_generator(
            bridge,
            sync_task,
            "sdn",
            result_extractor=lambda result: result if isinstance(result, dict) else {},
        ):
            yield frame

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
