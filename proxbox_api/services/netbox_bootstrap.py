"""Eager, idempotent NetBox-side bootstrap of supporting objects.

This module ships the bootstrap orchestrator described by issue #358. On
``proxbox-api`` lifespan startup it walks a hardcoded inventory of objects
the sync flows depend on (cluster type, manufacturer, device type, device
role, VM role/type, custom fields, tags, choice sets) and reconciles each
against the live NetBox via the typed ``upsert_*`` helpers in
:mod:`proxbox_api.services.netbox_writers`.

Each upsert ``find by lookup, diff declared fields, PATCH only on diff,
create only on miss`` so a fresh NetBox install converges in one pass and
subsequent restarts are a no-op. Behaviour is gated by the
``ensure_netbox_objects`` plugin flag; when disabled, the orchestrator
records ``skipped=True`` and the existing inline ``_ensure_*`` helpers in
the sync path still attempt creation defensively.

Declared-field surface
----------------------

The orchestrator only diffs the keys it sets. Operator-curated fields not
present in the declared payload (e.g. cluster ``description`` on a record
the operator hand-edited but the orchestrator does not write) survive
bootstrap unchanged. The exceptions are fields the orchestrator
intentionally manages — those are listed inside each call's payload.

Choice sets are scaffolded but not populated; concrete inventories are
deferred to the roadmap consumers (#5 cloud-init, #6 discovery tags, #8
role pinning, #11 last-run UUID).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from proxbox_api.constants import (
    DISCOVERY_TAG_CLUSTER,
    DISCOVERY_TAG_NODE,
    DISCOVERY_TAG_VM_LXC,
    DISCOVERY_TAG_VM_QEMU,
    VM_ROLE_MAPPINGS,
    VM_TYPE_MAPPINGS,
)
from proxbox_api.logger import logger
from proxbox_api.netbox_version import (
    detect_netbox_version,
    supports_virtual_machine_type,
)
from proxbox_api.services.netbox_writers import (
    UpsertResult,
    upsert_cluster_type,
    upsert_custom_field,
    upsert_device_role,
    upsert_device_type,
    upsert_manufacturer,
    upsert_tag,
    upsert_vm_role,
    upsert_vm_type,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@dataclass
class BootstrapStatus:
    """Outcome of a single bootstrap run.

    The ``bootstrap_done`` SSE frame echoes this dataclass's public fields
    so the plugin UI can render a per-object summary without a second
    round-trip.
    """

    ok: bool = True
    skipped: bool = False
    reason: str | None = None
    warnings: list[dict[str, str]] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    patched: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "skipped": self.skipped,
            "reason": self.reason,
            "warnings": list(self.warnings),
            "created": list(self.created),
            "patched": list(self.patched),
            "unchanged": list(self.unchanged),
        }


_PROXBOX_TAG_NAME = "Proxbox"
_PROXBOX_TAG_SLUG = "proxbox"
_PROXBOX_TAG_COLOR = "ff5722"
_PROXBOX_TAG_DESCRIPTION = "Proxbox Identifier (used to identify the items the plugin created)"

# First-discovery audit tags (issue #362). Stamped onto an object only when
# the reconciler takes the create branch; never re-attached on update. Operator
# removal is permanent. Per-type QEMU/LXC split lets a saved-search filter
# return exactly the VMs Proxbox first imported of that type.
_DISCOVERY_TAGS: tuple[dict[str, str], ...] = (
    {
        "name": "Proxbox: Discovered QEMU VM",
        "slug": DISCOVERY_TAG_VM_QEMU,
        "color": "2196f3",
        "description": "QEMU virtual machine first discovered by Proxbox (apply-on-create-only).",
    },
    {
        "name": "Proxbox: Discovered LXC Container",
        "slug": DISCOVERY_TAG_VM_LXC,
        "color": "00bcd4",
        "description": "LXC container first discovered by Proxbox (apply-on-create-only).",
    },
    {
        "name": "Proxbox: Discovered Cluster",
        "slug": DISCOVERY_TAG_CLUSTER,
        "color": "4caf50",
        "description": "Cluster first discovered by Proxbox (apply-on-create-only).",
    },
    {
        "name": "Proxbox: Discovered Node",
        "slug": DISCOVERY_TAG_NODE,
        "color": "9c27b0",
        "description": "Proxmox node first discovered by Proxbox (apply-on-create-only).",
    },
)


def _last_run_id_custom_field() -> dict[str, object]:
    """Custom field that pins a sync run's UUID onto its written objects.

    Required by roadmap items #5 (cloud-init), #6 (discovery tags), #8
    (role pinning), and #11 (last-run UUID). The orchestrator writes the
    field; the sync flow stamps it per run.
    """
    return {
        "object_types": [
            "dcim.device",
            "virtualization.cluster",
            "virtualization.virtualmachine",
        ],
        "type": "text",
        "name": "proxbox_last_run_id",
        "label": "Last Run ID",
        "description": "UUID of the most recent Proxbox sync run that touched this object.",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 250,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxbox",
    }


def _custom_field_inventory() -> list[dict[str, object]]:
    """Hardcoded custom-field inventory mirroring ``create_custom_fields``.

    The orchestrator owns the declarative inventory now; the legacy
    ``routes/extras/__init__.py::create_custom_fields`` endpoint still
    exists as a defensive lazy fallback (and shares the same payload
    surface).
    """
    return [
        {
            "object_types": ["virtualization.virtualmachine"],
            "type": "integer",
            "name": "proxmox_vm_id",
            "label": "VM ID",
            "description": "Proxmox Virtual Machine or Container ID",
            "ui_visible": "always",
            "weight": 100,
            "group_name": "Proxmox",
        },
        {
            "object_types": ["virtualization.virtualmachine"],
            "type": "text",
            "name": "proxmox_vm_type",
            "label": "VM Type",
            "description": "Proxmox VM type (qemu or lxc)",
            "ui_visible": "always",
            "weight": 100,
            "group_name": "Proxmox",
        },
        {
            "object_types": ["virtualization.virtualmachine"],
            "type": "boolean",
            "name": "proxmox_start_at_boot",
            "label": "Start at Boot",
            "description": "Proxmox Start at Boot Option",
            "ui_visible": "always",
            "weight": 100,
            "group_name": "Proxmox",
        },
        {
            "object_types": ["virtualization.virtualmachine"],
            "type": "boolean",
            "name": "proxmox_unprivileged_container",
            "label": "Unprivileged Container",
            "description": "Proxmox Unprivileged Container",
            "ui_visible": "if-set",
            "weight": 100,
            "group_name": "Proxmox",
        },
        {
            "object_types": ["virtualization.virtualmachine"],
            "type": "boolean",
            "name": "proxmox_qemu_agent",
            "label": "QEMU Guest Agent",
            "description": "Proxmox QEMU Guest Agent",
            "ui_visible": "if-set",
            "weight": 100,
            "group_name": "Proxmox",
        },
        {
            "object_types": ["virtualization.vminterface"],
            "type": "object",
            "name": "proxbox_bridge",
            "label": "Proxbox Bridge",
            "related_object_type": "dcim.interface",
            "description": "Node-level bridge interface (vmbr) used by this VM interface",
            "ui_visible": "always",
            "weight": 100,
            "group_name": "Proxmox",
        },
        {
            "object_types": [
                "dcim.device",
                "dcim.devicerole",
                "dcim.devicetype",
                "dcim.interface",
                "dcim.manufacturer",
                "dcim.site",
                "ipam.ipaddress",
                "ipam.vlan",
                "virtualization.cluster",
                "virtualization.clustertype",
                "virtualization.virtualdisk",
                "virtualization.virtualmachine",
                "virtualization.vminterface",
            ],
            "type": "datetime",
            "name": "proxmox_last_updated",
            "label": "Last Updated",
            "description": "Proxmox Plugin last modified this object",
            "ui_visible": "always",
            "weight": 200,
            "group_name": "Proxmox",
        },
        _last_run_id_custom_field(),
    ]


async def run_netbox_bootstrap(
    nb: object,
    *,
    enabled: bool = True,
) -> BootstrapStatus:
    """Run the NetBox-side bootstrap pass and return a status snapshot.

    Parameters
    ----------
    nb:
        A live NetBox async session (the same kind ``NetBoxAsyncSessionDep``
        provides). Caller is responsible for wiring this in lifespan code.
    enabled:
        When ``False``, the orchestrator records ``skipped=True`` and
        returns immediately without touching NetBox. The lifespan resolver
        passes ``ensure_netbox_objects`` here.

    The orchestrator never raises on per-entry failure — each upsert is
    captured into ``BootstrapStatus.warnings`` and the run continues.
    Connectivity-level failures (e.g. NetBox unreachable) are caught at
    the caller in ``app/factory.py::_lifespan``.
    """
    status = BootstrapStatus()

    if not enabled:
        status.skipped = True
        status.reason = "ensure_netbox_objects=false"
        logger.info("NetBox bootstrap skipped: ensure_netbox_objects=false")
        return status

    proxbox_tag = await _safe_upsert(
        status,
        "tag:Proxbox",
        lambda: upsert_tag(
            nb,
            name=_PROXBOX_TAG_NAME,
            slug=_PROXBOX_TAG_SLUG,
            color=_PROXBOX_TAG_COLOR,
            description=_PROXBOX_TAG_DESCRIPTION,
        ),
    )
    # The schema layer diffs tag refs by ``slug`` (name + slug + color), so we
    # ship the full tag payload here even though NetBox accepts ``{"id": ...}``.
    # Using the slug form keeps the desired-vs-current diff stable and works
    # whether or not the tag upsert returned a usable record.
    tag_refs: list[dict[str, object]] = []
    if proxbox_tag is not None:
        tag_refs = [
            {
                "name": _PROXBOX_TAG_NAME,
                "slug": _PROXBOX_TAG_SLUG,
                "color": _PROXBOX_TAG_COLOR,
            }
        ]

    for tag in _DISCOVERY_TAGS:
        await _safe_upsert(
            status,
            f"tag:{tag['slug']}",
            lambda tag=tag: upsert_tag(
                nb,
                name=tag["name"],
                slug=tag["slug"],
                color=tag["color"],
                description=tag["description"],
            ),
        )

    await _safe_upsert(
        status,
        "cluster_type:proxmox",
        lambda: upsert_cluster_type(nb, mode="cluster", tag_refs=tag_refs),
    )

    manufacturer = await _safe_upsert(
        status,
        "manufacturer:proxmox",
        lambda: upsert_manufacturer(
            nb,
            name="Proxmox",
            slug="proxmox",
            tag_refs=tag_refs,
        ),
    )
    manufacturer_id = _record_id(manufacturer.record) if manufacturer is not None else None

    await _safe_upsert(
        status,
        "device_type:proxmox-generic-device",
        lambda: upsert_device_type(
            nb,
            model="Proxmox Generic Device",
            slug="proxmox-generic-device",
            manufacturer_id=manufacturer_id,
            tag_refs=tag_refs,
        ),
    )

    await _safe_upsert(
        status,
        "device_role:proxmox-node",
        lambda: upsert_device_role(
            nb,
            name="Proxmox Node",
            slug="proxmox-node",
            color="00bcd4",
            description="Proxmox node",
            tag_refs=tag_refs,
        ),
    )

    for kind, role in VM_ROLE_MAPPINGS.items():
        await _safe_upsert(
            status,
            f"vm_role:{role['slug']}",
            lambda role=role: upsert_vm_role(
                nb,
                name=str(role["name"]),
                slug=str(role["slug"]),
                color=str(role["color"]),
                description=str(role.get("description", "")) or None,
                tag_refs=tag_refs,
            ),
        )
        del kind

    netbox_version = await detect_netbox_version(nb)
    if supports_virtual_machine_type(netbox_version):
        for kind, vm_type in VM_TYPE_MAPPINGS.items():
            await _safe_upsert(
                status,
                f"vm_type:{vm_type['slug']}",
                lambda vm_type=vm_type: upsert_vm_type(
                    nb,
                    name=vm_type["name"],
                    slug=vm_type["slug"],
                    description=vm_type.get("description"),
                    tag_refs=tag_refs,
                ),
            )
            del kind
    else:
        logger.info(
            "Skipping VirtualMachineType bootstrap: live NetBox %s does not support it.",
            ".".join(str(part) for part in netbox_version),
        )

    for cf in _custom_field_inventory():
        await _safe_upsert(
            status,
            f"custom_field:{cf['name']}",
            lambda cf=cf: upsert_custom_field(
                nb,
                name=str(cf["name"]),
                type=str(cf["type"]),
                label=str(cf["label"]),
                object_types=list(cf["object_types"]),  # type: ignore[arg-type]
                description=cf.get("description"),  # type: ignore[arg-type]
                group_name=cf.get("group_name"),  # type: ignore[arg-type]
                ui_visible=str(cf.get("ui_visible", "always")),
                ui_editable=str(cf.get("ui_editable", "hidden")),
                weight=int(cf.get("weight", 100)),  # type: ignore[arg-type]
                filter_logic=str(cf.get("filter_logic", "loose")),
                search_weight=int(cf.get("search_weight", 1000)),  # type: ignore[arg-type]
                related_object_type=cf.get("related_object_type"),  # type: ignore[arg-type]
            ),
        )

    if status.warnings:
        status.ok = False

    logger.info(
        "NetBox bootstrap finished: created=%d patched=%d unchanged=%d warnings=%d",
        len(status.created),
        len(status.patched),
        len(status.unchanged),
        len(status.warnings),
    )
    return status


async def _safe_upsert(
    status: BootstrapStatus,
    label: str,
    call: Callable[[], Awaitable[UpsertResult]],
) -> UpsertResult | None:
    """Run a single upsert, capturing failures into ``status.warnings``.

    Returns the :class:`UpsertResult` on success so the caller can chain
    dependent IDs (manufacturer → device type, Proxbox tag → tag refs).
    """
    try:
        result = await call()
    except Exception as exc:  # noqa: BLE001 — orchestrator must not abort the run
        logger.warning("NetBox bootstrap failed for %s: %s", label, exc)
        status.warnings.append({"object": label, "error": str(exc)})
        return None

    if result.status == "created":
        status.created.append(label)
    elif result.status == "updated":
        status.patched.append(label)
    else:
        status.unchanged.append(label)
    return result


def _record_id(record: object) -> int | None:
    """Return the numeric ID of a netbox-sdk record, if available."""
    try:
        serialized = record.serialize()  # type: ignore[attr-defined]
    except AttributeError:
        serialized = None

    if isinstance(serialized, dict):
        candidate = serialized.get("id")
        if isinstance(candidate, int):
            return candidate

    candidate = getattr(record, "id", None)
    if isinstance(candidate, int):
        return candidate
    return None
