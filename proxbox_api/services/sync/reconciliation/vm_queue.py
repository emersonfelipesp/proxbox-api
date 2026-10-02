"""Pure VM operation-queue reconciliation."""

from __future__ import annotations

import difflib
import json
import logging
from typing import Any, Literal

from proxbox_api.proxmox_to_netbox.models import NetBoxVirtualMachineCreateBody
from proxbox_api.runtime_settings import get_plugin_bool, get_plugin_str
from proxbox_api.services.sync.reconciliation.metrics import (
    increment_reconciliation_mismatch_total,
)
from proxbox_api.services.sync.reconciliation.rust_bridge import (
    build_vm_operation_queue_rust,
    rust_available,
)
from proxbox_api.services.sync.reconciliation.types import NetBoxVMOperation, PreparedVMState
from proxbox_api.services.sync.vm_cluster_guard import (
    filter_vm_records_in_cluster,
    log_cross_cluster_rejection,
    log_unverifiable_vm_skip,
    vm_cluster_verdict,
    vm_record_cluster_label,
    vm_record_in_cluster,
)
from proxbox_api.services.sync.vm_helpers import (
    normalize_current_virtual_machine_payload,
)
from proxbox_api.services.sync.vm_helpers import (
    relation_id as _relation_id,
)
from proxbox_api.services.sync.vmid_helpers import (
    extract_proxmox_endpoint_id,
    extract_proxmox_vmid,
)

logger = logging.getLogger(__name__)

_VALID_ENGINES = {"python", "compare", "rust"}
_MAX_DIFF_CHARS = 12000
_TypedSnapshotIndex = dict[tuple[int, int, str], dict[str, object]]
_UntypedSnapshotIndex = dict[tuple[int, int], list[dict[str, object]]]


class RustOperationAdaptationError(RuntimeError):
    """Raised when a raw Rust operation cannot be mapped back to prepared state."""


def normalize_current_vm_payload(
    record: dict[str, object],
    *,
    supports_virtual_machine_type_field: bool = True,
) -> dict[str, object]:
    """Normalize NetBox VM record for Pydantic diff comparison."""

    return normalize_current_virtual_machine_payload(
        record,
        supports_virtual_machine_type_field=supports_virtual_machine_type_field,
    )


def extract_cluster_and_proxmox_vmid(record: dict[str, object]) -> tuple[int, int] | None:
    """Build the legacy cluster-scoped index key used when endpoint identity is absent."""

    cluster_id = _relation_id(record.get("cluster"))
    if cluster_id is None:
        return None
    raw_vmid = extract_proxmox_vmid(record)
    try:
        proxmox_vmid = int(str(raw_vmid).strip())
    except (TypeError, ValueError):
        return None
    return (cluster_id, proxmox_vmid)


def extract_endpoint_and_proxmox_vmid(record: dict[str, object]) -> tuple[int, int] | None:
    """Build the endpoint-scoped index key used to correlate NetBox VM records."""

    endpoint_id = extract_proxmox_endpoint_id(record)
    if endpoint_id is None:
        return None
    raw_vmid = extract_proxmox_vmid(record)
    try:
        proxmox_vmid = int(str(raw_vmid).strip())
    except (TypeError, ValueError):
        return None
    return (endpoint_id, proxmox_vmid)


def normalize_proxmox_vm_type(value: object) -> str | None:
    """Normalize Proxmox VM type values used in snapshot identity keys."""

    if isinstance(value, dict):
        for key in ("value", "slug", "name", "label"):
            candidate = value.get(key)
            if candidate:
                value = candidate
                break
        else:
            value = None
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized or None


def extract_proxmox_vm_type(record: dict[str, object]) -> str | None:
    """Return the stored Proxmox VM type from hydrated typed sync state."""

    return normalize_proxmox_vm_type(record.get("proxmox_vm_type"))


