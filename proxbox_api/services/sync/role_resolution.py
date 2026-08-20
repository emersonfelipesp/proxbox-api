"""Hierarchical default-role resolution and per-VM snapshot lock helpers.

Synced Proxmox VMs/containers pick a NetBox ``DeviceRole`` from a four-tier
hierarchy — operator-edit lock → per-Node → per-Endpoint → plugin singleton.
The typed VM sync-state sidecar stores ``proxmox_last_synced_role_id`` so direct
operator edits in the NetBox UI remain preserved on subsequent syncs. The
deprecated same-named custom field is only a transition fallback.
"""

from __future__ import annotations

from collections.abc import Awaitable, Collection, Mapping
from dataclasses import dataclass
from typing import Literal

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    clear_rest_get_cache_for_path,
    rest_first_async,
    rest_patch_async,
)
from proxbox_api.services.sync.sync_state_reader import (
    VMRoleSnapshotRead,
    VMRoleSnapshotScan,
    read_vm_last_synced_role,
    reset_sidecar_reader_availability_cache,
)
from proxbox_api.services.sync.sync_state_writer import (
    VM_SYNC_STATE_PATH,
    write_vm_role_snapshot_exact,
)
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

    * fresh creates always write both fields together
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


async def resolve_snapshot_id(nb: object, record: dict[str, object] | None) -> int | None:
    """Compatibility wrapper returning a verified role snapshot id, if any."""
    return (await resolve_snapshot_read(nb, record)).snapshot_id


async def resolve_snapshot_read(
    nb: object,
    record: dict[str, object] | None,
) -> VMRoleSnapshotRead:
    """Resolve a role snapshot without collapsing read failure into absence."""
    return await read_vm_last_synced_role(
        nb,
        vm_record=record,
        custom_field_name=LAST_SYNCED_ROLE_CUSTOM_FIELD,
    )


def resolve_snapshot_read_from_scan(
    scan: VMRoleSnapshotScan,
    record: Mapping[str, object] | None,
    *,
    legacy_custom_fields_enabled: bool,
) -> VMRoleSnapshotRead:
    """Resolve one VM from a fleet scan, with safe legacy fallback."""
    if record is None:
        return VMRoleSnapshotRead(snapshot_id=None, verified=True)
    vm_id = _relation_id(record.get("id"))
    read = scan.for_vm(vm_id)
    if read.snapshot_id is not None:
        return read
    if (
        legacy_custom_fields_enabled
        and scan.read_verified
        and vm_id not in scan.unverified_vm_ids
        and (legacy_snapshot_id := extract_snapshot_id(dict(record))) is not None
    ):
        return VMRoleSnapshotRead(snapshot_id=legacy_snapshot_id, verified=True)
    return read


def apply_role_snapshot_policy(
    *,
    existing_record: Mapping[str, object] | None,
    existing_snapshot_id: int | None,
    desired_payload: Mapping[str, object],
    patchable_fields: Collection[str],
    overwrite_vm_role: bool,
    snapshot_read_verified: bool = True,
) -> tuple[dict[str, object], frozenset[str], RoleSnapshotDecision]:
    """Apply the role ownership decision to one reconcile payload.

    The returned payload and allowlist are safe for ``rest_reconcile_async``.
    On an existing VM, omitting ``role`` from the allowlist is the hard fence
    that preserves an operator edit. Fresh creates still receive the resolved
    desired role. Snapshot persistence is intentionally left to the caller so
    it can occur only after the NetBox write succeeds.
    """

    current = dict(existing_record) if existing_record is not None else None
    existing_role_id = _relation_id(current.get("role")) if current is not None else None
    desired_role_id = _relation_id(desired_payload.get("role"))
    if current is not None and not snapshot_read_verified:
        decision = RoleSnapshotDecision(
            role_value=existing_role_id,
            snapshot_value=existing_snapshot_id,
            write_role=False,
            write_snapshot=False,
        )
    else:
        decision = compute_role_snapshot_decision(
            existing_role_id=existing_role_id,
            existing_snapshot_id=existing_snapshot_id,
            desired_role_id=desired_role_id,
            overwrite_vm_role=overwrite_vm_role,
        )

    payload = dict(desired_payload)
    fields = set(patchable_fields)
    if decision.write_role:
        payload["role"] = decision.role_value
        fields.add("role")
    elif current is not None:
        fields.discard("role")

    return payload, frozenset(fields), decision


