from __future__ import annotations

import asyncio
from types import SimpleNamespace

from proxbox_api.routes.virtualization.virtual_machines import create_virtual_machines
from proxbox_api.routes.virtualization.virtual_machines.sync_vm import (
    create_only_vm_interfaces,
    create_only_vm_ip_addresses,
)


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


def _install_common_sync_patches(  # noqa: C901
    monkeypatch,
    *,
    vm_config: dict,
    ip_payloads: list[dict],
    interface_payloads: list[dict] | None = None,
):
    async def _fake_get_vm_config(**kwargs):
        return vm_config

    async def _fake_ensure_obj(*args, **kwargs):
        return SimpleNamespace(id=11)

    async def _fake_reconcile(_nb, path, lookup, payload, **kwargs):
        if path == "/api/virtualization/virtual-machines/":
            return {"id": 55, "name": "vm01"}
        if path == "/api/virtualization/interfaces/":
            if interface_payloads is not None and payload.get("type") != "bridge":
                interface_payloads.append(payload)
            return {"id": 66, "name": payload.get("name")}
        if path == "/api/ipam/ip-addresses/":
            ip_payloads.append(payload)
            return {"id": 77, "address": payload.get("address")}
        if path == "/api/dcim/device-roles/":
            return SimpleNamespace(id=33, name=payload.get("name"))
        return {"id": 99}

    async def _fake_rest_list(_nb, path, **kwargs):
        if path == "/api/plugins/proxbox/storage/":
            return []
        return []

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
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.rest_list_async",
        _fake_rest_list,
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


