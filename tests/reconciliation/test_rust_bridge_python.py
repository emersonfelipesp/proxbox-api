"""Tests for the Python-only Rust bridge payload adapter."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from proxbox_api.proxmox_to_netbox.models import ProxmoxVmConfigInput
from proxbox_api.services.sync.reconciliation.rust_bridge import (
    build_bridge_input,
    dump_bridge_input_json,
    rust_available,
)
from proxbox_api.services.sync.reconciliation.types import PreparedVMState


def _prepared_vm() -> PreparedVMState:
    return PreparedVMState(
        cluster_name="cluster-a",
        resource={"name": "vm-100", "vmid": 100, "type": "qemu"},
        vm_config={"memory": 2048},
        vm_config_obj=ProxmoxVmConfigInput.model_validate({}),
        desired_payload={
            "name": "vm-100",
            "status": "active",
            "cluster": 1,
            "device": 10,
            "memory": 2048,
            "custom_fields": {"proxmox_vm_id": 100, "proxmox_vm_type": "qemu"},
        },
        lookup={"cf_proxmox_vm_id": 100, "cluster_id": 1},
        now=datetime.now(timezone.utc),
        vm_type="qemu",
    )


def test_rust_bridge_is_unavailable_without_native_extension() -> None:
    assert rust_available() is False


def test_build_bridge_input_uses_serializable_prepared_subset() -> None:
    payload = build_bridge_input(
        prepared_vms=[_prepared_vm()],
        netbox_snapshot=[{"id": 2000, "custom_fields": {"proxmox_vm_id": 100}}],
        flags={"overwrite_vm_role": True},
    )

    assert len(payload.prepared_vms) == 1
    bridge_vm = payload.prepared_vms[0]
    assert bridge_vm.cluster_name == "cluster-a"
    assert bridge_vm.resource["vmid"] == 100
    assert bridge_vm.desired_payload["cluster"] == 1
    assert bridge_vm.lookup == {"cf_proxmox_vm_id": 100, "cluster_id": 1}
    assert bridge_vm.vm_type == "qemu"


def test_dump_bridge_input_json_returns_compact_bytes() -> None:
    output = dump_bridge_input_json(
        prepared_vms=[_prepared_vm()],
        netbox_snapshot=[{"id": 2000, "custom_fields": {"proxmox_vm_id": 100}}],
        flags={
            "overwrite_vm_role": True,
            "overwrite_vm_type": True,
            "supports_virtual_machine_type_field": True,
        },
    )

    decoded = json.loads(output)

    assert isinstance(output, bytes)
    assert decoded["prepared_vms"][0] == {
        "cluster_name": "cluster-a",
        "resource": {"name": "vm-100", "vmid": 100, "type": "qemu"},
        "desired_payload": {
            "name": "vm-100",
            "status": "active",
            "cluster": 1,
            "device": 10,
            "memory": 2048,
            "custom_fields": {"proxmox_vm_id": 100, "proxmox_vm_type": "qemu"},
        },
        "lookup": {"cf_proxmox_vm_id": 100, "cluster_id": 1},
        "vm_type": "qemu",
    }
    assert "vm_config" not in decoded["prepared_vms"][0]
    assert "now" not in decoded["prepared_vms"][0]
