from __future__ import annotations

import pytest

from proxbox_api.proxmox_to_netbox.errors import ProxmoxToNetBoxError
from proxbox_api.proxmox_to_netbox.mappers.virtual_machine import (
    map_proxmox_vm_to_netbox_vm_body,
)
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxInterfaceSyncState,
    NetBoxVirtualMachineInterfaceSyncState,
)
from proxbox_api.proxmox_to_netbox.normalize import build_virtual_machine_transform
from proxbox_api.services.sync.virtual_machines import (
    build_netbox_virtual_machine_payload,
)
from tests.fixtures import PROXMOX_VM_CONFIG, PROXMOX_VM_RESOURCE


def test_map_proxmox_vm_to_netbox_vm_body_uses_schema_driven_normalization(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.normalize.resolve_netbox_schema_contract",
        lambda: {
            "source": "cache",
            "openapi": {"paths": {"/api/virtualization/virtual-machines/": {"post": {}}}},
        },
    )

    body = map_proxmox_vm_to_netbox_vm_body(
        resource=PROXMOX_VM_RESOURCE,
        config=PROXMOX_VM_CONFIG,
        cluster_id=11,
        device_id=22,
        role_id=33,
        tag_ids=[7, 0],
    )

    assert body["name"] == "db-vm-01"
    assert body["status"] == "active"
    assert body["cluster"] == 11
    assert body["device"] == 22
    assert body["role"] == 33
    assert body["vcpus"] == 4
    assert body["memory"] > 0
    assert body["disk"] > 0
    assert body["tags"] == [7]
    assert body["custom_fields"]["proxmox_vm_id"] == 101
    assert body["custom_fields"]["proxmox_vm_type"] == "qemu"


def test_build_netbox_virtual_machine_payload_matches_mapper(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.normalize.resolve_netbox_schema_contract",
        lambda: {"source": "cache", "openapi": {"paths": {}}},
    )

    payload = build_netbox_virtual_machine_payload(
        proxmox_resource=PROXMOX_VM_RESOURCE,
        proxmox_config=PROXMOX_VM_CONFIG,
        cluster_id=11,
        device_id=None,
        role_id=None,
        tag_ids=[7],
    )
    assert payload["description"] == "Synced from Proxmox node pve01"
    assert payload["custom_fields"]["proxmox_qemu_agent"] is True


def test_build_netbox_virtual_machine_payload_sets_lxc_vm_type(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.normalize.resolve_netbox_schema_contract",
        lambda: {"source": "cache", "openapi": {"paths": {}}},
    )

    lxc_resource = dict(PROXMOX_VM_RESOURCE)
    lxc_resource["type"] = "lxc"

    payload = build_netbox_virtual_machine_payload(
        proxmox_resource=lxc_resource,
        proxmox_config=PROXMOX_VM_CONFIG,
        cluster_id=11,
        device_id=None,
        role_id=None,
        tag_ids=[7],
    )
    assert payload["custom_fields"]["proxmox_vm_type"] == "lxc"


def test_build_virtual_machine_transform_requires_cluster_id(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.normalize.resolve_netbox_schema_contract",
        lambda: {"source": "cache", "openapi": {"paths": {}}},
    )

    with pytest.raises(ValueError, match="cluster must be a positive NetBox object id"):
        build_virtual_machine_transform(
            resource=PROXMOX_VM_RESOURCE,
            config=PROXMOX_VM_CONFIG,
            cluster_id=0,
            device_id=None,
            role_id=None,
            tag_ids=[],
        )


def test_build_virtual_machine_transform_requires_generated_proxmox_operation(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.normalize.proxmox_operation_schema",
        lambda path, method: None,
    )

    with pytest.raises(
        ProxmoxToNetBoxError,
        match="Generated Proxmox OpenAPI is missing /cluster/resources GET operation.",
    ):
        build_virtual_machine_transform(
            resource=PROXMOX_VM_RESOURCE,
            config=PROXMOX_VM_CONFIG,
            cluster_id=11,
            device_id=None,
            role_id=None,
            tag_ids=[],
        )


def test_virtual_machine_interface_state_accepts_choice_object_mode():
    state = NetBoxVirtualMachineInterfaceSyncState.model_validate(
        {
            "virtual_machine": 1,
            "name": "net0",
            "mode": {"value": "access", "label": "Access"},
        }
    )

    assert state.mode == "access"


def test_interface_state_accepts_choice_object_mode():
    state = NetBoxInterfaceSyncState.model_validate(
        {
            "device": 1,
            "name": "eth0",
            "type": "other",
            "mode": {"value": "access", "label": "Access"},
        }
    )

    assert state.mode == "access"
