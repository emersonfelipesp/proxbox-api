"""Hierarchical default-role resolution and per-VM snapshot lock helpers.

Issue #364: synced Proxmox VMs/containers pick a NetBox ``DeviceRole``
from a four-tier hierarchy — operator-edit lock → per-Node → per-Endpoint →
plugin singleton — and a hidden ``proxmox_last_synced_role_id`` custom field
on each VM tracks the last value Proxbox wrote so direct operator edits in
the NetBox UI are preserved on subsequent syncs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_first_async
from proxbox_api.services.sync.vm_helpers import relation_id as _relation_id
from proxbox_api.settings_client import get_settings

LAST_SYNCED_ROLE_CUSTOM_FIELD = "proxmox_last_synced_role_id"

VMType = Literal["qemu", "lxc"]


def _coerce_int(value: object) -> int | None:
    rid = _relation_id(value)
    if rid is not None:
        return rid
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


async def _resolve_from_node_and_endpoint(
    nb: object,
    *,
    field_name: str,
    node_name: str,
    cluster_id: int | None,
) -> int | None:
    """Return the node- or endpoint-tier default role id, or ``None``."""
    query: dict[str, object] = {"name": node_name}
    if cluster_id is not None:
        query["proxmox_cluster_id"] = cluster_id
    try:
        node_record = await rest_first_async(nb, "/api/plugins/proxbox/proxmox-nodes/", query=query)
    except Exception as exc:
        logger.debug(
            "default-role node lookup failed for %s (cluster=%s): %s",
            node_name,
            cluster_id,
            exc,
        )
        return None

    if node_record is None:
        return None

    role_id = _coerce_int(node_record.get(field_name))
    if role_id is not None:
        return role_id

    endpoint_id = _relation_id(node_record.get("endpoint"))
    if endpoint_id is None:
        return None
    try:
        endpoint_record = await rest_first_async(
            nb,
            "/api/plugins/proxbox/endpoints/proxmox/",
            query={"id": endpoint_id},
        )
    except Exception as exc:
        logger.debug(
            "default-role endpoint lookup failed for endpoint=%s: %s",
            endpoint_id,
            exc,
        )
        return None
    if endpoint_record is None:
        return None
    return _coerce_int(endpoint_record.get(field_name))


async def resolve_default_role_id(
    nb: object,
    *,
    vm_type: str,
    node_name: str | None,
    cluster_id: int | None,
) -> int | None:
    """Resolve the default ``DeviceRole.id`` for a VM via the four-tier hierarchy.

    Walks per-Node → per-Endpoint → plugin singleton, returning the first
    non-null FK. Returns ``None`` when no tier provides a default — callers
    leave ``role`` unset in that case.
    """
    if vm_type not in {"qemu", "lxc"}:
        return None
    field_name = f"default_role_{vm_type}"

    if node_name:
        role_id = await _resolve_from_node_and_endpoint(
            nb,
            field_name=field_name,
            node_name=node_name,
            cluster_id=cluster_id,
        )
        if role_id is not None:
            return role_id

    try:
        settings = get_settings(nb)
    except Exception as exc:
        logger.debug("default-role plugin settings lookup failed: %s", exc)
        return None
    return _coerce_int(settings.get(f"default_role_{vm_type}_id"))


@dataclass(frozen=True, slots=True)
class RoleSnapshotDecision:
    """Resolved role + snapshot writes for a single VM reconciliation.

    Attributes mirror the two payload writes the sync must produce: ``role``
    on the VM and ``custom_fields.proxmox_last_synced_role_id``. ``None`` for
    a ``*_value`` means the field would be cleared in NetBox; the matching
    ``write_*`` flag is the source of truth for "include this field in the
    PATCH at all".
    """

    role_value: int | None
    snapshot_value: int | None
    write_role: bool
    write_snapshot: bool


def compute_role_snapshot_decision(
    *,
    existing_role_id: int | None,
    existing_snapshot_id: int | None,
    desired_role_id: int | None,
    overwrite_vm_role: bool,
) -> RoleSnapshotDecision:
    """Decide what the next sync should write for ``role`` and the snapshot.

    Encodes the nine-case matrix from the issue #364 plan:

    * fresh creates (handled by ``initial_create_decision``) always write
      both fields together
    * upgrade backfill — snapshot is ``None`` on an existing VM — captures
      operator intent: if the VM already has a role, treat it as operator
      intent and only write the snapshot; if the VM has no role, treat as a
      fresh-create-style apply
    * normal updates roll snapshot forward when role rolls forward
    * operator edits (``existing_role_id != snapshot_id``) lock both fields
      until ``overwrite_vm_role=True`` releases them
    """
    if existing_snapshot_id is None:
        if existing_role_id is not None:
            return RoleSnapshotDecision(
                role_value=existing_role_id,
                snapshot_value=existing_role_id,
                write_role=False,
                write_snapshot=True,
            )
        return RoleSnapshotDecision(
            role_value=desired_role_id,
            snapshot_value=desired_role_id,
            write_role=desired_role_id is not None,
            write_snapshot=desired_role_id is not None,
        )

    if existing_role_id != existing_snapshot_id:
        if overwrite_vm_role:
            return RoleSnapshotDecision(
                role_value=desired_role_id,
                snapshot_value=desired_role_id,
                write_role=desired_role_id is not None,
                write_snapshot=desired_role_id is not None,
            )
        return RoleSnapshotDecision(
            role_value=existing_role_id,
            snapshot_value=existing_snapshot_id,
            write_role=False,
            write_snapshot=False,
        )

    if existing_role_id != desired_role_id and desired_role_id is not None:
        return RoleSnapshotDecision(
            role_value=desired_role_id,
            snapshot_value=desired_role_id,
            write_role=True,
            write_snapshot=True,
        )

    return RoleSnapshotDecision(
        role_value=existing_role_id,
        snapshot_value=existing_snapshot_id,
        write_role=False,
        write_snapshot=False,
    )


def extract_snapshot_id(record: dict[str, object] | None) -> int | None:
    """Pull ``proxmox_last_synced_role_id`` out of a NetBox VM record."""
    if not isinstance(record, dict):
        return None
    custom_fields = record.get("custom_fields")
    if not isinstance(custom_fields, dict):
        return None
    return _coerce_int(custom_fields.get(LAST_SYNCED_ROLE_CUSTOM_FIELD))
