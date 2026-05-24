"""Python-side payload adapter for the optional Rust reconciliation bridge."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, TypeAdapter

try:
    from proxbox_reconcile_rs._native import build_vm_operation_queue_json as _rust_build
except ImportError:
    _rust_build = None


class _BridgeVm(BaseModel):
    """Serializable subset of prepared VM state consumed by the bridge."""

    cluster_name: str
    resource: dict[str, Any]
    desired_payload: dict[str, Any]
    lookup: dict[str, Any]
    vm_type: str


class _BridgeInput(BaseModel):
    """Complete bridge input payload."""

    prepared_vms: list[_BridgeVm]
    netbox_snapshot: list[dict[str, Any]]
    flags: dict[str, bool]


_input_adapter = TypeAdapter(_BridgeInput)


def rust_available() -> bool:
    """Return whether the optional Rust reconciliation extension is importable."""

    return _rust_build is not None


def build_bridge_input(
    *,
    prepared_vms: list[Any],
    netbox_snapshot: list[dict[str, Any]],
    flags: dict[str, bool],
) -> _BridgeInput:
    """Build the validated, Rust-ready bridge payload from prepared VM state."""

    return _BridgeInput(
        prepared_vms=[
            _BridgeVm(
                cluster_name=prepared.cluster_name,
                resource=prepared.resource,
                desired_payload=prepared.desired_payload,
                lookup=prepared.lookup,
                vm_type=prepared.vm_type,
            )
            for prepared in prepared_vms
        ],
        netbox_snapshot=netbox_snapshot,
        flags=flags,
    )


def dump_bridge_input_json(
    *,
    prepared_vms: list[Any],
    netbox_snapshot: list[dict[str, Any]],
    flags: dict[str, bool],
) -> bytes:
    """Serialize bridge input through Pydantic v2's JSON adapter."""

    payload = build_bridge_input(
        prepared_vms=prepared_vms,
        netbox_snapshot=netbox_snapshot,
        flags=flags,
    )
    return _input_adapter.dump_json(payload)


def build_vm_operation_queue_rust(
    *,
    prepared_vms: list[Any],
    netbox_snapshot: list[dict[str, Any]],
    flags: dict[str, bool],
) -> list[dict[str, Any]]:
    """Run the optional Rust VM queue builder and decode its JSON response."""

    if _rust_build is None:
        raise RuntimeError("proxbox-reconcile-rs is not installed")

    input_bytes = dump_bridge_input_json(
        prepared_vms=prepared_vms,
        netbox_snapshot=netbox_snapshot,
        flags=flags,
    )
    output_bytes = _rust_build(input_bytes)
    return json.loads(output_bytes)
