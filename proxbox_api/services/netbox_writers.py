"""Typed drift-detecting NetBox write helpers.

This module exposes ``upsert_*`` helpers that wrap
:func:`proxbox_api.netbox_rest.rest_reconcile_async_with_status` with the
schema, path, and current-record normalizer baked in for each NetBox object
kind.

Each helper performs a GET, diffs the desired payload against the current
record, PATCHes only when a real diff exists, and reports whether the call
resulted in a create, update, or no-op via the returned
:class:`UpsertResult.status`. The underlying reconcile primitive already
suppresses PATCH on no-ops, so this layer's job is to give callers (sync
services, full-update orchestration) a typed surface and a stable status
enum to drive per-resource counters and idempotency assertions.

This file currently ships the helpers needed by the cluster individual-sync
migration (issue #357 PR 1). Follow-up issues add one helper per migrated
reconciler (``upsert_vm``, ``upsert_interface``, ``upsert_ip_address``, etc.)
using the same shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from proxbox_api.netbox_rest import (
    ReconcileResult,
    ReconcileStatus,
    rest_reconcile_async_with_status,
)
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxClusterSyncState,
    NetBoxClusterTypeSyncState,
)

if TYPE_CHECKING:
    from proxbox_api.netbox_rest import RestRecord


@dataclass(frozen=True, slots=True)
class UpsertResult:
    """Outcome of a typed ``upsert_*`` call.

    Mirrors :class:`proxbox_api.netbox_rest.ReconcileResult` but kept as the
    public typed surface so callers do not have to import from the low-level
    REST module.
    """

    record: RestRecord
    status: ReconcileStatus


def _from_reconcile(result: ReconcileResult) -> UpsertResult:
    return UpsertResult(record=result.record, status=result.status)


def _relation_id_or_none(value: object) -> int | None:
    if isinstance(value, dict):
        candidate = value.get("id")
        return int(candidate) if isinstance(candidate, int) else None
    if isinstance(value, int):
        return value
    return None


def _last_updated_cf() -> dict[str, str]:
    return {"proxmox_last_updated": datetime.now(timezone.utc).isoformat()}


async def upsert_cluster_type(
    nb: object,
    *,
    mode: str,
    tag_refs: list[dict[str, object]],
) -> UpsertResult:
    """Create-or-update the ``virtualization.ClusterType`` for the given mode.

    ``mode`` is the Proxmox cluster mode (e.g. ``"cluster"``, ``"standalone"``)
    and doubles as the NetBox slug.
    """
    result = await rest_reconcile_async_with_status(
        nb,
        "/api/virtualization/cluster-types/",
        lookup={"slug": mode},
        payload={
            "name": mode.capitalize(),
            "slug": mode,
            "description": f"Proxmox {mode} mode",
            "tags": tag_refs,
            "custom_fields": _last_updated_cf(),
        },
        schema=NetBoxClusterTypeSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "description": record.get("description"),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
    )
    return _from_reconcile(result)


async def upsert_cluster(
    nb: object,
    *,
    cluster_name: str,
    cluster_type_id: int | None,
    mode: str,
    tag_refs: list[dict[str, object]],
) -> UpsertResult:
    """Create-or-update the ``virtualization.Cluster`` named ``cluster_name``.

    ``mode`` only influences the description string; the payload itself is
    limited to fields the existing ``NetBoxClusterSyncState`` schema allows.
    Scope (``site``/``tenant``) wiring is out of scope for issue #357 PR 1 —
    the cluster reconciler did not write those fields on v0.0.11 either.
    """
    payload: dict[str, object] = {
        "name": cluster_name,
        "type": cluster_type_id,
        "description": f"Proxmox {mode} cluster.",
        "tags": tag_refs,
        "custom_fields": _last_updated_cf(),
    }

    result = await rest_reconcile_async_with_status(
        nb,
        "/api/virtualization/clusters/",
        lookup={"name": cluster_name},
        payload=payload,
        schema=NetBoxClusterSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "type": _relation_id_or_none(record.get("type")),
            "description": record.get("description"),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
    )
    return _from_reconcile(result)
