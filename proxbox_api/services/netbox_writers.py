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

from pydantic import BaseModel, ConfigDict, Field

from proxbox_api.netbox_rest import (
    ReconcileResult,
    ReconcileStatus,
    rest_reconcile_async_with_status,
)
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxClusterSyncState,
    NetBoxClusterTypeSyncState,
    NetBoxCustomFieldSyncState,
    NetBoxDeviceRoleSyncState,
    NetBoxDeviceTypeSyncState,
    NetBoxManufacturerSyncState,
    NetBoxVirtualMachineTypeSyncState,
)
from proxbox_api.schemas.netbox.extras import TagSchema

if TYPE_CHECKING:
    from proxbox_api.netbox_rest import RestRecord


class _NetBoxChoiceSetSyncState(BaseModel):
    """Minimal sync schema for ``/api/extras/custom-field-choice-sets/``.

    Choices are stored as ``[value, label]`` pairs upstream; we accept tuples
    or lists and normalize via ``model_validator`` only if needed. This schema
    intentionally limits the writable surface to the orchestrator's needs.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    description: str | None = None
    base_choices: str | None = None
    extra_choices: list[list[str]] = Field(default_factory=list)
    order_alphabetically: bool = False


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


async def upsert_manufacturer(
    nb: object,
    *,
    name: str,
    slug: str,
    tag_refs: list[dict[str, object]] | None = None,
) -> UpsertResult:
    """Create-or-update the ``dcim.Manufacturer`` named ``name``."""
    result = await rest_reconcile_async_with_status(
        nb,
        "/api/dcim/manufacturers/",
        lookup={"slug": slug},
        payload={
            "name": name,
            "slug": slug,
            "tags": tag_refs or [],
            "custom_fields": _last_updated_cf(),
        },
        schema=NetBoxManufacturerSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
    )
    return _from_reconcile(result)


async def upsert_device_type(
    nb: object,
    *,
    model: str,
    slug: str,
    manufacturer_id: int | None,
    tag_refs: list[dict[str, object]] | None = None,
) -> UpsertResult:
    """Create-or-update the ``dcim.DeviceType`` identified by ``model``."""
    result = await rest_reconcile_async_with_status(
        nb,
        "/api/dcim/device-types/",
        lookup={"model": model},
        payload={
            "model": model,
            "slug": slug,
            "manufacturer": manufacturer_id,
            "tags": tag_refs or [],
            "custom_fields": _last_updated_cf(),
        },
        schema=NetBoxDeviceTypeSyncState,
        current_normalizer=lambda record: {
            "model": record.get("model"),
            "slug": record.get("slug"),
            "manufacturer": _relation_id_or_none(record.get("manufacturer")),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
    )
    return _from_reconcile(result)


async def upsert_device_role(
    nb: object,
    *,
    name: str,
    slug: str,
    color: str,
    description: str | None = None,
    vm_role: bool | None = None,
    tag_refs: list[dict[str, object]] | None = None,
) -> UpsertResult:
    """Create-or-update the ``dcim.DeviceRole`` identified by ``slug``.

    Use ``vm_role=True`` for VM-side roles (e.g. ``Virtual Machine (QEMU)``).
    """
    payload: dict[str, object] = {
        "name": name,
        "slug": slug,
        "color": color,
        "tags": tag_refs or [],
        "custom_fields": _last_updated_cf(),
    }
    if description is not None:
        payload["description"] = description
    if vm_role is not None:
        payload["vm_role"] = vm_role

    result = await rest_reconcile_async_with_status(
        nb,
        "/api/dcim/device-roles/",
        lookup={"slug": slug},
        payload=payload,
        schema=NetBoxDeviceRoleSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "color": record.get("color"),
            "description": record.get("description"),
            "vm_role": record.get("vm_role"),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
    )
    return _from_reconcile(result)


async def upsert_vm_role(
    nb: object,
    *,
    name: str,
    slug: str,
    color: str,
    description: str | None = None,
    tag_refs: list[dict[str, object]] | None = None,
) -> UpsertResult:
    """Create-or-update a VM-side ``dcim.DeviceRole`` (``vm_role=True``)."""
    return await upsert_device_role(
        nb,
        name=name,
        slug=slug,
        color=color,
        description=description,
        vm_role=True,
        tag_refs=tag_refs,
    )


async def upsert_vm_type(
    nb: object,
    *,
    name: str,
    slug: str,
    description: str | None = None,
    tag_refs: list[dict[str, object]] | None = None,
) -> UpsertResult:
    """Create-or-update a ``virtualization.VirtualMachineType`` (NetBox 4.6+).

    Callers must gate on
    :func:`proxbox_api.netbox_version.supports_virtual_machine_type` before
    invoking this helper. The orchestrator does that check up front.
    """
    payload: dict[str, object] = {
        "name": name,
        "slug": slug,
        "tags": tag_refs or [],
    }
    if description is not None:
        payload["description"] = description

    result = await rest_reconcile_async_with_status(
        nb,
        "/api/virtualization/virtual-machine-types/",
        lookup={"slug": slug},
        payload=payload,
        schema=NetBoxVirtualMachineTypeSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "description": record.get("description"),
            "tags": record.get("tags"),
        },
    )
    return _from_reconcile(result)


async def upsert_custom_field(
    nb: object,
    *,
    name: str,
    type: str,
    label: str,
    object_types: list[str],
    description: str | None = None,
    group_name: str | None = None,
    ui_visible: str = "always",
    ui_editable: str = "hidden",
    weight: int = 100,
    filter_logic: str = "loose",
    search_weight: int = 1000,
    related_object_type: str | None = None,
) -> UpsertResult:
    """Create-or-update an ``extras.CustomField`` identified by ``name``.

    ``object_types`` uses NetBox's ``app.model`` content-type strings, e.g.
    ``"virtualization.virtualmachine"``. The helper preserves the existing
    inline ``create_custom_fields`` payload surface so behavior is identical
    when called either from the bootstrap orchestrator or the legacy lazy
    code path.
    """
    payload: dict[str, object] = {
        "name": name,
        "type": type,
        "label": label,
        "ui_visible": ui_visible,
        "ui_editable": ui_editable,
        "weight": weight,
        "filter_logic": filter_logic,
        "search_weight": search_weight,
        "object_types": sorted(dict.fromkeys(object_types)),
    }
    if description is not None:
        payload["description"] = description
    if group_name is not None:
        payload["group_name"] = group_name
    if related_object_type is not None:
        payload["related_object_type"] = related_object_type

    result = await rest_reconcile_async_with_status(
        nb,
        "/api/extras/custom-fields/",
        lookup={"name": name},
        payload=payload,
        schema=NetBoxCustomFieldSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "type": (
                record.get("type", {}).get("value")
                if isinstance(record.get("type"), dict)
                else record.get("type")
            ),
            "label": record.get("label"),
            "description": record.get("description"),
            "ui_visible": (
                record.get("ui_visible", {}).get("value")
                if isinstance(record.get("ui_visible"), dict)
                else record.get("ui_visible")
            ),
            "ui_editable": (
                record.get("ui_editable", {}).get("value")
                if isinstance(record.get("ui_editable"), dict)
                else record.get("ui_editable")
            ),
            "weight": record.get("weight"),
            "filter_logic": (
                record.get("filter_logic", {}).get("value")
                if isinstance(record.get("filter_logic"), dict)
                else record.get("filter_logic")
            ),
            "search_weight": record.get("search_weight"),
            "group_name": record.get("group_name"),
            "object_types": record.get("object_types") or [],
            "related_object_type": record.get("related_object_type"),
        },
    )
    return _from_reconcile(result)


async def upsert_tag(
    nb: object,
    *,
    name: str,
    slug: str,
    color: str,
    description: str = "",
) -> UpsertResult:
    """Create-or-update an ``extras.Tag`` identified by ``slug``."""
    result = await rest_reconcile_async_with_status(
        nb,
        "/api/extras/tags/",
        lookup={"slug": slug},
        payload={
            "name": name,
            "slug": slug,
            "color": color,
            "description": description,
        },
        schema=TagSchema,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "slug": record.get("slug"),
            "color": record.get("color"),
            "description": record.get("description"),
        },
        patchable_fields={"name", "color", "description"},
    )
    return _from_reconcile(result)


async def upsert_choice_set(
    nb: object,
    *,
    name: str,
    extra_choices: list[list[str]] | None = None,
    base_choices: str | None = None,
    description: str | None = None,
    order_alphabetically: bool = False,
) -> UpsertResult:
    """Create-or-update an ``extras.CustomFieldChoiceSet`` identified by ``name``.

    ``extra_choices`` is a list of ``[value, label]`` pairs. ``base_choices``
    is the optional name of a built-in NetBox choice set (e.g. ``"IATA"``).
    The bootstrap orchestrator currently exposes this helper without invoking
    it against a concrete inventory; roadmap consumers will populate
    ``extra_choices`` when they ship.
    """
    payload: dict[str, object] = {
        "name": name,
        "extra_choices": extra_choices or [],
        "order_alphabetically": order_alphabetically,
    }
    if base_choices is not None:
        payload["base_choices"] = base_choices
    if description is not None:
        payload["description"] = description

    result = await rest_reconcile_async_with_status(
        nb,
        "/api/extras/custom-field-choice-sets/",
        lookup={"name": name},
        payload=payload,
        schema=_NetBoxChoiceSetSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "description": record.get("description"),
            "base_choices": (
                record.get("base_choices", {}).get("value")
                if isinstance(record.get("base_choices"), dict)
                else record.get("base_choices")
            ),
            "extra_choices": record.get("extra_choices") or [],
            "order_alphabetically": bool(record.get("order_alphabetically", False)),
        },
    )
    return _from_reconcile(result)
