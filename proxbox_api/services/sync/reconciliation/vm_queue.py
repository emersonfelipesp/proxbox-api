"""Pure VM operation-queue reconciliation."""

from __future__ import annotations

import difflib
import json
import logging
import os
from typing import Any, Literal

from proxbox_api.proxmox_to_netbox.models import NetBoxVirtualMachineCreateBody
from proxbox_api.services.sync.reconciliation.metrics import (
    increment_reconciliation_mismatch_total,
)
from proxbox_api.services.sync.reconciliation.rust_bridge import (
    build_vm_operation_queue_rust,
    rust_available,
)
from proxbox_api.services.sync.reconciliation.types import NetBoxVMOperation, PreparedVMState
from proxbox_api.services.sync.vm_helpers import (
    normalize_current_virtual_machine_payload,
)
from proxbox_api.services.sync.vm_helpers import (
    relation_id as _relation_id,
)

logger = logging.getLogger(__name__)

_ENGINE_ENV = "PROXBOX_RECONCILIATION_ENGINE"
_COMPARE_STRICT_ENV = "PROXBOX_RECONCILIATION_COMPARE_STRICT"
_VALID_ENGINES = {"python", "compare", "rust"}
_MAX_DIFF_CHARS = 12000


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
    """Build the in-memory index key used to correlate NetBox VM records."""

    cluster_id = _relation_id(record.get("cluster"))
    if cluster_id is None:
        return None
    custom_fields = record.get("custom_fields")
    if not isinstance(custom_fields, dict):
        return None
    raw_vmid = custom_fields.get("proxmox_vm_id")
    try:
        proxmox_vmid = int(str(raw_vmid).strip())
    except (TypeError, ValueError):
        return None
    return (cluster_id, proxmox_vmid)


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
    """Return the stored Proxmox VM type custom field for a NetBox VM record."""

    custom_fields = record.get("custom_fields")
    if not isinstance(custom_fields, dict):
        return None
    return normalize_proxmox_vm_type(custom_fields.get("proxmox_vm_type"))


def build_vm_snapshot_identity_indexes(
    snapshot: list[dict[str, object]],
) -> tuple[
    dict[tuple[int, int, str], dict[str, object]],
    dict[tuple[int, int], list[dict[str, object]]],
]:
    """Index NetBox VM records by typed identity plus legacy untyped candidates."""

    typed_index: dict[tuple[int, int, str], dict[str, object]] = {}
    untyped_candidates: dict[tuple[int, int], list[dict[str, object]]] = {}
    for current in snapshot:
        key = extract_cluster_and_proxmox_vmid(current)
        if key is None:
            continue
        untyped_candidates.setdefault(key, []).append(current)
        vm_type = extract_proxmox_vm_type(current)
        if vm_type is not None:
            typed_index.setdefault((key[0], key[1], vm_type), current)
    return typed_index, untyped_candidates


def select_existing_vm_record(
    *,
    prepared: PreparedVMState,
    cluster_id: int | None,
    proxmox_vmid: int | None,
    typed_index: dict[tuple[int, int, str], dict[str, object]],
    untyped_candidates: dict[tuple[int, int], list[dict[str, object]]],
) -> dict[str, object] | None:
    """Find the NetBox VM record for prepared state without guessing on type collisions."""

    if cluster_id is None or proxmox_vmid is None:
        return None

    prepared_vm_type = normalize_proxmox_vm_type(prepared.vm_type)
    untyped_key = (cluster_id, proxmox_vmid)
    if prepared_vm_type is not None:
        exact_record = typed_index.get((cluster_id, proxmox_vmid, prepared_vm_type))
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


def prepared_vm_result_key(prepared: PreparedVMState) -> tuple[str, int, str]:
    """Build the deterministic in-memory result key for a prepared VM."""

    vmid = int(prepared.resource.get("vmid", 0) or 0)
    vm_type = normalize_proxmox_vm_type(prepared.vm_type) or ""
    return (prepared.cluster_name, vmid, vm_type)


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

    typed_vm_index, untyped_vm_candidates = build_vm_snapshot_identity_indexes(netbox_snapshot)

    operation_queue: list[NetBoxVMOperation] = []

    for prepared in prepared_vms:
        cluster_id = _relation_id(prepared.desired_payload.get("cluster"))
        proxmox_vmid = _relation_id(prepared.resource.get("vmid"))
        if cluster_id is None or proxmox_vmid is None:
            operation_queue.append(NetBoxVMOperation(method="CREATE", prepared=prepared))
            continue

        existing_record = select_existing_vm_record(
            prepared=prepared,
            cluster_id=cluster_id,
            proxmox_vmid=proxmox_vmid,
            typed_index=typed_vm_index,
            untyped_candidates=untyped_vm_candidates,
        )
        if existing_record is None:
            operation_queue.append(NetBoxVMOperation(method="CREATE", prepared=prepared))
            continue

        desired_state = NetBoxVirtualMachineCreateBody.model_validate(prepared.desired_payload)
        desired_payload = desired_state.model_dump(exclude_none=True, by_alias=True)
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
        if not overwrite_vm_custom_fields:
            existing_custom_fields = existing_record.get("custom_fields")
            if isinstance(existing_custom_fields, dict) and existing_custom_fields:
                patch_payload.pop("custom_fields", None)
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
        return _build_vm_operation_queue_with_rust(prepared_vms, netbox_snapshot, flags)

    py_ops = build_vm_operation_queue_python(
        prepared_vms,
        netbox_snapshot,
        **flags,
    )

    if engine == "python" or not rust_available():
        return py_ops

    try:
        rust_ops = _build_vm_operation_queue_with_rust(prepared_vms, netbox_snapshot, flags)
    except Exception as exc:
        increment_reconciliation_mismatch_total()
        logger.exception("Rust reconciliation failed in compare mode; returning Python output")
        if _reconciliation_compare_strict():
            raise AssertionError("Rust reconciliation failed in compare mode") from exc
        return py_ops

    normalized_py_ops = _normalize_ops(py_ops)
    normalized_rust_ops = _normalize_ops(rust_ops)
    if normalized_py_ops != normalized_rust_ops:
        increment_reconciliation_mismatch_total()
        diff = _format_diff(normalized_py_ops, normalized_rust_ops)
        logger.error("Rust reconciliation mismatch:\n%s", diff)
        if _reconciliation_compare_strict():
            raise AssertionError(f"Rust/Python reconciliation mismatch:\n{diff}")

    return py_ops


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
    return _adapt_to_dataclasses(raw_ops, prepared_vms)


def _reconciliation_engine() -> Literal["python", "compare", "rust"]:
    engine = os.getenv(_ENGINE_ENV, "python").strip().lower()
    if engine not in _VALID_ENGINES:
        valid = ", ".join(sorted(_VALID_ENGINES))
        raise ValueError(f"Invalid {_ENGINE_ENV}={engine!r}; expected one of: {valid}")
    return engine  # type: ignore[return-value]


def _reconciliation_compare_strict() -> bool:
    return os.getenv(_COMPARE_STRICT_ENV, "false").strip().lower() == "true"


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