def test_vm_sync_uses_guest_agent_interface_name_by_default(monkeypatch):
    data = _vm_sync_inputs(
        {
            "agent": 1,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        }
    )
    ip_payloads: list[dict] = []
    interface_payloads: list[dict] = []
    _install_common_sync_patches(
        monkeypatch,
        vm_config=data["vm_config"],
        ip_payloads=ip_payloads,
        interface_payloads=interface_payloads,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.get_qemu_guest_agent_network_interfaces",
        lambda *args, **kwargs: [
            {
                "name": "ens18",
                "mac_address": "AA:BB:CC:DD:EE:FF",
                "ip_addresses": [],
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
    assert interface_payloads and interface_payloads[0]["name"] == "ens18"


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


def test_vm_sync_can_disable_guest_agent_interface_name(monkeypatch):
    data = _vm_sync_inputs(
        {
            "agent": 1,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        }
    )
    ip_payloads: list[dict] = []
    interface_payloads: list[dict] = []
    _install_common_sync_patches(
        monkeypatch,
        vm_config=data["vm_config"],
        ip_payloads=ip_payloads,
        interface_payloads=interface_payloads,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.get_qemu_guest_agent_network_interfaces",
        lambda *args, **kwargs: [
            {
                "name": "ens18",
                "mac_address": "AA:BB:CC:DD:EE:FF",
                "ip_addresses": [],
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
            use_guest_agent_interface_name=False,
        )
    )
    assert len(result) == 1
    assert interface_payloads and interface_payloads[0]["name"] == "net0"


def test_vm_sync_populates_task_history(monkeypatch):
    data = _vm_sync_inputs(
        {
            "agent": 0,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        }
    )
    ip_payloads: list[dict] = []
    _install_common_sync_patches(
        monkeypatch,
        vm_config=data["vm_config"],
        ip_payloads=ip_payloads,
    )
    task_history_calls: list[dict] = []

    async def _fake_task_history(**kwargs):
        task_history_calls.append(kwargs)
        return 2

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.sync_virtual_machine_task_history",
        _fake_task_history,
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
    assert task_history_calls and task_history_calls[0]["virtual_machine_id"] == 55
    assert task_history_calls[0]["vm_type"] == "qemu"


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


def test_vm_sync_marks_missing_primary_ip_as_warning(monkeypatch):
    data = _vm_sync_inputs({"agent": 0, "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0"})
    ip_payloads: list[dict] = []
    _install_common_sync_patches(monkeypatch, vm_config=data["vm_config"], ip_payloads=ip_payloads)

    class _WebSocket:
        def __init__(self):
            self.payloads: list[dict] = []

        async def send_json(self, payload: dict):
            self.payloads.append(payload)

    websocket = _WebSocket()

    result = asyncio.run(
        create_virtual_machines(
            netbox_session=data["netbox_session"],
            pxs=data["pxs"],
            cluster_status=data["cluster_status"],
            cluster_resources=data["cluster_resources"],
            custom_fields=data["custom_fields"],
            tag=data["tag"],
            websocket=websocket,
            use_websocket=True,
        )
    )

    assert len(result) == 1
    warning_payloads = [
        payload
        for payload in websocket.payloads
        if payload.get("object") == "virtual_machine"
        and isinstance(payload.get("data"), dict)
        and payload["data"].get("warning")
    ]
    assert warning_payloads
    assert warning_payloads[0]["data"]["completed"] is True
    assert warning_payloads[0]["data"]["status"] == "warning"
    assert "No IP address found; primary IP not set." in warning_payloads[0]["data"]["warning"]
    assert not ip_payloads


def test_vm_sync_ignore_ipv6_link_local_true_skips_fe80(monkeypatch):
    data = _vm_sync_inputs(
        {
            "agent": 1,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
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
                    {"ip_address": "fe80::1", "prefix": 64, "ip_address_type": "ipv6"}
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
            ignore_ipv6_link_local_addresses=True,
        )
    )
    assert len(result) == 1
    assert ip_payloads == []


def test_vm_sync_ignore_ipv6_link_local_false_includes_fe80(monkeypatch):
    data = _vm_sync_inputs(
        {
            "agent": 1,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
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
                    {"ip_address": "fe80::1", "prefix": 64, "ip_address_type": "ipv6"}
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
            ignore_ipv6_link_local_addresses=False,
        )
    )
    assert len(result) == 1
    assert ip_payloads and ip_payloads[0]["address"] == "fe80::1/64"


def test_vm_only_interface_sync_uses_resolved_netbox_vm_id(monkeypatch):
    data = _vm_sync_inputs(
        {
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        }
    )
    captured_vm_ids: list[int | None] = []

    def _fake_get_vm_config(*args, **kwargs):
        return data["vm_config"]

    async def _fake_sync_vm_interface_and_ip(**kwargs):
        captured_vm_ids.append(kwargs["virtual_machine"].get("id"))
        return {"id": 66, "ip_id": 77, "ip_address": "10.0.0.50/24"}

    async def _fake_resolve_netbox_vm(*args, **kwargs):
        return {"id": 55, "name": "vm01"}

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.get_vm_config",
        _fake_get_vm_config,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.resolve_vm_sync_concurrency",
        lambda: 1,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._resolve_netbox_virtual_machine_by_proxmox_id",
        _fake_resolve_netbox_vm,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.sync_vm_interface_and_ip",
        _fake_sync_vm_interface_and_ip,
    )

    result = asyncio.run(
        create_only_vm_interfaces(
            netbox_session=data["netbox_session"],
            pxs=data["pxs"],
            cluster_status=data["cluster_status"],
            cluster_resources=data["cluster_resources"],
            custom_fields=data["custom_fields"],
            tag=data["tag"],
        )
    )

    assert result == [{"id": 66, "ip_id": 77, "ip_address": "10.0.0.50/24"}]
    assert captured_vm_ids == [55]


def test_vm_only_ip_sync_uses_resolved_netbox_vm_id(monkeypatch):
    data = _vm_sync_inputs(
        {
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,ip=10.0.0.20/24",
        }
    )
    captured_vm_ids: list[int | None] = []

    def _fake_get_vm_config(*args, **kwargs):
        return data["vm_config"]

    async def _fake_sync_vm_interface_and_ip(**kwargs):
        captured_vm_ids.append(kwargs["virtual_machine"].get("id"))
        return {"id": 66, "ip_id": 77, "ip_address": "10.0.0.20/24"}

    async def _fake_resolve_netbox_vm(*args, **kwargs):
        return {"id": 55, "name": "vm01"}

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.get_vm_config",
        _fake_get_vm_config,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.resolve_vm_sync_concurrency",
        lambda: 1,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._resolve_netbox_virtual_machine_by_proxmox_id",
        _fake_resolve_netbox_vm,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.sync_vm_interface_and_ip",
        _fake_sync_vm_interface_and_ip,
    )

    result = asyncio.run(
        create_only_vm_ip_addresses(
            netbox_session=data["netbox_session"],
            pxs=data["pxs"],
            cluster_status=data["cluster_status"],
            cluster_resources=data["cluster_resources"],
            custom_fields=data["custom_fields"],
            tag=data["tag"],
        )
    )

    assert result == [
        {
            "ip_id": 77,
            "address": "10.0.0.20/24",
            "interface_name": "net0",
            "interface_id": 66,
            "vm": "vm01",
        }
    ]
    assert captured_vm_ids == [55]