def _mapping_value(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return value
    for method_name in ("serialize", "dict", "model_dump"):
        method = getattr(value, method_name, None)
        if not callable(method):
            continue
        try:
            serialized = method()
        except Exception:  # noqa: BLE001 - defensive against SDK proxy objects
            continue
        if isinstance(serialized, Mapping):
            return serialized
    return None


def _role_matches(record: object, expected_role_id: int | None) -> bool:
    mapping = _mapping_value(record)
    if mapping is None or "role" not in mapping:
        return False
    return _relation_id(mapping.get("role")) == expected_role_id


async def compensate_failed_role_snapshot(
    nb: object,
    *,
    virtual_machine_id: int,
    previous_role_id: int | None,
    attempts: int = 3,
) -> None:
    """Restore and verify the prior role after ownership persistence fails."""
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            patched = await rest_patch_async(
                nb,
                "/api/virtualization/virtual-machines/",
                virtual_machine_id,
                {"role": previous_role_id},
            )
            if _role_matches(patched, previous_role_id):
                return
            current = await rest_first_async(
                nb,
                "/api/virtualization/virtual-machines/",
                query={"id": virtual_machine_id, "limit": 2},
            )
            if current is not None and _role_matches(current, previous_role_id):
                return
            last_error = RuntimeError("NetBox did not confirm the compensated VM role")
        except Exception as exc:  # noqa: BLE001 - retry a correctness-critical rollback
            last_error = exc
        logger.error(
            "VM role compensation attempt %s/%s failed for virtual_machine_id=%s: %s",
            attempt,
            max(1, attempts),
            virtual_machine_id,
            last_error,
        )
    raise RuntimeError(
        f"Could not restore VM {virtual_machine_id} role after snapshot failure"
    ) from last_error


async def persist_sync_state_with_role_compensation(
    nb: object,
    *,
    persistence: Awaitable[None],
    virtual_machine_id: int | None,
    previous_role_id: int | None,
    previous_snapshot_id: int | None,
    expected_snapshot_id: int | None,
    role_write_applied: bool,
) -> None:
    """Persist ownership, confirming a lost response or restoring the prior pair."""
    try:
        await persistence
    except Exception as snapshot_error:
        if not role_write_applied:
            raise
        if virtual_machine_id is None:
            raise RuntimeError(
                "Cannot compensate VM role after snapshot failure without a NetBox id"
            ) from snapshot_error
        try:
            clear_rest_get_cache_for_path(nb, VM_SYNC_STATE_PATH)
        except Exception:  # noqa: BLE001 - authoritative read remains the deciding check
            pass
        reset_sidecar_reader_availability_cache()
        committed_snapshot = await read_vm_last_synced_role(
            nb,
            vm_record={"id": virtual_machine_id},
            custom_field_name=LAST_SYNCED_ROLE_CUSTOM_FIELD,
        )
        if (
            expected_snapshot_id is not None
            and committed_snapshot.verified
            and committed_snapshot.snapshot_id == expected_snapshot_id
        ):
            logger.warning(
                "Confirmed VM role ownership snapshot after a lost write response: "
                "virtual_machine_id=%s role_id=%s",
                virtual_machine_id,
                expected_snapshot_id,
            )
            return
        try:
            await write_vm_role_snapshot_exact(
                nb,
                virtual_machine_id=virtual_machine_id,
                snapshot_id=previous_snapshot_id,
            )
            await compensate_failed_role_snapshot(
                nb,
                virtual_machine_id=virtual_machine_id,
                previous_role_id=previous_role_id,
            )
        except Exception as compensation_error:
            logger.critical(
                "VM role/snapshot compensation failed after ownership persistence error: "
                "virtual_machine_id=%s snapshot_error=%s compensation_error=%s",
                virtual_machine_id,
                snapshot_error,
                compensation_error,
            )
            raise RuntimeError(
                f"VM {virtual_machine_id} ownership persistence and compensation failed"
            ) from compensation_error
        logger.warning(
            "Restored prior VM role and ownership snapshot after persistence failure: "
            "virtual_machine_id=%s role_id=%s snapshot_id=%s",
            virtual_machine_id,
            previous_role_id,
            previous_snapshot_id,
        )
        raise
