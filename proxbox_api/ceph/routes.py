"""Read-only Ceph routes mounted under ``/ceph``.

The v1 surface intentionally collects only Proxmox-managed Ceph state.  Every
sync endpoint accepts ``netbox_branch_schema_id`` so the NetBox plugin can keep
one branch-aware contract when persistence is added.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from proxbox_api.ceph.schemas import (
    CephStatusItem,
    CephStatusResponse,
    CephSyncResource,
    CephSyncResponse,
    CephSyncSummary,
)
from proxbox_api.logger import logger
from proxbox_api.session.proxmox import ProxmoxSessionsDep

if TYPE_CHECKING:
    from proxbox_api.session.proxmox import ProxmoxSession

router = APIRouter()


def _session_name(px: ProxmoxSession) -> str:
    return (
        getattr(px, "name", None)
        or getattr(px, "cluster_name", None)
        or getattr(px, "node_name", None)
        or getattr(px, "domain", None)
        or getattr(px, "ip_address", None)
        or "proxmox"
    )


def _session_host(px: ProxmoxSession) -> str | None:
    return getattr(px, "domain", None) or getattr(px, "ip_address", None)


def _node_names(px: ProxmoxSession) -> list[str]:
    nodes: list[str] = []
    for item in getattr(px, "cluster_status", None) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "node" and item.get("name"):
            nodes.append(str(item["name"]))
    if nodes:
        return sorted(set(nodes))
    node_name = getattr(px, "node_name", None)
    if node_name:
        return [str(node_name)]
    return ["localhost"]


def _client_class() -> Any:
    try:
        from proxmox_sdk.ceph import CephClient  # type: ignore[import-not-found]  # noqa: PLC0415
    except ImportError:
        from proxbox_api.ceph.client import CephClient  # noqa: PLC0415

    return CephClient


def _client_for(px: ProxmoxSession) -> Any:
    """Wrap a resolved Proxmox session in a read-only Ceph client."""
    try:
        CephClient = _client_class()
    except ImportError as exc:  # pragma: no cover - defensive
        raise HTTPException(
            status_code=503,
            detail=f"Ceph support unavailable; /ceph/* disabled ({exc})",
        ) from exc

    sdk = getattr(px, "session", None) or getattr(px, "proxmox", None)
    if sdk is None:
        raise HTTPException(
            status_code=503,
            detail=f"Proxmox session {_session_name(px)!r} is not connected",
        )
    try:
        return CephClient.from_sdk(sdk)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _count_payload(payload: Any) -> int:
    if payload is None:
        return 0
    if isinstance(payload, list | tuple | set):
        return len(payload)
    if isinstance(payload, dict):
        return 1 if payload else 0
    return 1


@router.get("/status", response_model=CephStatusResponse)
async def ceph_status(pxs: ProxmoxSessionsDep) -> CephStatusResponse:
    """Report Ceph reachability/health for each resolved Proxmox endpoint."""
    items: list[CephStatusItem] = []
    for px in pxs:
        item = CephStatusItem(
            name=_session_name(px),
            host=_session_host(px),
            port=getattr(px, "http_port", None),
            reachable=False,
        )
        try:
            client = _client_for(px)
            status = await client.status()
            item.reachable = True
            item.health = getattr(status, "health", None)
            item.fsid = getattr(status, "fsid", None)
        except HTTPException as http_exc:
            item.reason = str(http_exc.detail)
        except Exception as exc:  # noqa: BLE001
            item.reason = f"{type(exc).__name__}: {exc}"
            logger.info("Ceph status probe failed for endpoint %s: %s", _session_name(px), exc)
        items.append(item)
    return CephStatusResponse(items=items)


async def _sync_one(  # noqa: C901 - explicit stages keep the read-only flow clear
    px: ProxmoxSession,
    resource: CephSyncResource,
    netbox_branch_schema_id: str | None,
) -> CephSyncSummary:
    nodes = _node_names(px)
    summary = CephSyncSummary(
        name=_session_name(px),
        host=_session_host(px),
        resource=resource,
        nodes=nodes,
        netbox_branch_schema_id=netbox_branch_schema_id,
    )

    try:
        client = _client_for(px)
    except HTTPException as http_exc:
        summary.errors.append(str(http_exc.detail))
        return summary

    try:
        if resource in ("status", "full"):
            status = await client.cluster.status()
            metadata = await client.cluster.metadata()
            summary.fetched += _count_payload(status) + _count_payload(metadata)

        if resource in ("daemons", "full"):
            for node in nodes:
                summary.fetched += len(await client.nodes.monitors(node))
                summary.fetched += len(await client.nodes.managers(node))
                summary.fetched += len(await client.nodes.metadata_servers(node))

        if resource in ("osds", "full"):
            for node in nodes:
                summary.fetched += len(await client.nodes.osds(node))

        if resource in ("pools", "full"):
            for node in nodes:
                summary.fetched += len(await client.nodes.pools(node))

        if resource in ("filesystems", "full"):
            for node in nodes:
                summary.fetched += len(await client.nodes.filesystems(node))

        if resource in ("crush", "full"):
            for node in nodes:
                summary.fetched += _count_payload(await client.nodes.crush(node))
                summary.fetched += len(await client.nodes.rules(node))

        if resource in ("flags", "full"):
            summary.fetched += len(await client.cluster.flags())
    except Exception as exc:  # noqa: BLE001
        summary.errors.append(f"{type(exc).__name__}: {exc}")

    return summary


async def _sync_all(
    pxs: list[ProxmoxSession],
    resource: CephSyncResource,
    netbox_branch_schema_id: str | None,
) -> CephSyncResponse:
    items = [await _sync_one(px, resource, netbox_branch_schema_id) for px in pxs]
    return CephSyncResponse(items=items)


@router.get("/sync/full", response_model=CephSyncResponse)
async def ceph_sync_full(
    pxs: ProxmoxSessionsDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> CephSyncResponse:
    return await _sync_all(pxs, "full", netbox_branch_schema_id)


@router.get("/sync/status", response_model=CephSyncResponse)
async def ceph_sync_status(
    pxs: ProxmoxSessionsDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> CephSyncResponse:
    return await _sync_all(pxs, "status", netbox_branch_schema_id)


@router.get("/sync/daemons", response_model=CephSyncResponse)
async def ceph_sync_daemons(
    pxs: ProxmoxSessionsDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> CephSyncResponse:
    return await _sync_all(pxs, "daemons", netbox_branch_schema_id)


@router.get("/sync/osds", response_model=CephSyncResponse)
async def ceph_sync_osds(
    pxs: ProxmoxSessionsDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> CephSyncResponse:
    return await _sync_all(pxs, "osds", netbox_branch_schema_id)


@router.get("/sync/pools", response_model=CephSyncResponse)
async def ceph_sync_pools(
    pxs: ProxmoxSessionsDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> CephSyncResponse:
    return await _sync_all(pxs, "pools", netbox_branch_schema_id)


@router.get("/sync/filesystems", response_model=CephSyncResponse)
async def ceph_sync_filesystems(
    pxs: ProxmoxSessionsDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> CephSyncResponse:
    return await _sync_all(pxs, "filesystems", netbox_branch_schema_id)


@router.get("/sync/crush", response_model=CephSyncResponse)
async def ceph_sync_crush(
    pxs: ProxmoxSessionsDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> CephSyncResponse:
    return await _sync_all(pxs, "crush", netbox_branch_schema_id)


@router.get("/sync/flags", response_model=CephSyncResponse)
async def ceph_sync_flags(
    pxs: ProxmoxSessionsDep,
    netbox_branch_schema_id: Annotated[str | None, Query()] = None,
) -> CephSyncResponse:
    return await _sync_all(pxs, "flags", netbox_branch_schema_id)


__all__ = ["router"]
