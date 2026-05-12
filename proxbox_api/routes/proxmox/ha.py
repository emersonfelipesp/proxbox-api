"""Proxmox cluster High-Availability endpoints (read-only).

Surfaces `/cluster/ha/status/current`, `/cluster/ha/resources`, and
`/cluster/ha/groups` from Proxmox so the netbox-proxbox plugin can render
HA state on the NetBox VM detail page and a cluster-wide HA page.

All endpoints are read-only by design. HA mutations (add/remove/migrate)
are intentionally out of scope and will land in a follow-up release.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from proxbox_api.logger import logger
from proxbox_api.services.proxmox_helpers import (
    get_ha_groups,
    get_ha_resources,
    get_ha_status_current,
)
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()


class HaStatusItemSchema(BaseModel):
    """One row from `/cluster/ha/status/current`.

    The Proxmox payload mixes service rows with cluster-wide rows
    (quorum, master, lrm:<node>); fields are optional accordingly.
    """

    cluster_name: str | None = None
    id: str | None = None
    type: str | None = None
    sid: str | None = None
    node: str | None = None
    state: str | None = None
    status: str | None = None
    crm_state: str | None = None
    request_state: str | None = None
    quorate: bool | None = None
    failback: bool | None = None
    max_relocate: int | None = None
    max_restart: int | None = None
    timestamp: int | None = None


class HaResourceSchema(BaseModel):
    """A resource configured under HA management."""

    cluster_name: str | None = None
    sid: str | None = None
    type: str | None = None
    state: str | None = None
    group: str | None = None
    max_relocate: int | None = None
    max_restart: int | None = None
    failback: bool | None = None
    comment: str | None = None
    digest: str | None = None
    # Live runtime fields, merged from `status/current` when available.
    node: str | None = None
    crm_state: str | None = None
    request_state: str | None = None
    status: str | None = None


class HaGroupSchema(BaseModel):
    """An HA group definition."""

    cluster_name: str | None = None
    group: str | None = None
    type: str | None = None
    nodes: str | None = None
    restricted: bool | None = None
    nofailback: bool | None = None
    comment: str | None = None
    digest: str | None = None


class HaSummarySchema(BaseModel):
    """Single-call payload for the cluster-wide HA page.

    Aggregates status/groups/resources across every configured Proxmox
    cluster so the NetBox plugin doesn't fan out four HTTP requests per
    page render.
    """

    status: list[HaStatusItemSchema]
    groups: list[HaGroupSchema]
    resources: list[HaResourceSchema]


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _coerce_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off", ""):
            return False
    return None


def _status_item_to_schema(cluster_name: str, item: object) -> HaStatusItemSchema:
    if hasattr(item, "model_dump"):
        data = item.model_dump(mode="python", by_alias=True, exclude_none=True)
    elif isinstance(item, dict):
        data = dict(item)
    else:
        data = {}
    return HaStatusItemSchema(
        cluster_name=cluster_name,
        id=data.get("id"),
        type=str(data.get("type")) if data.get("type") is not None else None,
        sid=data.get("sid"),
        node=data.get("node"),
        state=data.get("state"),
        status=data.get("status"),
        crm_state=data.get("crm_state") or data.get("crm-state"),
        request_state=data.get("request_state") or data.get("request-state"),
        quorate=_coerce_bool(data.get("quorate")),
        failback=_coerce_bool(data.get("failback")),
        max_relocate=_coerce_int(data.get("max_relocate") or data.get("max-relocate")),
        max_restart=_coerce_int(data.get("max_restart") or data.get("max-restart")),
        timestamp=_coerce_int(data.get("timestamp")),
    )


def _resource_to_schema(
    cluster_name: str,
    row: dict[str, object],
    *,
    runtime: dict[str, HaStatusItemSchema] | None = None,
) -> HaResourceSchema:
    sid = row.get("sid")
    live = runtime.get(str(sid)) if (runtime and sid) else None
    return HaResourceSchema(
        cluster_name=cluster_name,
        sid=sid if isinstance(sid, str) else None,
        type=row.get("type") if isinstance(row.get("type"), str) else None,
        state=row.get("state") if isinstance(row.get("state"), str) else None,
        group=row.get("group") if isinstance(row.get("group"), str) else None,
        max_relocate=_coerce_int(row.get("max_relocate") or row.get("max-relocate")),
        max_restart=_coerce_int(row.get("max_restart") or row.get("max-restart")),
        failback=_coerce_bool(row.get("failback")),
        comment=row.get("comment") if isinstance(row.get("comment"), str) else None,
        digest=row.get("digest") if isinstance(row.get("digest"), str) else None,
        node=live.node if live else None,
        crm_state=live.crm_state if live else None,
        request_state=live.request_state if live else None,
        status=live.status if live else None,
    )


def _group_to_schema(cluster_name: str, row: dict[str, object]) -> HaGroupSchema:
    return HaGroupSchema(
        cluster_name=cluster_name,
        group=row.get("group") if isinstance(row.get("group"), str) else None,
        type=row.get("type") if isinstance(row.get("type"), str) else None,
        nodes=row.get("nodes") if isinstance(row.get("nodes"), str) else None,
        restricted=_coerce_bool(row.get("restricted")),
        nofailback=_coerce_bool(row.get("nofailback")),
        comment=row.get("comment") if isinstance(row.get("comment"), str) else None,
        digest=row.get("digest") if isinstance(row.get("digest"), str) else None,
    )


@router.get("/ha/status", response_model=list[HaStatusItemSchema])
async def ha_status(pxs: ProxmoxSessionsDep) -> list[HaStatusItemSchema]:
    """Retrieve current HA status across all configured Proxmox clusters."""
    aggregated: list[HaStatusItemSchema] = []
    for px in pxs:
        try:
            rows = await get_ha_status_current(px)
        except Exception as error:  # noqa: BLE001
            logger.exception("Error fetching HA status for Proxmox cluster %s", px.name)
            aggregated.append(HaStatusItemSchema(cluster_name=px.name, status=f"error: {error}"))
            continue
        for row in rows:
            aggregated.append(_status_item_to_schema(px.name, row))
    return aggregated


@router.get("/ha/resources", response_model=list[HaResourceSchema])
async def ha_resources(pxs: ProxmoxSessionsDep) -> list[HaResourceSchema]:
    """List all HA resources (with merged runtime state) across clusters."""
    aggregated: list[HaResourceSchema] = []
    for px in pxs:
        try:
            rows = await get_ha_resources(px)
            status_rows = await get_ha_status_current(px)
        except Exception:
            logger.exception("Error fetching HA resources for Proxmox cluster %s", px.name)
            continue
        if not isinstance(rows, list):
            continue
        runtime = _runtime_index(px.name, status_rows)
        for row in rows:
            if not isinstance(row, dict):
                continue
            sid = row.get("sid")
            detail: dict[str, object] = dict(row)
            if isinstance(sid, str):
                try:
                    detail_model = await get_ha_resources(px, sid=sid)
                    if hasattr(detail_model, "model_dump"):
                        detail = detail_model.model_dump(
                            mode="python", by_alias=True, exclude_none=True
                        )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "HA resource detail fetch failed for %s on %s",
                        sid,
                        px.name,
                    )
            aggregated.append(_resource_to_schema(px.name, detail, runtime=runtime))
    return aggregated


@router.get("/ha/resources/by-vm/{vmid}", response_model=HaResourceSchema | None)
async def ha_resource_by_vm(pxs: ProxmoxSessionsDep, vmid: int) -> HaResourceSchema | None:
    """Return the HA resource matching a VM/CT id, or null when unmanaged.

    Tries `vm:{vmid}` first, then falls back to `ct:{vmid}`. Returns
    `null` (not 404) when neither SID is HA-managed so the NetBox tab can
    render an empty state without surfacing a fake error.
    """
    for px in pxs:
        runtime: dict[str, HaStatusItemSchema] = {}
        try:
            status_rows = await get_ha_status_current(px)
            runtime = _runtime_index(px.name, status_rows)
        except Exception:  # noqa: BLE001
            logger.debug("HA status fetch failed for %s", px.name)
        for sid in (f"vm:{vmid}", f"ct:{vmid}"):
            try:
                detail_model = await get_ha_resources(px, sid=sid)
            except Exception:  # noqa: BLE001
                continue
            if hasattr(detail_model, "model_dump"):
                detail = detail_model.model_dump(mode="python", by_alias=True, exclude_none=True)
                return _resource_to_schema(px.name, detail, runtime=runtime)
    return None


@router.get("/ha/groups", response_model=list[HaGroupSchema])
async def ha_groups(pxs: ProxmoxSessionsDep) -> list[HaGroupSchema]:
    """List configured HA groups across all clusters."""
    aggregated: list[HaGroupSchema] = []
    for px in pxs:
        try:
            rows = await get_ha_groups(px)
        except Exception:
            logger.exception("Error fetching HA groups for Proxmox cluster %s", px.name)
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            group_name = row.get("group")
            detail: dict[str, object] = dict(row)
            if isinstance(group_name, str):
                try:
                    detail_payload = await get_ha_groups(px, group=group_name)
                    if isinstance(detail_payload, dict):
                        detail = {**detail, **detail_payload}
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "HA group detail fetch failed for %s on %s",
                        group_name,
                        px.name,
                    )
            aggregated.append(_group_to_schema(px.name, detail))
    return aggregated


@router.get("/ha/groups/{group}", response_model=HaGroupSchema | None)
async def ha_group_detail(pxs: ProxmoxSessionsDep, group: str) -> HaGroupSchema | None:
    """Return a single HA group's detail, or null when no cluster has it."""
    for px in pxs:
        try:
            payload = await get_ha_groups(px, group=group)
        except Exception:  # noqa: BLE001
            logger.debug("HA group detail fetch failed for %s on %s", group, px.name)
            continue
        if isinstance(payload, dict) and payload:
            payload.setdefault("group", group)
            return _group_to_schema(px.name, payload)
    return None


@router.get("/ha/summary", response_model=HaSummarySchema)
async def ha_summary(pxs: ProxmoxSessionsDep) -> HaSummarySchema:
    """Return a single envelope of status, groups, and resources.

    Calls the three underlying handlers in parallel via `asyncio.gather`
    so the NetBox cluster-wide HA page only triggers one round-trip.
    """
    status_task = asyncio.create_task(ha_status(pxs))
    groups_task = asyncio.create_task(ha_groups(pxs))
    resources_task = asyncio.create_task(ha_resources(pxs))
    status_rows, group_rows, resource_rows = await asyncio.gather(
        status_task, groups_task, resources_task
    )
    return HaSummarySchema(
        status=status_rows,
        groups=group_rows,
        resources=resource_rows,
    )


def _runtime_index(cluster_name: str, status_rows: list[object]) -> dict[str, HaStatusItemSchema]:
    """Build {sid: HaStatusItemSchema} from raw `status/current` items."""
    index: dict[str, HaStatusItemSchema] = {}
    for row in status_rows:
        item = _status_item_to_schema(cluster_name, row)
        if item.sid:
            index[item.sid] = item
    return index
