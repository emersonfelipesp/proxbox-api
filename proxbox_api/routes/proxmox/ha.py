"""Proxmox cluster High-Availability endpoints (read-only).

Surfaces `/cluster/ha/status/current`, `/cluster/ha/resources`,
`/cluster/ha/groups` (PVE ≤ 8.x), and `/cluster/ha/rules` (PVE 9.x+)
from Proxmox so the netbox-proxbox plugin can render HA state on the
NetBox VM detail page and a cluster-wide HA page.

PVE 9.x removed `cluster/ha/groups` in favour of `cluster/ha/rules`.
The `/ha/groups` endpoint degrades to an empty list on PVE 9.x clusters
(the helper detects the "migrated to rules" HTTP 500 and returns `[]`
silently).  Use the new `/ha/rules` endpoint for PVE 9.x HA rule data.
`/ha/summary` now includes both `groups` and `rules` so consumers can
handle mixed-version clusters.

PVE 9.2 additions (disarm/arm HA stack, manager status, CRS config):
- ``POST /ha/disarm`` / ``POST /ha/arm`` — disarm/arm the HA stack
  cluster-wide via the new ``cluster/ha/status/disarm-ha`` and
  ``cluster/ha/status/arm-ha`` CRM commands.  Safe for planned
  maintenance windows without triggering node fencing.
- ``GET /ha/manager-status`` — CRM manager status (queue depths, CRS
  scheduling state).
- ``GET /ha/crs`` — CRS (Cluster Resource Scheduler) configuration
  extracted from datacenter options; drives the PVE 9.2 dynamic load
  balancer that migrates HA-managed guests to lower node imbalance.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from fastapi import APIRouter
from pydantic import BaseModel

from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.services.proxmox_helpers import (
    get_ha_groups,
    get_ha_resources,
    get_ha_rules,
    get_ha_status_current,
)
from proxbox_api.session.proxmox import ProxmoxSession, ProxmoxSessionsDep

_T = TypeVar("_T")

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
    """An HA group definition (PVE ≤ 8.x)."""

    cluster_name: str | None = None
    group: str | None = None
    type: str | None = None
    nodes: str | None = None
    restricted: bool | None = None
    nofailback: bool | None = None
    comment: str | None = None
    digest: str | None = None


class HaRuleSchema(BaseModel):
    """An HA rule definition (PVE 9.x+).

    PVE 9.x replaced ``cluster/ha/groups`` with ``cluster/ha/rules``.
    Rules carry ``type`` (``node-affinity`` / ``resource-affinity``),
    optional ``affinity`` (``positive`` / ``negative``), and a
    ``resources`` selector string instead of a plain group name.
    """

    cluster_name: str | None = None
    rule: str | None = None
    type: str | None = None
    affinity: str | None = None
    nodes: str | None = None
    resources: str | None = None
    strict: bool | None = None
    disable: bool | None = None
    comment: str | None = None


class HaSummarySchema(BaseModel):
    """Single-call payload for the cluster-wide HA page.

    Aggregates status/groups/resources/rules across every configured
    Proxmox cluster so the NetBox plugin doesn't fan out multiple HTTP
    requests per page render.

    ``groups`` is populated for PVE ≤ 8.x clusters; ``rules`` is
    populated for PVE 9.x+ clusters.  Both default to ``[]`` so
    mixed-version and single-version deployments work without changes.
    """

    status: list[HaStatusItemSchema] = []
    groups: list[HaGroupSchema] = []
    resources: list[HaResourceSchema] = []
    rules: list[HaRuleSchema] = []


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


def _str(row: dict[str, object], key: str) -> str | None:
    v = row.get(key)
    return v if isinstance(v, str) else None


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
        group=_str(row, "group"),
        type=_str(row, "type"),
        nodes=_str(row, "nodes"),
        restricted=_coerce_bool(row.get("restricted")),
        nofailback=_coerce_bool(row.get("nofailback")),
        comment=_str(row, "comment"),
        digest=_str(row, "digest"),
    )


def _rule_to_schema(cluster_name: str, row: dict[str, object]) -> HaRuleSchema:
    return HaRuleSchema(
        cluster_name=cluster_name,
        rule=_str(row, "rule"),
        type=_str(row, "type"),
        affinity=_str(row, "affinity"),
        nodes=_str(row, "nodes"),
        resources=_str(row, "resources"),
        strict=_coerce_bool(row.get("strict")),
        disable=_coerce_bool(row.get("disable")),
        comment=_str(row, "comment"),
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


async def _aggregate_ha_items(
    pxs: ProxmoxSessionsDep,
    list_fn: Callable[[ProxmoxSession], Awaitable[object]],
    detail_fn: Callable[[ProxmoxSession, str], Awaitable[object]],
    id_key: str,
    to_schema: Callable[[str, dict[str, object]], _T],
    error_label: str,
) -> list[_T]:
    aggregated: list[_T] = []
    for px in pxs:
        try:
            rows = await list_fn(px)
        except Exception:  # noqa: BLE001
            logger.exception("Error fetching %s for Proxmox cluster %s", error_label, px.name)
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            item_id = row.get(id_key)
            detail: dict[str, object] = dict(row)
            if isinstance(item_id, str):
                try:
                    detail_payload = await detail_fn(px, item_id)
                    if isinstance(detail_payload, dict):
                        detail = {**detail, **detail_payload}
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "%s detail fetch failed for %s on %s", error_label, item_id, px.name
                    )
            aggregated.append(to_schema(px.name, detail))
    return aggregated


@router.get("/ha/groups", response_model=list[HaGroupSchema])
async def ha_groups(pxs: ProxmoxSessionsDep) -> list[HaGroupSchema]:
    """List configured HA groups across all clusters."""
    return await _aggregate_ha_items(
        pxs,
        list_fn=lambda px: get_ha_groups(px),
        detail_fn=lambda px, v: get_ha_groups(px, group=v),
        id_key="group",
        to_schema=_group_to_schema,
        error_label="HA groups",
    )


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


@router.get("/ha/rules", response_model=list[HaRuleSchema])
async def ha_rules(pxs: ProxmoxSessionsDep) -> list[HaRuleSchema]:
    """List configured HA rules across all clusters (PVE 9.x+).

    PVE 9.x replaced ``cluster/ha/groups`` with ``cluster/ha/rules``.
    Returns an empty list on pre-9.x clusters where the endpoint does not
    exist.
    """
    return await _aggregate_ha_items(
        pxs,
        list_fn=lambda px: get_ha_rules(px),
        detail_fn=lambda px, v: get_ha_rules(px, rule=v),
        id_key="rule",
        to_schema=_rule_to_schema,
        error_label="HA rules",
    )


@router.get("/ha/summary", response_model=HaSummarySchema)
async def ha_summary(pxs: ProxmoxSessionsDep) -> HaSummarySchema:
    """Return a single envelope of status, groups, resources, and rules.

    Calls the four underlying handlers in parallel via `asyncio.gather`
    so the NetBox cluster-wide HA page only triggers one round-trip.
    ``groups`` is populated for PVE ≤ 8.x; ``rules`` is populated for
    PVE 9.x+.  Both default to ``[]`` when the node type does not support
    the respective endpoint.
    """
    status_task = asyncio.create_task(ha_status(pxs))
    groups_task = asyncio.create_task(ha_groups(pxs))
    resources_task = asyncio.create_task(ha_resources(pxs))
    rules_task = asyncio.create_task(ha_rules(pxs))
    status_rows, group_rows, resource_rows, rule_rows = await asyncio.gather(
        status_task, groups_task, resources_task, rules_task
    )
    return HaSummarySchema(
        status=status_rows,
        groups=group_rows,
        resources=resource_rows,
        rules=rule_rows,
    )


def _runtime_index(cluster_name: str, status_rows: list[object]) -> dict[str, HaStatusItemSchema]:
    """Build {sid: HaStatusItemSchema} from raw `status/current` items."""
    index: dict[str, HaStatusItemSchema] = {}
    for row in status_rows:
        item = _status_item_to_schema(cluster_name, row)
        if item.sid:
            index[item.sid] = item
    return index


# ---------------------------------------------------------------------------
# PVE 9.2 additions: disarm/arm, manager-status, CRS config
# ---------------------------------------------------------------------------


class HaManagerStatusSchema(BaseModel):
    """CRM manager status from ``/cluster/ha/status/manager_status``."""

    cluster_name: str | None = None
    manager_status: str | None = None
    timestamp: int | None = None
    quorum_ok: bool | None = None
    mode: str | None = None


class HaDisarmResultSchema(BaseModel):
    """Result of a disarm-ha or arm-ha CRM command."""

    cluster_name: str | None = None
    status: str = "ok"
    error: str | None = None


class HaCrsConfigSchema(BaseModel):
    """CRS (Cluster Resource Scheduler) configuration (PVE 9.2+).

    Extracted from the ``crs`` sub-object of ``GET /cluster/options``.
    CRS drives the dynamic load balancer that migrates HA-managed guests
    to lower the overall node imbalance across the cluster.
    """

    cluster_name: str | None = None
    ha: str | None = None
    status: str = "ok"
    error: str | None = None


@router.post("/ha/disarm", response_model=list[HaDisarmResultSchema])
async def ha_disarm(pxs: ProxmoxSessionsDep) -> list[HaDisarmResultSchema]:
    """Disarm the HA stack cluster-wide (PVE 9.2+).

    Calls ``POST /cluster/ha/status/disarm-ha`` on each configured
    Proxmox cluster.  While disarmed, HA-managed guests are **not**
    automatically restarted or migrated, and no fencing events are
    triggered.  Use ``POST /ha/arm`` to re-enable HA after maintenance.
    """
    results: list[HaDisarmResultSchema] = []
    for px in pxs:
        try:
            await resolve_async(px.session("cluster/ha/status/disarm-ha").post())
            results.append(HaDisarmResultSchema(cluster_name=px.name))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error disarming HA for Proxmox cluster %s", px.name)
            results.append(
                HaDisarmResultSchema(cluster_name=px.name, status="error", error=str(exc))
            )
    return results


@router.post("/ha/arm", response_model=list[HaDisarmResultSchema])
async def ha_arm(pxs: ProxmoxSessionsDep) -> list[HaDisarmResultSchema]:
    """Re-arm the HA stack cluster-wide (PVE 9.2+).

    Calls ``POST /cluster/ha/status/arm-ha`` on each configured
    Proxmox cluster.  HA resources return to their previous state after
    the maintenance window is completed.
    """
    results: list[HaDisarmResultSchema] = []
    for px in pxs:
        try:
            await resolve_async(px.session("cluster/ha/status/arm-ha").post())
            results.append(HaDisarmResultSchema(cluster_name=px.name))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error arming HA for Proxmox cluster %s", px.name)
            results.append(
                HaDisarmResultSchema(cluster_name=px.name, status="error", error=str(exc))
            )
    return results


@router.get("/ha/manager-status", response_model=list[HaManagerStatusSchema])
async def ha_manager_status(pxs: ProxmoxSessionsDep) -> list[HaManagerStatusSchema]:
    """Retrieve CRM manager status across all clusters (PVE 9.2+).

    Proxies ``GET /cluster/ha/status/manager_status``.  Returns queue
    depths, CRS scheduling state, and quorum health per cluster.
    """
    results: list[HaManagerStatusSchema] = []
    for px in pxs:
        try:
            raw = await resolve_async(px.session("cluster/ha/status/manager_status").get())
            data: dict[str, object] = {}
            if hasattr(raw, "model_dump"):
                data = raw.model_dump(mode="python", by_alias=True, exclude_none=True)
            elif isinstance(raw, dict):
                data = raw
            results.append(
                HaManagerStatusSchema(
                    cluster_name=px.name,
                    manager_status=str(data.get("manager_status"))
                    if data.get("manager_status") is not None
                    else None,
                    timestamp=data.get("timestamp")
                    if isinstance(data.get("timestamp"), int)
                    else None,
                    quorum_ok=_coerce_bool(data.get("quorum_ok") or data.get("quorum-ok")),
                    mode=str(data.get("mode")) if data.get("mode") is not None else None,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error fetching HA manager status for Proxmox cluster %s", px.name)
            results.append(
                HaManagerStatusSchema(cluster_name=px.name, manager_status=f"error: {exc}")
            )
    return results


@router.get("/ha/crs", response_model=list[HaCrsConfigSchema])
async def ha_crs(pxs: ProxmoxSessionsDep) -> list[HaCrsConfigSchema]:
    """Retrieve CRS (Cluster Resource Scheduler) config across all clusters (PVE 9.2+).

    Extracts the ``crs`` sub-object from ``GET /cluster/options``.
    The CRS ``ha`` field controls the scheduling mode:
    ``static`` (pre-9.2 default), ``basic``, or ``dynamic`` (PVE 9.2).
    """
    results: list[HaCrsConfigSchema] = []
    for px in pxs:
        try:
            raw = await resolve_async(px.session("cluster/options").get())
            data: dict[str, object] = {}
            if hasattr(raw, "model_dump"):
                data = raw.model_dump(mode="python", by_alias=True, exclude_none=True)
            elif isinstance(raw, dict):
                data = raw
            crs = data.get("crs")
            crs_ha: str | None = None
            if isinstance(crs, dict):
                crs_ha = str(crs.get("ha")) if crs.get("ha") is not None else None
            elif isinstance(crs, str):
                crs_ha = crs
            results.append(HaCrsConfigSchema(cluster_name=px.name, ha=crs_ha))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error fetching CRS config for Proxmox cluster %s", px.name)
            results.append(HaCrsConfigSchema(cluster_name=px.name, status="error", error=str(exc)))
    return results
