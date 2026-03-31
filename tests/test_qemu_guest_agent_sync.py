from __future__ import annotations

import asyncio
from types import SimpleNamespace

from proxbox_api.routes.virtualization.virtual_machines import create_virtual_machines


def _vm_sync_inputs(vm_config: dict):
    cluster_status = [
        SimpleNamespace(
            name="lab",
            mode="cluster",
            node_list=[SimpleNamespace(name="pve01")],
        )
    ]
    cluster_resources = [
        {
            "lab": [
                {
                    "type": "qemu",
                    "name": "vm01",
                    "node": "pve01",
                    "vmid": 101,
                    "status": "running",
                    "maxcpu": 2,
                    "maxmem": 4_294_967_296,
                    "maxdisk": 53_687_091_200,
                }
            ]
        }
    ]
    return {
        "netbox_session": SimpleNamespace(
            client=object(),
            extras=SimpleNamespace(journal_entries=object()),
        ),
        "pxs": [SimpleNamespace(name="lab", session=object())],
        "cluster_status": cluster_status,
        "cluster_resources": cluster_resources,
        "custom_fields": [],
        "tag": SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
        "vm_config": vm_config,
    }


def _install_common_sync_patches(monkeypatch, *, vm_config: dict, ip_payloads: list[dict]):
    async def _fake_get_vm_config(**kwargs):
        return vm_config

    async def _fake_ensure_obj(*args, **kwargs):
        return SimpleNamespace(id=11)

    async def _fake_reconcile(_nb, path, lookup, payload, **kwargs):
        if path == "/api/virtualization/virtual-machines/":
            return {"id": 55, "name": "vm01"}
        if path == "/api/virtualization/interfaces/":
            return {"id": 66, "name": payload.get("name")}
        if path == "/api/ipam/ip-addresses/":
            ip_payloads.append(payload)
            return {"id": 77, "address": payload.get("address")}
        if path == "/api/dcim/device-roles/":
            return SimpleNamespace(id=33, name=payload.get("name"))
        return {"id": 99}

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.get_vm_config",
        _fake_get_vm_config,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.resolve_vm_sync_concurrency",
        lambda: 1,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._ensure_cluster_type",
        _fake_ensure_obj,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._ensure_cluster",
        _fake_ensure_obj,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._ensure_manufacturer",
        _fake_ensure_obj,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._ensure_device_type",
        _fake_ensure_obj,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._ensure_proxmox_node_role",
        _fake_ensure_obj,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._ensure_site",
        _fake_ensure_obj,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._ensure_device",
        _fake_ensure_obj,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.rest_reconcile_async",
        _fake_reconcile,
    )


def test_vm_sync_prefers_guest_agent_ip(monkeypatch):
    data = _vm_sync_inputs(
        {
            "agent": 1,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,ip=10.0.0.20/24",
        }
    )
    ip_payloads: list[dict] = []
    _install_common_sync_patches(monkeypatch, vm_config=data["vm_config"], ip_payloads=ip_payloads)
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.get_qemu_guest_agent_network_interfaces",
        lambda *args, **kwargs: [
            {
                "name": "ens18",
                "mac_address": "AA:BB:CC:DD:EE:FF",
                "ip_addresses": [
                    {"ip_address": "10.0.0.50", "prefix": 24, "ip_address_type": "ipv4"}
                ],
            }
        ],
    )

    result = asyncio.run(
        create_virtual_machines(
            netbox_session=data["netbox_session"],
            pxs=data["pxs"],
            cluster_status=data["cluster_status"],
            cluster_resources=data["cluster_resources"],
            custom_fields=data["custom_fields"],
            tag=data["tag"],
        )
    )
    assert len(result) == 1
    assert ip_payloads and ip_payloads[0]["address"] == "10.0.0.50/24"


def test_vm_sync_falls_back_to_config_when_guest_agent_unavailable(monkeypatch):
    data = _vm_sync_inputs(
        {
            "agent": 1,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,ip=10.0.0.21/24",
        }
    )
    ip_payloads: list[dict] = []
    _install_common_sync_patches(monkeypatch, vm_config=data["vm_config"], ip_payloads=ip_payloads)
    helper_calls = {"count": 0}

    def _fake_guest_helper(*args, **kwargs):
        helper_calls["count"] += 1
        return []

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.get_qemu_guest_agent_network_interfaces",
        _fake_guest_helper,
    )

    result = asyncio.run(
        create_virtual_machines(
            netbox_session=data["netbox_session"],
            pxs=data["pxs"],
            cluster_status=data["cluster_status"],
            cluster_resources=data["cluster_resources"],
            custom_fields=data["custom_fields"],
            tag=data["tag"],
        )
    )
    assert len(result) == 1
    assert helper_calls["count"] == 1
    assert ip_payloads and ip_payloads[0]["address"] == "10.0.0.21/24"


def test_vm_sync_skips_guest_agent_call_when_disabled(monkeypatch):
    data = _vm_sync_inputs(
        {
            "agent": 0,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,ip=10.0.0.22/24",
        }
    )
    ip_payloads: list[dict] = []
    _install_common_sync_patches(monkeypatch, vm_config=data["vm_config"], ip_payloads=ip_payloads)
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.get_qemu_guest_agent_network_interfaces",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    result = asyncio.run(
        create_virtual_machines(
            netbox_session=data["netbox_session"],
            pxs=data["pxs"],
            cluster_status=data["cluster_status"],
            cluster_resources=data["cluster_resources"],
            custom_fields=data["custom_fields"],
            tag=data["tag"],
        )
    )
    assert len(result) == 1
    assert ip_payloads and ip_payloads[0]["address"] == "10.0.0.22/24"
