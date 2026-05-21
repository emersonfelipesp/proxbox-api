"""Proxmox SDN (Software Defined Networking) endpoints (read-only).

Surfaces PVE 9.2 SDN fabric, route-map, and prefix-list objects
so operators can inspect WireGuard/BGP fabrics and their routing policy
objects without leaving the Proxbox API surface.

All endpoints are read-only.  SDN mutations require cluster-level
access and remain intentionally out of scope for this proxy layer.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


class SdnFabricSchema(BaseModel):
    """A single SDN fabric object."""

    cluster_name: str | None = None
    fabric: str | None = None
    type: str | None = None
    advertise_subnets: bool | None = None
    disable_arp_nd_suppression: bool | None = None
    vrf_vxlan: int | None = None
    peers: str | None = None
    asn: int | None = None
    status: str = "ok"
    error: str | None = None


class SdnRouteMapSchema(BaseModel):
    """A single SDN route-map entry."""

    cluster_name: str | None = None
    name: str | None = None
    action: str | None = None
    match_peer: str | None = None
    match_ip: str | None = None
    set_community: str | None = None
    order: int | None = None
    status: str = "ok"
    error: str | None = None


class SdnPrefixListSchema(BaseModel):
    """A single SDN prefix-list entry."""

    cluster_name: str | None = None
    name: str | None = None
    cidr: str | None = None
    action: str | None = None
    le: int | None = None
    ge: int | None = None
    status: str = "ok"
    error: str | None = None


def _to_fabric(cluster_name: str, raw: object) -> SdnFabricSchema:
    data: dict[str, object] = {}
    if hasattr(raw, "model_dump"):
        data = raw.model_dump(mode="python", by_alias=True, exclude_none=True)
    elif isinstance(raw, dict):
        data = dict(raw)
    return SdnFabricSchema(
        cluster_name=cluster_name,
        fabric=str(data.get("fabric")) if data.get("fabric") is not None else None,
        type=str(data.get("type")) if data.get("type") is not None else None,
        advertise_subnets=bool(data.get("advertise_subnets"))
        if "advertise_subnets" in data
        else None,
        disable_arp_nd_suppression=bool(data.get("disable_arp_nd_suppression"))
        if "disable_arp_nd_suppression" in data
        else None,
        vrf_vxlan=int(data["vrf_vxlan"]) if isinstance(data.get("vrf_vxlan"), (int, str)) else None,
        asn=int(data["asn"]) if isinstance(data.get("asn"), (int, str)) else None,
        peers=str(data.get("peers")) if data.get("peers") is not None else None,
    )


def _to_route_map(cluster_name: str, raw: object) -> SdnRouteMapSchema:
    data: dict[str, object] = {}
    if hasattr(raw, "model_dump"):
        data = raw.model_dump(mode="python", by_alias=True, exclude_none=True)
    elif isinstance(raw, dict):
        data = dict(raw)
    return SdnRouteMapSchema(
        cluster_name=cluster_name,
        name=str(data.get("name")) if data.get("name") is not None else None,
        action=str(data.get("action")) if data.get("action") is not None else None,
        match_peer=str(data.get("match-peer") or data.get("match_peer"))
        if (data.get("match-peer") or data.get("match_peer")) is not None
        else None,
        match_ip=str(data.get("match-ip") or data.get("match_ip"))
        if (data.get("match-ip") or data.get("match_ip")) is not None
        else None,
        set_community=str(data.get("set-community") or data.get("set_community"))
        if (data.get("set-community") or data.get("set_community")) is not None
        else None,
        order=int(data["order"]) if isinstance(data.get("order"), (int, str)) else None,
    )


def _to_prefix_list(cluster_name: str, raw: object) -> SdnPrefixListSchema:
    data: dict[str, object] = {}
    if hasattr(raw, "model_dump"):
        data = raw.model_dump(mode="python", by_alias=True, exclude_none=True)
    elif isinstance(raw, dict):
        data = dict(raw)
    return SdnPrefixListSchema(
        cluster_name=cluster_name,
        name=str(data.get("name")) if data.get("name") is not None else None,
        cidr=str(data.get("cidr")) if data.get("cidr") is not None else None,
        action=str(data.get("action")) if data.get("action") is not None else None,
        le=int(data["le"]) if isinstance(data.get("le"), (int, str)) else None,
        ge=int(data["ge"]) if isinstance(data.get("ge"), (int, str)) else None,
    )


@router.get("/sdn/fabrics", response_model=list[SdnFabricSchema])
async def sdn_fabrics(pxs: ProxmoxSessionsDep) -> list[SdnFabricSchema]:
    """List SDN fabrics across all configured Proxmox clusters (PVE 9.2+).

    Proxies ``GET /cluster/sdn/fabrics``.  Returns WireGuard and BGP
    fabric objects (and any pre-existing VXLAN/OSPF fabrics).
    """
    results: list[SdnFabricSchema] = []
    for px in pxs:
        try:
            raw = await resolve_async(px.session("cluster/sdn/fabrics").get())
            rows = raw if isinstance(raw, list) else (raw.root if hasattr(raw, "root") else [raw])
            for row in rows:
                results.append(_to_fabric(px.name, row))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error fetching SDN fabrics for Proxmox cluster %s", px.name)
            results.append(SdnFabricSchema(cluster_name=px.name, status="error", error=str(exc)))
    return results


@router.get("/sdn/fabrics/all", response_model=list[SdnFabricSchema])
async def sdn_fabrics_all(pxs: ProxmoxSessionsDep) -> list[SdnFabricSchema]:
    """List all SDN fabrics (including inherited) across all clusters (PVE 9.2+).

    Proxies ``GET /cluster/sdn/fabrics/all``.
    """
    results: list[SdnFabricSchema] = []
    for px in pxs:
        try:
            raw = await resolve_async(px.session("cluster/sdn/fabrics/all").get())
            rows = raw if isinstance(raw, list) else (raw.root if hasattr(raw, "root") else [raw])
            for row in rows:
                results.append(_to_fabric(px.name, row))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error fetching all SDN fabrics for Proxmox cluster %s", px.name)
            results.append(SdnFabricSchema(cluster_name=px.name, status="error", error=str(exc)))
    return results


@router.get("/sdn/route-maps", response_model=list[SdnRouteMapSchema])
async def sdn_route_maps(pxs: ProxmoxSessionsDep) -> list[SdnRouteMapSchema]:
    """List SDN route-map objects across all clusters (PVE 9.2+).

    Proxies ``GET /cluster/sdn/route-maps``.  Route maps allow
    fine-grained BGP/EVPN route filtering for SDN BGP fabric protocols.
    """
    results: list[SdnRouteMapSchema] = []
    for px in pxs:
        try:
            raw = await resolve_async(px.session("cluster/sdn/route-maps").get())
            rows = raw if isinstance(raw, list) else (raw.root if hasattr(raw, "root") else [raw])
            for row in rows:
                results.append(_to_route_map(px.name, row))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error fetching SDN route-maps for Proxmox cluster %s", px.name)
            results.append(SdnRouteMapSchema(cluster_name=px.name, status="error", error=str(exc)))
    return results


@router.get("/sdn/prefix-lists", response_model=list[SdnPrefixListSchema])
async def sdn_prefix_lists(pxs: ProxmoxSessionsDep) -> list[SdnPrefixListSchema]:
    """List SDN prefix-list objects across all clusters (PVE 9.2+).

    Proxies ``GET /cluster/sdn/prefix-lists``.  Prefix lists define
    CIDR ranges used by route maps for BGP/EVPN route filtering.
    """
    results: list[SdnPrefixListSchema] = []
    for px in pxs:
        try:
            raw = await resolve_async(px.session("cluster/sdn/prefix-lists").get())
            rows = raw if isinstance(raw, list) else (raw.root if hasattr(raw, "root") else [raw])
            for row in rows:
                results.append(_to_prefix_list(px.name, row))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error fetching SDN prefix-lists for Proxmox cluster %s", px.name)
            results.append(
                SdnPrefixListSchema(cluster_name=px.name, status="error", error=str(exc))
            )
    return results
