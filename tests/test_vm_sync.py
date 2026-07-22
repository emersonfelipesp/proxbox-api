"""Tests for virtual machine mapping and sync logic."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from proxbox_api.app.full_update import full_update_sync
from proxbox_api.proxmox_to_netbox.errors import ProxmoxToNetBoxError
from proxbox_api.proxmox_to_netbox.mappers.virtual_machine import (
    map_proxmox_vm_to_netbox_vm_body,
)
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxInterfaceSyncState,
    NetBoxVirtualMachineInterfaceSyncState,
)
from proxbox_api.proxmox_to_netbox.normalize import build_virtual_machine_transform
from proxbox_api.routes.virtualization.virtual_machines import read_vm, sync_vm
from proxbox_api.routes.virtualization.virtual_machines.sync_vm import (
    SyncResultList,
    create_only_vm_interfaces,
    create_only_vm_ip_addresses,
)
from proxbox_api.schemas.sync import SyncBehaviorFlags, SyncOverwriteFlags
from proxbox_api.services.netbox_bootstrap import BootstrapStatus
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


def test_default_vm_sync_network_preserves_operator_rename_with_sidecar(monkeypatch):
    existing_vm = {
        "id": 55,
        "name": "gateway-prod",
        "status": "active",
        "cluster": {"id": 10, "name": "cluster-a"},
        "device": {"id": 22},
        "role": {"id": 17},
        "vcpus": 2,
        "memory": 2048,
        "disk": 0,
        "tags": [{"id": 7}],
        "custom_fields": {
            "proxmox_endpoint_id": 500,
            "proxmox_vm_id": 101,
            "proxmox_vm_type": "qemu",
        },
        "description": "Synced from Proxmox node pve01",
    }
    vm_reconcile_payloads: list[dict[str, object]] = []
    sidecar_kwargs: dict[str, object] = {}
    last_synced_loads = 0

    async def _fake_detect_netbox_version(_nb):
        return (4, 5, 0)

    async def _fake_rest_list(_nb, _path, **_kwargs):
        return []

    async def _fake_ensure(*_args, **_kwargs):
        return SimpleNamespace(id=1)

    async def _fake_ensure_cluster(*_args, **_kwargs):
        return SimpleNamespace(id=10)

    async def _fake_ensure_device(*_args, **_kwargs):
        return SimpleNamespace(id=22)

    async def _fake_reconcile(_nb, path, **kwargs):
        if path == "/api/virtualization/virtual-machines/":
            payload = dict(kwargs["payload"])
            vm_reconcile_payloads.append(payload)
            return {"id": 55, **payload}
        return SimpleNamespace(id=17, name=(kwargs.get("payload") or {}).get("name"))

    async def _fake_snapshot(_nb, *, fresh=False):
        assert fresh is True
        return [dict(existing_vm)]

    async def _fake_last_synced(_nb):
        nonlocal last_synced_loads
        last_synced_loads += 1
        return {55: "web-01"}

    async def _fake_existing_resolution(*_args, **_kwargs):
        return SimpleNamespace(record=existing_vm, record_id=55, source="sidecar")

    def _fake_get_vm_config(**_kwargs):
        return {}

    async def _fake_write_sidecar(*_args, **kwargs):
        sidecar_kwargs.update(kwargs)
        return None

    async def _fake_stamp(*_args, **_kwargs):
        return None

    async def _fake_task_history(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(sync_vm, "detect_netbox_version", _fake_detect_netbox_version)
    monkeypatch.setattr(sync_vm, "rest_list_async", _fake_rest_list)
    monkeypatch.setattr(sync_vm, "rest_reconcile_async", _fake_reconcile)
    monkeypatch.setattr(sync_vm, "_load_netbox_virtual_machine_snapshot", _fake_snapshot)
    monkeypatch.setattr(sync_vm, "load_vm_last_synced_names", _fake_last_synced)
    monkeypatch.setattr(
        sync_vm,
        "resolve_virtual_machine_by_sync_state",
        _fake_existing_resolution,
    )
    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)
    monkeypatch.setattr(sync_vm, "resolve_vm_sync_concurrency", lambda: 1)
    monkeypatch.setattr(sync_vm, "write_virtual_machine_sync_state", _fake_write_sidecar)
    monkeypatch.setattr(sync_vm, "stamp_vm_last_run_id", _fake_stamp)
    monkeypatch.setattr(sync_vm, "sync_virtual_machine_task_history", _fake_task_history)
    for name in (
        "_ensure_cluster_type",
        "_ensure_manufacturer",
        "_ensure_device_type",
        "_ensure_site",
        "_resolve_tenant",
        "_ensure_proxmox_node_role",
    ):
        monkeypatch.setattr(sync_vm, name, _fake_ensure)
    monkeypatch.setattr(sync_vm, "_ensure_cluster", _fake_ensure_cluster)
    monkeypatch.setattr(sync_vm, "_ensure_device", _fake_ensure_device)

    result = asyncio.run(
        sync_vm.create_virtual_machines(
            netbox_session=object(),
            pxs=[
                SimpleNamespace(
                    name="cluster-a",
                    cluster_name="cluster-a",
                    db_endpoint_id=500,
                    domain="pve.example",
                    http_port=8006,
                )
            ],
            cluster_status=[SimpleNamespace(name="cluster-a", mode="cluster")],
            cluster_resources=[
                {
                    "cluster-a": [
                        {
                            "type": "qemu",
                            "name": "web-01",
                            "node": "pve01",
                            "vmid": 101,
                            "status": "running",
                            "maxcpu": 2,
                            "maxmem": 2_147_483_648,
                            "maxdisk": 0,
                        }
                    ]
                }
            ],
            custom_fields=[],
            tag=SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
            behavior_flags=SyncBehaviorFlags(custom_fields_enabled=True),
        )
    )

    assert len(result) == 1
    assert last_synced_loads == 1
    assert vm_reconcile_payloads[0]["name"] == "gateway-prod"
    assert result[0]["name"] == "gateway-prod"
    assert sidecar_kwargs["virtual_machine_id"] == 55
    assert sidecar_kwargs["proxmox_vm_name"] == "web-01"


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


def test_create_only_vm_interfaces_mirrors_bridge_sidecar_with_custom_field_gate(monkeypatch):
    patch_calls: list[dict[str, object]] = []
    sidecar_calls: list[dict[str, object]] = []

    cluster_status = [SimpleNamespace(name="lab")]
    cluster_resources = [
        {
            "lab": [
                {
                    "type": "qemu",
                    "name": "vm01",
                    "node": "pve01",
                    "vmid": 101,
                }
            ]
        }
    ]

    def _fake_get_vm_config(**_kwargs):
        return {"net0": "bridge=vmbr0"}

    async def _fake_load_snapshot(_nb):
        return [
            {
                "id": 55,
                "name": "vm01",
                "custom_fields": {"proxmox_vm_id": 101},
            }
        ]

    async def _fake_resolve_cluster_id(*_args, **_kwargs):
        return None

    async def _fake_bulk_reconcile(_nb, _payloads, **_kwargs):
        return ([{"id": 66, "name": "net0", "virtual_machine": 55}], {("net0", 55): 66})

    async def _fake_ensure_bridge(*_args, **_kwargs):
        return 400

    async def _fake_rest_first(_nb, path, *, query=None):
        if path == "/api/dcim/devices/":
            return {"id": 12}
        if path == "/api/virtualization/interfaces/":
            return {"id": 66, "name": query["name"], "virtual_machine": query["virtual_machine_id"]}
        return None

    async def _fake_rest_patch(_nb, path, record_id, payload):
        patch_calls.append({"path": path, "record_id": record_id, "payload": payload})
        return {"id": record_id, **payload}

    async def _fake_sidecar(_nb, **kwargs):
        sidecar_calls.append(kwargs)

    async def _fake_guest_sidecars(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)
    monkeypatch.setattr(sync_vm, "resolve_vm_sync_concurrency", lambda: 1)
    monkeypatch.setattr(sync_vm, "_load_netbox_virtual_machine_snapshot", _fake_load_snapshot)
    monkeypatch.setattr(sync_vm, "resolve_netbox_cluster_id_by_name", _fake_resolve_cluster_id)
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.bulk_reconcile_vm_interfaces",
        _fake_bulk_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.bridge_interfaces.ensure_bridge_interfaces",
        _fake_ensure_bridge,
    )
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_first_async", _fake_rest_first)
    monkeypatch.setattr(sync_vm, "rest_patch_async", _fake_rest_patch)
    monkeypatch.setattr(sync_vm, "write_vm_interface_sync_state", _fake_sidecar)
    monkeypatch.setattr(sync_vm, "reconcile_guest_vm_interfaces", _fake_guest_sidecars)

    common_kwargs = {
        "netbox_session": SimpleNamespace(client=object()),
        "pxs": [SimpleNamespace(name="lab")],
        "cluster_status": cluster_status,
        "cluster_resources": cluster_resources,
        "custom_fields": [],
        "tag": SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
        "sync_mac": False,
    }

    result = asyncio.run(create_only_vm_interfaces(**common_kwargs))

    assert result == [
        {
            "id": 66,
            "mac_address": None,
            "interface": {"id": 66, "name": "net0", "virtual_machine": 55},
        }
    ]
    assert patch_calls == []
    assert sidecar_calls == [
        {
            "vm_interface_id": 66,
            "proxbox_bridge_id": 400,
            "overwrite_custom_fields": True,
        }
    ]

    patch_calls.clear()
    sidecar_calls.clear()

    asyncio.run(
        create_only_vm_interfaces(
            **common_kwargs,
            overwrite_flags=SyncOverwriteFlags(overwrite_vm_interface_custom_fields=False),
        )
    )

    assert patch_calls == []
    assert sidecar_calls == [
        {
            "vm_interface_id": 66,
            "proxbox_bridge_id": 400,
            "overwrite_custom_fields": False,
        }
    ]


def test_create_only_vm_interfaces_reports_partial_bulk_warning(monkeypatch):
    phase_summaries: list[dict[str, object]] = []
    warning_messages: list[str] = []

    class _Bridge:
        async def send_json(self, _payload):
            return None

        async def emit_phase_summary(self, **kwargs):
            phase_summaries.append(kwargs)

    def _capture_warning(message, *args, **_kwargs):
        warning_messages.append(message % args if args else message)

    cluster_status = [SimpleNamespace(name="lab")]
    cluster_resources = [
        {
            "lab": [
                {"type": "qemu", "name": "vm01", "node": "pve01", "vmid": 101},
                {"type": "qemu", "name": "vm02", "node": "pve01", "vmid": 102},
            ]
        }
    ]

    def _fake_get_vm_config(**_kwargs):
        return {"net0": "virtio=aa:bb:cc:dd:ee:ff"}

    async def _fake_load_snapshot(_nb):
        return [
            {"id": 55, "name": "vm01", "custom_fields": {"proxmox_vm_id": 101}},
            {"id": 56, "name": "vm02", "custom_fields": {"proxmox_vm_id": 102}},
        ]

    async def _fake_resolve_cluster_id(*_args, **_kwargs):
        return None

    async def _fake_bulk_reconcile(_nb, payloads, **_kwargs):
        assert [payload["name"] for payload in payloads] == ["net0", "net0"]
        return ([{"id": 66, "name": "net0", "virtual_machine": 55}], {("net0", 55): 66})

    async def _fake_guest_sidecars(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)
    monkeypatch.setattr(sync_vm, "resolve_vm_sync_concurrency", lambda: 1)
    monkeypatch.setattr(sync_vm, "_load_netbox_virtual_machine_snapshot", _fake_load_snapshot)
    monkeypatch.setattr(sync_vm, "resolve_netbox_cluster_id_by_name", _fake_resolve_cluster_id)
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.bulk_reconcile_vm_interfaces",
        _fake_bulk_reconcile,
    )
    monkeypatch.setattr(sync_vm, "reconcile_guest_vm_interfaces", _fake_guest_sidecars)
    monkeypatch.setattr(sync_vm.logger, "warning", _capture_warning)

    result = asyncio.run(
        create_only_vm_interfaces(
            netbox_session=SimpleNamespace(client=object()),
            pxs=[SimpleNamespace(name="lab")],
            cluster_status=cluster_status,
            cluster_resources=cluster_resources,
            custom_fields=[],
            tag=SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
            websocket=_Bridge(),
            use_websocket=True,
            sync_mac=False,
        )
    )

    assert list(result) == [
        {
            "id": 66,
            "mac_address": None,
            "interface": {"id": 66, "name": "net0", "virtual_machine": 55},
        }
    ]
    assert result.warnings == [
        {
            "phase": "vm-interfaces",
            "succeeded": 1,
            "failed": 1,
            "requested": 2,
            "message": (
                "VM interface reconciliation completed with partial failures: "
                "1 succeeded, 1 failed."
            ),
        }
    ]
    assert phase_summaries == [
        {
            "phase": "vm-interfaces",
            "created": 1,
            "failed": 1,
            "message": (
                "VM interface reconciliation completed with partial failures: "
                "1 succeeded, 1 failed."
            ),
        }
    ]
    assert any("partial failures" in message for message in warning_messages)


def test_create_only_vm_interfaces_truncates_guest_interface_name(monkeypatch):
    captured_payloads: list[dict[str, object]] = []
    warning_messages: list[str] = []
    long_guest_name = "veth" + ("x" * 80)

    from proxbox_api.services.sync import network as network_module

    def _capture_warning(message, *args, **_kwargs):
        warning_messages.append(message % args if args else message)

    cluster_status = [SimpleNamespace(name="lab")]
    cluster_resources = [
        {
            "lab": [
                {"type": "qemu", "name": "vm01", "node": "pve01", "vmid": 101},
            ]
        }
    ]

    def _fake_get_vm_config(**_kwargs):
        return {"agent": "1", "net0": "virtio=aa:bb:cc:dd:ee:ff"}

    async def _fake_guest_interfaces(*_args, **_kwargs):
        return [{"name": long_guest_name, "mac_address": "aa:bb:cc:dd:ee:ff"}]

    async def _fake_load_snapshot(_nb):
        return [{"id": 55, "name": "vm01", "custom_fields": {"proxmox_vm_id": 101}}]

    async def _fake_resolve_cluster_id(*_args, **_kwargs):
        return None

    async def _fake_bulk_reconcile(_nb, payloads, **_kwargs):
        captured_payloads.extend(payloads)
        name = payloads[0]["name"]
        return ([{"id": 66, "name": name, "virtual_machine": 55}], {(name, 55): 66})

    async def _fake_guest_sidecars(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)
    monkeypatch.setattr(
        sync_vm,
        "get_qemu_guest_agent_network_interfaces",
        _fake_guest_interfaces,
    )
    monkeypatch.setattr(sync_vm, "resolve_vm_sync_concurrency", lambda: 1)
    monkeypatch.setattr(sync_vm, "_load_netbox_virtual_machine_snapshot", _fake_load_snapshot)
    monkeypatch.setattr(sync_vm, "resolve_netbox_cluster_id_by_name", _fake_resolve_cluster_id)
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.bulk_reconcile_vm_interfaces",
        _fake_bulk_reconcile,
    )
    monkeypatch.setattr(sync_vm, "reconcile_guest_vm_interfaces", _fake_guest_sidecars)
    monkeypatch.setattr(network_module.logger, "warning", _capture_warning)

    result = asyncio.run(
        create_only_vm_interfaces(
            netbox_session=SimpleNamespace(client=object()),
            pxs=[SimpleNamespace(name="lab")],
            cluster_status=cluster_status,
            cluster_resources=cluster_resources,
            custom_fields=[],
            tag=SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
            sync_mac=False,
            vm_interface_sync_strategy="legacy_rename",
        )
    )

    assert len(captured_payloads) == 1
    assert len(captured_payloads[0]["name"]) == 64
    assert captured_payloads[0]["name"] == long_guest_name[:64]
    assert result[0]["interface"]["name"] == long_guest_name[:64]
    assert any("Truncated VM interface name" in message for message in warning_messages)


def test_normalize_vm_interface_name_strips_control_characters():
    from proxbox_api.services.sync.network import normalize_vm_interface_name

    assert normalize_vm_interface_name("\x00ens\x1f18\x7f", fallback="net0") == "ens18"


def test_normalize_vm_interface_name_uses_fallback_when_empty_after_stripping():
    from proxbox_api.services.sync.network import normalize_vm_interface_name

    assert normalize_vm_interface_name("\x00\n\x7f", fallback="net9") == "net9"


def test_create_only_vm_ip_addresses_uses_normalized_interface_name(monkeypatch):
    captured_payloads: list[dict[str, object]] = []
    long_guest_name = "veth" + ("x" * 80)
    normalized_guest_name = long_guest_name[:64]

    cluster_status = [SimpleNamespace(name="lab")]
    cluster_resources = [
        {
            "lab": [
                {"type": "qemu", "name": "vm01", "node": "pve01", "vmid": 101},
            ]
        }
    ]

    def _fake_get_vm_config(**_kwargs):
        return {"agent": "1", "net0": "virtio=aa:bb:cc:dd:ee:ff"}

    async def _fake_guest_interfaces(*_args, **_kwargs):
        return [
            {
                "name": long_guest_name,
                "mac_address": "aa:bb:cc:dd:ee:ff",
                "ip_addresses": [{"ip_address": "192.0.2.10", "prefix": 24}],
            }
        ]

    async def _fake_load_snapshot(_nb):
        return [{"id": 55, "name": "vm01", "custom_fields": {"proxmox_vm_id": 101}}]

    async def _fake_resolve_cluster_id(*_args, **_kwargs):
        return None

    async def _fake_rest_list(_nb, path, query=None):
        if path == "/api/virtualization/interfaces/":
            assert query == {"virtual_machine_id": 55, "limit": 500}
            return [{"id": 66, "name": normalized_guest_name}]
        return []

    async def _fake_bulk_reconcile_ips(_nb, payloads, **_kwargs):
        captured_payloads.extend(payloads)
        return [
            {
                "id": 10,
                "address": "192.0.2.10/24",
                "assigned_object_id": 66,
            }
        ]

    async def _fake_rest_first(*_args, **_kwargs):
        return None

    async def _fake_resolve_vm_dns_name(**_kwargs):
        return None

    async def _fake_guest_sidecars(*_args, **_kwargs):
        return None

    async def _fake_cleanup_stale_ips(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)
    monkeypatch.setattr(
        sync_vm,
        "get_qemu_guest_agent_network_interfaces",
        _fake_guest_interfaces,
    )
    monkeypatch.setattr(sync_vm, "_resolve_vm_dns_name", _fake_resolve_vm_dns_name)
    monkeypatch.setattr(sync_vm, "resolve_vm_sync_concurrency", lambda: 1)
    monkeypatch.setattr(sync_vm, "_load_netbox_virtual_machine_snapshot", _fake_load_snapshot)
    monkeypatch.setattr(sync_vm, "resolve_netbox_cluster_id_by_name", _fake_resolve_cluster_id)
    monkeypatch.setattr(sync_vm, "reconcile_guest_vm_interfaces", _fake_guest_sidecars)
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _fake_rest_list)
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_first_async", _fake_rest_first)
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.bulk_reconcile_vm_interface_ips",
        _fake_bulk_reconcile_ips,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.cleanup_stale_ips_for_interface",
        _fake_cleanup_stale_ips,
    )

    result = asyncio.run(
        create_only_vm_ip_addresses(
            netbox_session=SimpleNamespace(client=object()),
            pxs=[SimpleNamespace(name="lab")],
            cluster_status=cluster_status,
            cluster_resources=cluster_resources,
            custom_fields=[],
            tag=SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
            vm_interface_sync_strategy="legacy_rename",
        )
    )

    assert len(captured_payloads) == 1
    payload = captured_payloads[0]
    last_updated = payload["custom_fields"]["proxmox_last_updated"]
    assert isinstance(last_updated, str)
    assert payload == {
        "address": "192.0.2.10/24",
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": 66,
        "status": "active",
        "dns_name": "",
        "tags": [{"name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        "custom_fields": {"proxmox_last_updated": last_updated},
    }
    assert result == [{"ip_id": 10, "address": "192.0.2.10/24"}]


def test_standalone_vm_interfaces_response_surfaces_warnings(monkeypatch):
    warning = {
        "phase": "vm-interfaces",
        "succeeded": 1,
        "failed": 1,
        "requested": 2,
        "message": "VM interface reconciliation completed with partial failures.",
    }

    async def _fake_create_only_vm_interfaces(**_kwargs):
        return SyncResultList([{"id": 66}], warnings=[warning])

    monkeypatch.setattr(sync_vm, "create_only_vm_interfaces", _fake_create_only_vm_interfaces)

    body = asyncio.run(
        read_vm.create_virtual_machines_interfaces(
            netbox_session=SimpleNamespace(client=object()),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            custom_fields=[],
            tag=SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
            use_guest_agent_interface_name=True,
            vm_interface_sync_strategy="guest_os_model",
            ignore_ipv6_link_local_addresses=True,
            primary_ip_preference="ipv4",
            sync_vm_interface_macs=False,
            overwrite_flags=SyncOverwriteFlags(),
        )
    )

    assert body == {"vm_interfaces": [{"id": 66}], "count": 1, "warnings": [warning]}


def test_standalone_vm_interfaces_stream_complete_surfaces_warnings(monkeypatch):
    warning = {
        "phase": "vm-interfaces",
        "succeeded": 1,
        "failed": 1,
        "requested": 2,
        "message": "VM interface reconciliation completed with partial failures.",
    }

    async def _fake_create_only_vm_interfaces(**_kwargs):
        return SyncResultList([{"id": 66}], warnings=[warning])

    monkeypatch.setattr(sync_vm, "create_only_vm_interfaces", _fake_create_only_vm_interfaces)

    response = asyncio.run(
        read_vm.create_virtual_machines_interfaces_stream(
            netbox_session=SimpleNamespace(client=object()),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            custom_fields=[],
            tag=SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
            use_guest_agent_interface_name=True,
            vm_interface_sync_strategy="guest_os_model",
            ignore_ipv6_link_local_addresses=True,
            primary_ip_preference="ipv4",
            sync_vm_interface_macs=False,
            overwrite_flags=SyncOverwriteFlags(),
        )
    )

    async def _collect_body() -> str:
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    stream_body = asyncio.run(_collect_body())
    complete_payloads = []
    for frame in stream_body.strip().split("\n\n"):
        lines = frame.splitlines()
        if "event: complete" not in lines:
            continue
        data_line = next(line for line in lines if line.startswith("data: "))
        complete_payloads.append(json.loads(data_line.removeprefix("data: ")))

    assert complete_payloads[-1]["result"] == {"count": 1, "warnings": [warning]}


def test_full_update_continues_to_ip_stage_after_vm_interface_warning(monkeypatch):
    ip_stage_called = False

    async def _fake_devices(**kwargs):
        return []

    async def _fake_vms(**kwargs):
        return []

    async def _fake_storage(**kwargs):
        return []

    async def _fake_disks(**kwargs):
        return {"count": 0, "created": 0, "updated": 0, "skipped": 0}

    async def _fake_backups(**kwargs):
        return []

    async def _fake_snapshots(**kwargs):
        return {"count": 0, "created": 0, "skipped": 0}

    async def _fake_task_history(**kwargs):
        return {"count": 0, "created": 0, "skipped": 0}

    async def _fake_node_interfaces(**kwargs):
        return []

    async def _fake_vm_interfaces(**kwargs):
        return SyncResultList(
            [{"id": 66}],
            warnings=[
                {
                    "phase": "vm-interfaces",
                    "succeeded": 1,
                    "failed": 1,
                    "requested": 2,
                    "message": "VM interface reconciliation completed with partial failures.",
                }
            ],
        )

    async def _fake_vm_ip_addresses(**kwargs):
        nonlocal ip_stage_called
        ip_stage_called = True
        return [{"ip_id": 10, "address": "192.0.2.10/24"}]

    async def _fake_replications(**kwargs):
        return {"created": 0, "updated": 0, "errors": 0}

    async def _fake_backup_routines(**kwargs):
        return {"created": 0, "updated": 0, "errors": 0}

    monkeypatch.setattr("proxbox_api.app.full_update.create_proxmox_devices", _fake_devices)
    monkeypatch.setattr("proxbox_api.app.full_update.create_virtual_machines", _fake_vms)
    monkeypatch.setattr("proxbox_api.app.full_update.create_storages", _fake_storage)
    monkeypatch.setattr("proxbox_api.app.full_update.create_virtual_disks", _fake_disks)
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_all_virtual_machine_backups", _fake_backups
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_all_virtual_machine_snapshots", _fake_snapshots
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.sync_all_virtual_machine_task_histories", _fake_task_history
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_all_device_interfaces", _fake_node_interfaces
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_only_vm_interfaces", _fake_vm_interfaces
    )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_only_vm_ip_addresses", _fake_vm_ip_addresses
    )
    monkeypatch.setattr("proxbox_api.app.full_update.sync_all_replications", _fake_replications)
    monkeypatch.setattr(
        "proxbox_api.app.full_update.sync_all_backup_routines", _fake_backup_routines
    )

    body = asyncio.run(
        full_update_sync(
            netbox_session=object(),
            _sync_deps=BootstrapStatus(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            custom_fields=[],
            tag=type("Tag", (), {"id": 1})(),
        )
    )

    assert ip_stage_called is True
    assert body["vm_interfaces_count"] == 1
    assert body["vm_ip_addresses"] == [{"ip_id": 10, "address": "192.0.2.10/24"}]
    assert body["warnings"][0]["phase"] == "vm-interfaces"


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