def build_vm_snapshot_identity_indexes(
    snapshot: list[dict[str, object]],
) -> tuple[
    _TypedSnapshotIndex,
    _UntypedSnapshotIndex,
    _TypedSnapshotIndex,
    _UntypedSnapshotIndex,
]:
    """Index NetBox VM records by endpoint and cluster identity."""

    endpoint_typed_index: _TypedSnapshotIndex = {}
    endpoint_untyped_candidates: _UntypedSnapshotIndex = {}
    cluster_typed_index: _TypedSnapshotIndex = {}
    cluster_untyped_candidates: _UntypedSnapshotIndex = {}
    for current in snapshot:
        vm_type = extract_proxmox_vm_type(current)
        endpoint_key = extract_endpoint_and_proxmox_vmid(current)
        if endpoint_key is not None:
            endpoint_untyped_candidates.setdefault(endpoint_key, []).append(current)
            if vm_type is not None:
                endpoint_typed_index.setdefault(
                    (endpoint_key[0], endpoint_key[1], vm_type), current
                )

        cluster_key = extract_cluster_and_proxmox_vmid(current)
        if cluster_key is not None:
            cluster_untyped_candidates.setdefault(cluster_key, []).append(current)
            if vm_type is not None:
                cluster_typed_index.setdefault((cluster_key[0], cluster_key[1], vm_type), current)
    return (
        endpoint_typed_index,
        endpoint_untyped_candidates,
        cluster_typed_index,
        cluster_untyped_candidates,
    )


def _select_scoped_vm_record(
    *,
    prepared: PreparedVMState,
    endpoint_id: int | None,
    cluster_id: int | None,
    proxmox_vmid: int | None,
    endpoint_typed_index: _TypedSnapshotIndex,
    endpoint_untyped_candidates: _UntypedSnapshotIndex,
    cluster_typed_index: _TypedSnapshotIndex,
    cluster_untyped_candidates: _UntypedSnapshotIndex,
) -> dict[str, object] | None:
    """Look up a VM by endpoint (or cluster) scope without checking its cluster."""

    if proxmox_vmid is None:
        return None
    if endpoint_id is not None:
        scope_id = endpoint_id
        typed_index = endpoint_typed_index
        untyped_candidates = endpoint_untyped_candidates
    elif cluster_id is not None:
        scope_id = cluster_id
        typed_index = cluster_typed_index
        untyped_candidates = cluster_untyped_candidates
    else:
        return None

    prepared_vm_type = normalize_proxmox_vm_type(prepared.vm_type)
    untyped_key = (scope_id, proxmox_vmid)
    if prepared_vm_type is not None:
        exact_record = typed_index.get((scope_id, proxmox_vmid, prepared_vm_type))
        if exact_record is not None:
            return exact_record

        candidates = untyped_candidates.get(untyped_key, [])
        if len(candidates) == 1 and extract_proxmox_vm_type(candidates[0]) is None:
            return candidates[0]
        return None

    candidates = untyped_candidates.get(untyped_key, [])
    if len(candidates) == 1:
        return candidates[0]
    return None


def _select_cluster_scoped_endpoint_record(
    *,
    prepared: PreparedVMState,
    endpoint_id: int,
    cluster_id: int | None,
    proxmox_vmid: int,
    endpoint_untyped_candidates: _UntypedSnapshotIndex,
) -> dict[str, object] | None:
    """Pick the endpoint-keyed candidate that lives in the cluster being synced.

    The endpoint indexes keep only the first record per key, so when two
    clusters expose the same ``(endpoint id, vmid)`` pair the first one can
    shadow the record that actually belongs to the live cluster. Scan the full
    candidate list restricted to the live cluster instead.
    """

    candidates = filter_vm_records_in_cluster(
        endpoint_untyped_candidates.get((endpoint_id, proxmox_vmid), []),
        cluster_id=cluster_id,
        cluster_name=prepared.cluster_name,
    )
    prepared_vm_type = normalize_proxmox_vm_type(prepared.vm_type)
    if prepared_vm_type is None:
        return candidates[0] if len(candidates) == 1 else None
    typed = [c for c in candidates if extract_proxmox_vm_type(c) == prepared_vm_type]
    if len(typed) == 1:
        return typed[0]
    untyped = [c for c in candidates if extract_proxmox_vm_type(c) is None]
    return untyped[0] if len(candidates) == 1 and len(untyped) == 1 else None


