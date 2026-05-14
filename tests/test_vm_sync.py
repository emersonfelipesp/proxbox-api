"""Tests for virtual machine mapping and sync logic."""

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
    # Without parseable disk entries in PROXMOX_VM_CONFIG, body["disk"] == 0:
    # the maxdisk fallback was removed (issue #349) so VM.disk equals the
    # aggregate of parsed virtual-disks. See
    # test_map_proxmox_vm_to_netbox_vm_body_uses_config_disk_aggregate for the
    # >0 path.
    assert body["disk"] == 0
    assert body["tags"] == [7]
    assert body["custom_fields"]["proxmox_vm_id"] == 101
    assert body["custom_fields"]["proxmox_vm_type"] == "qemu"


def test_map_proxmox_vm_to_netbox_vm_body_uses_config_disk_aggregate(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.normalize.resolve_netbox_schema_contract",
        lambda: {"source": "cache", "openapi": {"paths": {}}},
    )

    body = map_proxmox_vm_to_netbox_vm_body(
        resource=PROXMOX_VM_RESOURCE,
        config={
            **PROXMOX_VM_CONFIG,
            "scsi0": "local-lvm:vm-101-disk-0,size=32G",
            "scsi1": "local-lvm:vm-101-disk-1,size=64G",
        },
        cluster_id=11,
        device_id=22,
        role_id=33,
        tag_ids=[7],
    )

    # NetBox validates VM.disk against the sum of assigned virtual disk sizes (MiB).
    assert body["disk"] == 98_304


def test_map_proxmox_vm_to_netbox_vm_body_parses_virtio_disks(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.normalize.resolve_netbox_schema_contract",
        lambda: {"source": "cache", "openapi": {"paths": {}}},
    )

    body = map_proxmox_vm_to_netbox_vm_body(
        resource=PROXMOX_VM_RESOURCE,
        config={
            **PROXMOX_VM_CONFIG,
            "virtio0": "local-lvm:vm-101-disk-0,size=40G",
        },
        cluster_id=11,
        device_id=22,
        role_id=33,
        tag_ids=[7],
    )

    assert body["disk"] == 40_960


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


def test_interface_state_accepts_choice_object_type():
    state = NetBoxInterfaceSyncState.model_validate(
        {
            "device": 1,
            "name": "vmbr0",
            "type": {"value": "bridge", "label": "Bridge"},
        }
    )

    assert state.type == "bridge"


def test_virtual_machine_interface_state_accepts_choice_object_type():
    state = NetBoxVirtualMachineInterfaceSyncState.model_validate(
        {
            "virtual_machine": 1,
            "name": "vmbr0",
            "type": {"value": "bridge", "label": "Bridge"},
        }
    )

    assert state.type == "bridge"


def test_build_payload_applies_description_metadata_when_enabled(monkeypatch):
    """End-to-end: ``parse_description_metadata=True`` plus a fenced JSON block
    on the Proxmox VM description applies known PKs to the resulting body and
    strips the metadata block from the written description.

    Issue #365: ``tenant`` is *not* a known VM create-body key — proxbox-api
    leaves tenant assignment to the netbox-proxbox plugin's name-regex
    mapping — so an inline ``tenant`` is silently dropped.
    """

    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.normalize.resolve_netbox_schema_contract",
        lambda: {"source": "cache", "openapi": {"paths": {}}},
    )

    description = 'Production database VM.\n```netbox-metadata\n{"tenant": 13, "site": 4}\n```\n'

    payload = build_netbox_virtual_machine_payload(
        proxmox_resource=PROXMOX_VM_RESOURCE,
        proxmox_config={**PROXMOX_VM_CONFIG, "description": description},
        cluster_id=11,
        device_id=None,
        role_id=None,
        tag_ids=[7],
        parse_description_metadata=True,
    )

    assert payload["site"] == 4
    assert "tenant" not in payload
    assert "netbox-metadata" not in payload["description"]
    assert "Production database VM." in payload["description"]


def test_build_payload_ignores_metadata_when_flag_off(monkeypatch):
    """When the toggle is off, fenced metadata is silently ignored and the
    description preserves the existing fallback."""

    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.normalize.resolve_netbox_schema_contract",
        lambda: {"source": "cache", "openapi": {"paths": {}}},
    )

    description = '```netbox-metadata\n{"tenant": 13}\n```'

    payload = build_netbox_virtual_machine_payload(
        proxmox_resource=PROXMOX_VM_RESOURCE,
        proxmox_config={**PROXMOX_VM_CONFIG, "description": description},
        cluster_id=11,
        device_id=None,
        role_id=None,
        tag_ids=[7],
        parse_description_metadata=False,
    )

    assert payload.get("tenant") in (None, 0) or "tenant" not in payload
    assert payload["description"] == "Synced from Proxmox node pve01"


def test_build_payload_respects_overwrite_vm_role_when_off(monkeypatch):
    """Metadata key ``role`` is gated off when ``overwrite_vm_role=False`` and
    the fallback PK resolves instead.

    Issue #365: ``tenant`` is never a valid VM create-body key, regardless of
    overwrite flags — it is always dropped.
    """

    from proxbox_api.schemas.sync import SyncOverwriteFlags

    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.normalize.resolve_netbox_schema_contract",
        lambda: {"source": "cache", "openapi": {"paths": {}}},
    )

    overwrite_flags = SyncOverwriteFlags(overwrite_vm_role=False)
    description = '```netbox-metadata\n{"role": 5, "tenant": 13}\n```'

    payload = build_netbox_virtual_machine_payload(
        proxmox_resource=PROXMOX_VM_RESOURCE,
        proxmox_config={**PROXMOX_VM_CONFIG, "description": description},
        cluster_id=11,
        device_id=None,
        role_id=99,  # fallback from regular resolution
        tag_ids=[7],
        parse_description_metadata=True,
        overwrite_flags=overwrite_flags,
    )

    assert payload["role"] == 99
    assert "tenant" not in payload