def _select_verified_cluster_record(
    *,
    prepared: PreparedVMState,
    cluster_id: int | None,
    proxmox_vmid: int,
    cluster_typed_index: _TypedSnapshotIndex,
    cluster_untyped_candidates: _UntypedSnapshotIndex,
) -> dict[str, object] | None:
    """Pick the cluster-keyed record for the live cluster, only when it is verified.

    A record that already names a different Proxmox endpoint belongs to that
    endpoint's VM and is never adopted here.
    """
    if cluster_id is None:
        return None
    record = _select_scoped_vm_record(
        prepared=prepared,
        endpoint_id=None,
        cluster_id=cluster_id,
        proxmox_vmid=proxmox_vmid,
        endpoint_typed_index={},
        endpoint_untyped_candidates={},
        cluster_typed_index=cluster_typed_index,
        cluster_untyped_candidates=cluster_untyped_candidates,
    )
    if record is None or not vm_record_in_cluster(
        record, cluster_id=cluster_id, cluster_name=prepared.cluster_name
    ):
        return None
    record_endpoint = extract_proxmox_endpoint_id(record)
    prepared_endpoint = extract_proxmox_endpoint_id(prepared.sync_state_fields)
    if record_endpoint is not None and record_endpoint != prepared_endpoint:
        return None
    return record


def _unverifiable_endpoint_candidate(
    prepared: PreparedVMState,
    *,
    endpoint_untyped_candidates: _UntypedSnapshotIndex,
    cluster_typed_index: _TypedSnapshotIndex,
    cluster_untyped_candidates: _UntypedSnapshotIndex,
) -> dict[str, object] | None:
    """Return the endpoint-keyed record that blocks a write because its cluster is unknown.

    A prepared VM is blocked only when the live cluster is known, the
    endpoint-keyed candidates hold no verified ``match`` for it and the
    selector cannot resolve a verified cluster-keyed record for
    ``(cluster id, vmid)`` (the same fallback ``select_existing_vm_record``
    uses), yet at least one
    candidate is ``unknown`` or ``unassigned`` (explicit ``null`` cluster): that
    row could be the live cluster's own VM, so creating would duplicate it and
    updating could let a colliding cluster adopt or rewrite another cluster's VM.
    """
    endpoint_id = extract_proxmox_endpoint_id(prepared.sync_state_fields)
    vmid = _relation_id(prepared.resource.get("vmid"))
    cluster_id = _relation_id(prepared.desired_payload.get("cluster"))
    if endpoint_id is None or vmid is None:
        return None
    if (
        _select_verified_cluster_record(
            prepared=prepared,
            cluster_id=cluster_id,
            proxmox_vmid=vmid,
            cluster_typed_index=cluster_typed_index,
            cluster_untyped_candidates=cluster_untyped_candidates,
        )
        is not None
    ):
        return None
    verdicts = [
        (
            record,
            vm_cluster_verdict(record, cluster_id=cluster_id, cluster_name=prepared.cluster_name),
        )
        for record in endpoint_untyped_candidates.get((endpoint_id, vmid), [])
    ]
    if any(verdict == "match" for _, verdict in verdicts):
        return None
    return next((record for record, verdict in verdicts if verdict != "mismatch"), None)


def _partition_unverifiable_vm_candidates(
    prepared_vms: list[PreparedVMState],
    *,
    endpoint_untyped_candidates: _UntypedSnapshotIndex,
    cluster_typed_index: _TypedSnapshotIndex,
    cluster_untyped_candidates: _UntypedSnapshotIndex,
) -> tuple[list[PreparedVMState], list[tuple[PreparedVMState, dict[str, object]]]]:
    """Split prepared VMs into writable ones and ``(prepared, blocking record)`` skips."""
    kept: list[PreparedVMState] = []
    skipped: list[tuple[PreparedVMState, dict[str, object]]] = []
    for prepared in prepared_vms:
        blocker = _unverifiable_endpoint_candidate(
            prepared,
            endpoint_untyped_candidates=endpoint_untyped_candidates,
            cluster_typed_index=cluster_typed_index,
            cluster_untyped_candidates=cluster_untyped_candidates,
        )
        if blocker is None:
            kept.append(prepared)
        else:
            skipped.append((prepared, blocker))
    return kept, skipped


def skip_unverifiable_vm_candidates(
    prepared_vms: list[PreparedVMState],
    *,
    endpoint_untyped_candidates: _UntypedSnapshotIndex,
    cluster_typed_index: _TypedSnapshotIndex,
    cluster_untyped_candidates: _UntypedSnapshotIndex,
) -> list[PreparedVMState]:
    """Drop prepared VMs whose only endpoint-keyed candidate is not cluster-verifiable.

    Skipping with a warning is preferred over a CREATE because the candidate's
    unknown or unassigned cluster could be the live one; the next sync retries
    once the NetBox row carries cluster data.
    """
    kept, skipped = _partition_unverifiable_vm_candidates(
        prepared_vms,
        endpoint_untyped_candidates=endpoint_untyped_candidates,
        cluster_typed_index=cluster_typed_index,
        cluster_untyped_candidates=cluster_untyped_candidates,
    )
    for prepared, blocker in skipped:
        log_unverifiable_vm_skip(
            blocker,
            vmid=_relation_id(prepared.resource.get("vmid")),
            endpoint_id=extract_proxmox_endpoint_id(prepared.sync_state_fields),
            cluster_id=_relation_id(prepared.desired_payload.get("cluster")),
            cluster_name=prepared.cluster_name,
        )
    return kept


def drop_unverifiable_vm_candidates(
    prepared_vms: list[PreparedVMState],
    netbox_snapshot: list[dict[str, object]],
) -> list[PreparedVMState]:
    """Apply :func:`skip_unverifiable_vm_candidates` over a raw NetBox snapshot.

    Run before any engine so Python and Rust receive the same prepared set.
    """
    indexes = build_vm_snapshot_identity_indexes(netbox_snapshot)
    return skip_unverifiable_vm_candidates(
        prepared_vms,
        endpoint_untyped_candidates=indexes[1],
        cluster_typed_index=indexes[2],
        cluster_untyped_candidates=indexes[3],
    )


def unverifiable_vm_warnings(
    prepared_vms: list[PreparedVMState],
    netbox_snapshot: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Describe every VM the queue will skip as a structured stage warning."""
    indexes = build_vm_snapshot_identity_indexes(netbox_snapshot)
    _, skipped = _partition_unverifiable_vm_candidates(
        prepared_vms,
        endpoint_untyped_candidates=indexes[1],
        cluster_typed_index=indexes[2],
        cluster_untyped_candidates=indexes[3],
    )
    return [
        {
            "netbox_vm_id": blocker.get("id"),
            "vmid": _relation_id(prepared.resource.get("vmid")),
            "cluster": prepared.cluster_name,
            "reason": (
                "NetBox VM matches the Proxmox endpoint key but its cluster "
                f"({vm_record_cluster_label(blocker)}) cannot be verified; "
                "the VM was neither updated nor created"
            ),
        }
        for prepared, blocker in skipped
    ]


def select_existing_vm_record(
    *,
    prepared: PreparedVMState,
    endpoint_id: int | None,
    cluster_id: int | None,
    proxmox_vmid: int | None,
    endpoint_typed_index: _TypedSnapshotIndex,
    endpoint_untyped_candidates: _UntypedSnapshotIndex,
    cluster_typed_index: _TypedSnapshotIndex,
    cluster_untyped_candidates: _UntypedSnapshotIndex,
) -> dict[str, object] | None:
    """Find the NetBox VM record for prepared state without guessing on type collisions.

    An endpoint-keyed match is only honoured when the record lives in the
    cluster being synchronized. Endpoint ids are not unique across clusters
    (stale or colliding ids), so a match from a different cluster is dropped,
    logged, and replaced by the live cluster's own candidate when one exists.
    """

    record = _select_scoped_vm_record(
        prepared=prepared,
        endpoint_id=endpoint_id,
        cluster_id=cluster_id,
        proxmox_vmid=proxmox_vmid,
        endpoint_typed_index=endpoint_typed_index,
        endpoint_untyped_candidates=endpoint_untyped_candidates,
        cluster_typed_index=cluster_typed_index,
        cluster_untyped_candidates=cluster_untyped_candidates,
    )
    if endpoint_id is None or proxmox_vmid is None:
        return record
    if record is not None and vm_record_in_cluster(
        record, cluster_id=cluster_id, cluster_name=prepared.cluster_name
    ):
        return record
    if record is not None:
        log_cross_cluster_rejection(
            record,
            vmid=proxmox_vmid,
            endpoint_id=endpoint_id,
            cluster_id=cluster_id,
            cluster_name=prepared.cluster_name,
            context="endpoint-scoped",
        )
    return _select_cluster_scoped_endpoint_record(
        prepared=prepared,
        endpoint_id=endpoint_id,
        cluster_id=cluster_id,
        proxmox_vmid=proxmox_vmid,
        endpoint_untyped_candidates=endpoint_untyped_candidates,
    ) or _select_verified_cluster_record(
        prepared=prepared,
        cluster_id=cluster_id,
        proxmox_vmid=proxmox_vmid,
        cluster_typed_index=cluster_typed_index,
        cluster_untyped_candidates=cluster_untyped_candidates,
    )


def prepared_vm_result_key(prepared: PreparedVMState) -> tuple[str, int, str]:
    """Build the deterministic in-memory result key for a prepared VM."""

    vmid = int(prepared.resource.get("vmid", 0) or 0)
    vm_type = normalize_proxmox_vm_type(prepared.vm_type) or ""
    return (prepared.cluster_name, vmid, vm_type)


def desired_vm_state(prepared: PreparedVMState) -> NetBoxVirtualMachineCreateBody:
    """Return the finalized desired model or validate legacy caller input."""

    if prepared.desired_state is not None:
        return prepared.desired_state
    return NetBoxVirtualMachineCreateBody.model_validate(prepared.desired_payload)


def validate_vm_platform_relations(
    prepared_vms: list[PreparedVMState],
    netbox_snapshot: list[dict[str, object]],
) -> None:
    """Validate creation-only platform relations before selecting an engine."""

    for prepared in prepared_vms:
        if prepared.desired_state is not None:
            continue
        payload = prepared.desired_payload
        if "platform" not in payload:
            continue
        NetBoxVirtualMachineCreateBody.model_validate(
            {
                "name": "platform-relation-validation",
                "status": "active",
                "platform": payload["platform"],
            }
        )
    for payload in netbox_snapshot:
        if "platform" not in payload:
            continue
        NetBoxVirtualMachineCreateBody.model_validate(
            {
                "name": "platform-relation-validation",
                "status": "active",
                "platform": payload["platform"],
            }
        )


def attach_reconciliation_flags(
    operations: list[NetBoxVMOperation],
    flags: dict[str, bool],
) -> list[NetBoxVMOperation]:
    """Retain queue policy for stale-snapshot CREATE recovery during dispatch."""

    for operation in operations:
        operation.reconciliation_flags = dict(flags)
    return operations


def build_vm_operation_queue_python(  # noqa: C901
    prepared_vms: list[PreparedVMState],
    netbox_snapshot: list[dict[str, object]],
    overwrite_vm_role: bool = True,
    overwrite_vm_type: bool = True,
    overwrite_vm_tags: bool = True,
    overwrite_vm_description: bool = True,
    overwrite_vm_custom_fields: bool = True,
    supports_virtual_machine_type_field: bool = True,
) -> list[NetBoxVMOperation]:
    """Classify desired VM state into GET/CREATE/UPDATE operations using Pydantic."""

    (
        endpoint_typed_vm_index,
        endpoint_untyped_vm_candidates,
        cluster_typed_vm_index,
        cluster_untyped_vm_candidates,
    ) = build_vm_snapshot_identity_indexes(netbox_snapshot)
    prepared_vms = skip_unverifiable_vm_candidates(
        prepared_vms,
        endpoint_untyped_candidates=endpoint_untyped_vm_candidates,
        cluster_typed_index=cluster_typed_vm_index,
        cluster_untyped_candidates=cluster_untyped_vm_candidates,
    )

    operation_queue: list[NetBoxVMOperation] = []

    for prepared in prepared_vms:
        cluster_id = _relation_id(prepared.desired_payload.get("cluster"))
        endpoint_id = extract_proxmox_endpoint_id(prepared.sync_state_fields)
        proxmox_vmid = _relation_id(prepared.resource.get("vmid"))
        if proxmox_vmid is None:
            operation_queue.append(NetBoxVMOperation(method="CREATE", prepared=prepared))
            continue

        existing_record = select_existing_vm_record(
            prepared=prepared,
            endpoint_id=endpoint_id,
            cluster_id=cluster_id,
            proxmox_vmid=proxmox_vmid,
            endpoint_typed_index=endpoint_typed_vm_index,
            endpoint_untyped_candidates=endpoint_untyped_vm_candidates,
            cluster_typed_index=cluster_typed_vm_index,
            cluster_untyped_candidates=cluster_untyped_vm_candidates,
        )
        if existing_record is None:
            operation_queue.append(NetBoxVMOperation(method="CREATE", prepared=prepared))
            continue

        desired_state = desired_vm_state(prepared)
        desired_payload = desired_state.model_dump(exclude_none=True, by_alias=True)
        # Platform is creation-only in `_compute_vm_patchable_fields`. Validate it so
        # nested current NetBox relations cannot abort reconciliation, but keep it out
        # of the existing-record diff so inferred guest OS data never overwrites an
        # operator-selected platform. The Rust normalizer enforces the same boundary by
        # omitting platform from its desired/current diff payloads.
        desired_payload.pop("platform", None)
        if not supports_virtual_machine_type_field:
            desired_payload.pop("virtual_machine_type", None)
        current_state = NetBoxVirtualMachineCreateBody.model_validate(
            normalize_current_vm_payload(
                existing_record,
                supports_virtual_machine_type_field=supports_virtual_machine_type_field,
            )
        )
        current_payload = current_state.model_dump(exclude_none=True, by_alias=True)

        patch_payload = {
            field_name: desired_value
            for field_name, desired_value in desired_payload.items()
            if current_payload.get(field_name) != desired_value
        }

        if not overwrite_vm_role and _relation_id(existing_record.get("role")) is not None:
            patch_payload.pop("role", None)
        if (
            not overwrite_vm_type
            and _relation_id(existing_record.get("virtual_machine_type")) is not None
        ):
            patch_payload.pop("virtual_machine_type", None)
        if not overwrite_vm_description:
            existing_description = existing_record.get("description")
            if isinstance(existing_description, str) and existing_description:
                patch_payload.pop("description", None)
            existing_comments = existing_record.get("comments")
            if isinstance(existing_comments, str) and existing_comments:
                patch_payload.pop("comments", None)
        if not overwrite_vm_tags:
            existing_tags = existing_record.get("tags")
            if isinstance(existing_tags, list) and existing_tags:
                patch_payload.pop("tags", None)
        elif "tags" in patch_payload:
            # Preserve existing user tags while ensuring desired Proxbox tags are present.
            existing_normalized: list[int] = current_payload.get("tags") or []
            desired_normalized: list[int] = desired_payload.get("tags") or []
            merged = sorted(set(existing_normalized) | set(desired_normalized))
            if merged == existing_normalized:
                patch_payload.pop("tags", None)
            else:
                patch_payload["tags"] = merged

        if patch_payload:
            operation_queue.append(
                NetBoxVMOperation(
                    method="UPDATE",
                    prepared=prepared,
                    existing_record=existing_record,
                    patch_payload=patch_payload,
                )
            )
        else:
            operation_queue.append(
                NetBoxVMOperation(
                    method="GET",
                    prepared=prepared,
                    existing_record=existing_record,
                )
            )

    return operation_queue


def build_vm_operation_queue(
    prepared_vms: list[PreparedVMState],
    netbox_snapshot: list[dict[str, object]],
    overwrite_vm_role: bool = True,
    overwrite_vm_type: bool = True,
    overwrite_vm_tags: bool = True,
    overwrite_vm_description: bool = True,
    overwrite_vm_custom_fields: bool = True,
    supports_virtual_machine_type_field: bool = True,
) -> list[NetBoxVMOperation]:
    """Engine-neutral VM operation-queue entry point."""

    # Platform never enters an existing-record diff, including in the Rust engine.
    # Validate the creation-only relation before engine selection so malformed or
    # schema-drifted NetBox values fail identically in python, compare, and rust modes.
    validate_vm_platform_relations(prepared_vms, netbox_snapshot)
    # Both engines must see the same prepared set: an unverifiable endpoint
    # candidate would otherwise surface as a Rust CREATE the Python path skips.
    prepared_vms = drop_unverifiable_vm_candidates(prepared_vms, netbox_snapshot)

    flags = {
        "overwrite_vm_role": overwrite_vm_role,
        "overwrite_vm_type": overwrite_vm_type,
        "overwrite_vm_tags": overwrite_vm_tags,
        "overwrite_vm_description": overwrite_vm_description,
        "overwrite_vm_custom_fields": overwrite_vm_custom_fields,
        "supports_virtual_machine_type_field": supports_virtual_machine_type_field,
    }
    engine = _reconciliation_engine()

    if engine == "rust":
        operations = _build_vm_operation_queue_with_rust(prepared_vms, netbox_snapshot, flags)
        return attach_reconciliation_flags(operations, flags)

    py_ops = build_vm_operation_queue_python(
        prepared_vms,
        netbox_snapshot,
        **flags,
    )

    if engine == "python" or not rust_available():
        return attach_reconciliation_flags(py_ops, flags)

    try:
        rust_ops = _build_vm_operation_queue_with_rust(prepared_vms, netbox_snapshot, flags)
    except Exception as exc:
        increment_reconciliation_mismatch_total()
        logger.exception("Rust reconciliation failed in compare mode; returning Python output")
        if _reconciliation_compare_strict():
            raise AssertionError("Rust reconciliation failed in compare mode") from exc
        return attach_reconciliation_flags(py_ops, flags)

    normalized_py_ops = _normalize_ops(py_ops)
    normalized_rust_ops = _normalize_ops(rust_ops)
    if normalized_py_ops != normalized_rust_ops:
        increment_reconciliation_mismatch_total()
        diff = _format_diff(normalized_py_ops, normalized_rust_ops)
        logger.error("Rust reconciliation mismatch:\n%s", diff)
        if _reconciliation_compare_strict():
            raise AssertionError(f"Rust/Python reconciliation mismatch:\n{diff}")

    return attach_reconciliation_flags(py_ops, flags)


def _build_vm_operation_queue_with_rust(
    prepared_vms: list[PreparedVMState],
    netbox_snapshot: list[dict[str, object]],
    flags: dict[str, bool],
) -> list[NetBoxVMOperation]:
    raw_ops = build_vm_operation_queue_rust(
        prepared_vms=prepared_vms,
        netbox_snapshot=netbox_snapshot,
        flags=flags,
    )
    return _reject_cross_cluster_operations(
        _adapt_to_dataclasses(raw_ops, prepared_vms), netbox_snapshot, flags
    )


def _python_selector_finds_record(
    prepared: PreparedVMState,
    indexes: tuple[
        _TypedSnapshotIndex, _UntypedSnapshotIndex, _TypedSnapshotIndex, _UntypedSnapshotIndex
    ],
) -> bool:
    """Return whether the selector adopts an endpoint-less cluster-keyed record.

    Rust can never match a record without an endpoint id, so only that case is
    corrected here; any other Rust/Python difference stays visible to compare mode.
    """
    vmid = _relation_id(prepared.resource.get("vmid"))
    if vmid is None:
        return False
    record = select_existing_vm_record(
        prepared=prepared,
        endpoint_id=extract_proxmox_endpoint_id(prepared.sync_state_fields),
        cluster_id=_relation_id(prepared.desired_payload.get("cluster")),
        proxmox_vmid=vmid,
        endpoint_typed_index=indexes[0],
        endpoint_untyped_candidates=indexes[1],
        cluster_typed_index=indexes[2],
        cluster_untyped_candidates=indexes[3],
    )
    return record is not None and extract_proxmox_endpoint_id(record) is None


def _reject_cross_cluster_operations(
    operations: list[NetBoxVMOperation],
    netbox_snapshot: list[dict[str, object]],
    flags: dict[str, bool],
) -> list[NetBoxVMOperation]:
    """Re-resolve Rust GET/UPDATE operations that target another cluster's VM.

    The Rust engine matches on the endpoint-scoped key only and may pick a VM
    from a different cluster when several clusters collide on the same
    ``(endpoint id, vmid)``. Apply the same cluster guard as
    :func:`select_existing_vm_record`: the offending operation is rebuilt by
    the Python selector over the full NetBox snapshot, so the live cluster's
    own record is still updated, and a CREATE is queued only when the live
    cluster has no candidate at all.
    """

    guarded: list[NetBoxVMOperation] = []
    indexes = build_vm_snapshot_identity_indexes(netbox_snapshot)
    for operation in operations:
        record = operation.existing_record
        prepared = operation.prepared
        cluster_id = _relation_id(prepared.desired_payload.get("cluster"))
        if record is None and _python_selector_finds_record(prepared, indexes):
            # Rust matches on the endpoint key only; the Python selector also
            # resolves a verified cluster-keyed record, so a CREATE would duplicate it.
            guarded.extend(
                build_vm_operation_queue_python([prepared], netbox_snapshot, **flags)[:1]
            )
            continue
        if record is not None and not vm_record_in_cluster(
            record, cluster_id=cluster_id, cluster_name=prepared.cluster_name
        ):
            log_cross_cluster_rejection(
                record,
                vmid=_relation_id(prepared.resource.get("vmid")),
                endpoint_id=extract_proxmox_endpoint_id(prepared.sync_state_fields),
                cluster_id=cluster_id,
                cluster_name=prepared.cluster_name,
                context="reconciliation",
            )
            guarded.extend(
                build_vm_operation_queue_python([prepared], netbox_snapshot, **flags)[:1]
            )
            continue
        guarded.append(operation)
    return guarded


def _reconciliation_engine() -> Literal["python", "compare", "rust"]:
    engine = get_plugin_str(
        settings_key="reconciliation_engine",
        default="python",
    ).lower()
    if engine not in _VALID_ENGINES:
        valid = ", ".join(sorted(_VALID_ENGINES))
        raise ValueError(
            "Invalid ProxboxPluginSettings.reconciliation_engine="
            f"{engine!r}; expected one of: {valid}"
        )
    return engine  # type: ignore[return-value]


def _reconciliation_compare_strict() -> bool:
    return get_plugin_bool(
        settings_key="reconciliation_compare_strict",
        default=False,
    )


def _adapt_to_dataclasses(
    raw_ops: list[dict[str, Any]],
    prepared_vms: list[PreparedVMState],
) -> list[NetBoxVMOperation]:
    by_key = _prepared_by_result_key(prepared_vms)
    adapted: list[NetBoxVMOperation] = []

    for index, raw_op in enumerate(raw_ops):
        key = _raw_operation_result_key(raw_op, index)
        prepared = by_key.get(key)
        if prepared is None:
            raise RustOperationAdaptationError(
                f"Rust operation {index} references unknown prepared VM identity {key!r}"
            )

        method = raw_op.get("method")
        if method not in {"GET", "CREATE", "UPDATE"}:
            raise RustOperationAdaptationError(
                f"Rust operation {index} has invalid method {method!r}"
            )

        existing_record = raw_op.get("existing_record")
        if existing_record is not None and not isinstance(existing_record, dict):
            raise RustOperationAdaptationError(
                f"Rust operation {index} has non-object existing_record"
            )

        patch_payload = raw_op.get("patch_payload") or {}
        if not isinstance(patch_payload, dict):
            raise RustOperationAdaptationError(
                f"Rust operation {index} has non-object patch_payload"
            )

        adapted.append(
            NetBoxVMOperation(
                method=method,
                prepared=prepared,
                existing_record=existing_record,
                patch_payload=patch_payload,
            )
        )

    return adapted


def _prepared_by_result_key(
    prepared_vms: list[PreparedVMState],
) -> dict[tuple[str, int, str], PreparedVMState]:
    by_key: dict[tuple[str, int, str], PreparedVMState] = {}
    for prepared in prepared_vms:
        key = prepared_vm_result_key(prepared)
        if key in by_key:
            raise RustOperationAdaptationError(f"Duplicate prepared VM identity {key!r}")
        by_key[key] = prepared
    return by_key


def _raw_operation_result_key(raw_op: dict[str, Any], index: int) -> tuple[str, int, str]:
    cluster_name = raw_op.get("cluster_name")
    if not isinstance(cluster_name, str) or not cluster_name:
        raise RustOperationAdaptationError(
            f"Rust operation {index} has invalid cluster_name {cluster_name!r}"
        )

    try:
        vmid = int(raw_op.get("vmid"))
    except (TypeError, ValueError) as exc:
        raise RustOperationAdaptationError(
            f"Rust operation {index} has invalid vmid {raw_op.get('vmid')!r}"
        ) from exc

    vm_type = normalize_proxmox_vm_type(raw_op.get("vm_type")) or ""
    return (cluster_name, vmid, vm_type)


def _normalize_ops(ops: list[NetBoxVMOperation]) -> list[dict[str, object]]:
    return [
        {
            "method": op.method,
            "prepared": {
                "cluster_name": prepared_vm_result_key(op.prepared)[0],
                "vmid": prepared_vm_result_key(op.prepared)[1],
                "vm_type": prepared_vm_result_key(op.prepared)[2],
            },
            "existing_record": _normalize_value(op.existing_record),
            "patch_payload": _normalize_value(op.patch_payload),
            "desired_payload": _normalize_value(op.prepared.desired_payload),
        }
        for op in ops
    ]


def _normalize_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _normalize_value(child)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list | tuple):
        return [_normalize_value(child) for child in value]
    return value


def _format_diff(
    python_ops: list[dict[str, object]],
    rust_ops: list[dict[str, object]],
) -> str:
    python_text = json.dumps(python_ops, indent=2, sort_keys=True, default=str).splitlines()
    rust_text = json.dumps(rust_ops, indent=2, sort_keys=True, default=str).splitlines()
    diff = "\n".join(
        difflib.unified_diff(
            python_text,
            rust_text,
            fromfile="python",
            tofile="rust",
            lineterm="",
        )
    )
    if len(diff) > _MAX_DIFF_CHARS:
        return f"{diff[:_MAX_DIFF_CHARS]}\n... diff truncated ..."
    return diff
