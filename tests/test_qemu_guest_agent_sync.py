"""Tests for QEMU guest-agent driven VM sync behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from proxbox_api.exception import ProxboxException
from proxbox_api.routes.virtualization.virtual_machines import create_virtual_machines, sync_vm
from proxbox_api.routes.virtualization.virtual_machines.sync_vm import (
    create_only_vm_interfaces,
    create_only_vm_ip_addresses,
)
from proxbox_api.schemas.sync import SyncBehaviorFlags
from proxbox_api.services.proxmox_helpers import GuestAgentFetchResult
from proxbox_api.services.sync import sync_state_reader


@pytest.fixture(autouse=True)
def enable_legacy_custom_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "proxbox_api.services.custom_fields.get_plugin_bool",
        lambda settings_key, default=False: (
            True if settings_key == "custom_fields_enabled" else default
        ),
    )
    kwdefaults = getattr(create_virtual_machines, "__kwdefaults__", None)
    if isinstance(kwdefaults, dict):
        monkeypatch.setitem(
            kwdefaults,
            "behavior_flags",
            SyncBehaviorFlags(custom_fields_enabled=True),
        )

    async def _legacy_vm_snapshot_bridge(
        netbox_session,
        path,
        *,
        base_query=None,
        page_size=200,
        max_offset=None,
    ):
        del max_offset
        query = dict(base_query or {})
        query["limit"] = page_size
        query.setdefault("offset", 0)
        return await sync_vm.rest_list_async(netbox_session, path, query=query)

    # Production now uses the shared exhaustive paginator. These focused tests
    # provide a one-page path-aware fake, so bridge the new dependency to it.
    monkeypatch.setattr(sync_vm, "rest_list_paginated_async", _legacy_vm_snapshot_bridge)


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
    first_queries: list[dict] | None = None,
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
        if path == "/api/virtualization/virtual-machine-types/":
            return SimpleNamespace(id=99)
        return SimpleNamespace(id=99)

    async def _fake_rest_list(_nb, path, **kwargs):
        query = kwargs.get("query", {})
        if path == "/api/plugins/proxbox/storage/":
            return []
        if path == "/api/virtualization/interfaces/" and query.get("name"):
            return []
        return []

    async def _fake_rest_first(_nb, path, **kwargs):
        query = kwargs.get("query", {})
        if first_queries is not None:
            first_queries.append({"path": path, "query": query})
        if path == "/api/virtualization/interfaces/" and query.get("name"):
            return None
        return None

    async def _fake_guest_plugin_create(_nb, path, payload, **kwargs):
        _ = kwargs
        if path == "/api/plugins/proxbox/guest-vm-interfaces/":
            return {"id": 901, **payload}
        if path == "/api/plugins/proxbox/guest-vm-interface-addresses/":
            return {"id": 902, **payload}
        return {"id": 999, **payload}

    async def _fake_guest_plugin_patch(_nb, path, record_id, payload, **kwargs):
        _ = kwargs
        return {"id": record_id, **payload}

    async def _fake_resolve_netbox_vm(*args, **kwargs):
        return {"id": 55, "name": "vm01"}

    async def _fake_task_history(**_kwargs):
        return {"count": 1, "created": 0, "skipped": 0}

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
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.ensure_vm_type",
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
    monkeypatch.setattr(
        "proxbox_api.services.sync.sync_state_reader.rest_list_async",
        _fake_rest_list,
    )
    sync_state_reader.reset_sidecar_reader_availability_cache()
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.rest_first_async",
        _fake_rest_first,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._resolve_netbox_virtual_machine_by_proxmox_id",
        _fake_resolve_netbox_vm,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.sync_all_virtual_machine_task_histories",
        _fake_task_history,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.rest_reconcile_async",
        _fake_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_first_async",
        _fake_rest_first,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_create_async",
        _fake_guest_plugin_create,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_patch_async",
        _fake_guest_plugin_patch,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.rest_first_async",
        _fake_rest_first,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.rest_list_async",
        _fake_rest_list,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.ip_ownership.rest_reconcile_async",
        _fake_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.ip_ownership.rest_list_async",
        _fake_rest_list,
    )


def test_vm_sync_fetches_tag_color_map_once_per_cluster_under_concurrency(monkeypatch):
    data = _vm_sync_inputs({"tags": "critical;production"})
    base_resource = data["cluster_resources"][0]["lab"][0]
    data["cluster_resources"][0]["lab"] = [
        {**base_resource, "name": f"vm{i}", "vmid": 100 + i} for i in range(4)
    ]
    ip_payloads: list[dict] = []
    _install_common_sync_patches(monkeypatch, vm_config=data["vm_config"], ip_payloads=ip_payloads)
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.resolve_vm_sync_concurrency",
        lambda: 4,
    )

    fetch_calls: list[object] = []
    resolved_color_maps: list[dict[str, str] | None] = []

    async def _fake_fetch_tag_color_map(px):
        fetch_calls.append(px)
        await asyncio.sleep(0.01)
        return {"critical": "ff5722"}

    async def _fake_resolve_proxmox_tag_ids(_nb, _raw, *, color_map=None):
        resolved_color_maps.append(color_map)
        return [201]

    async def _fake_rest_create(_nb, _path, payload, **kwargs):
        custom_fields = payload.get("custom_fields") if isinstance(payload, dict) else None
        lookup = kwargs.get("lookup") or {}
        vmid = int(
            (custom_fields.get("proxmox_vm_id") if isinstance(custom_fields, dict) else None)
            or lookup["cf_proxmox_vm_id"]
        )
        return {"id": 1000 + vmid, **payload}

    async def _fake_task_history(**_kwargs):
        return 0

    stamp_calls: list[tuple[int, str]] = []

    async def _fake_stamp(_nb, vm_record, run_id):
        stamp_calls.append((int(vm_record["id"]), str(run_id)))

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.fetch_tag_color_map",
        _fake_fetch_tag_color_map,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.resolve_proxmox_tag_ids",
        _fake_resolve_proxmox_tag_ids,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.rest_create_async",
        _fake_rest_create,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.sync_all_virtual_machine_task_histories",
        _fake_task_history,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.stamp_vm_last_run_id",
        _fake_stamp,
    )

    result = asyncio.run(
        create_virtual_machines(
            netbox_session=data["netbox_session"],
            pxs=data["pxs"],
            cluster_status=data["cluster_status"],
            cluster_resources=data["cluster_resources"],
            custom_fields=data["custom_fields"],
            tag=data["tag"],
            sync_vm_network=False,
            run_id="issue-519-run",
        )
    )

    assert len(result) == 4
    assert len(fetch_calls) == 1
    assert resolved_color_maps == [{"critical": "ff5722"}] * 4
    assert stamp_calls == [
        (1100, "issue-519-run"),
        (1101, "issue-519-run"),
        (1102, "issue-519-run"),
        (1103, "issue-519-run"),
    ]


def test_vm_sync_prefers_guest_agent_ip(monkeypatch):
    data = _vm_sync_inputs(
        {
            "agent": 1,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,ip=10.0.0.20/24",
        }
    )
    ip_payloads: list[dict] = []
    _install_common_sync_patches(monkeypatch, vm_config=data["vm_config"], ip_payloads=ip_payloads)

    async def _fake_guest_ifaces_with_ip(*args, **kwargs):
        return GuestAgentFetchResult(
            interfaces=[
                {
                    "name": "ens18",
                    "mac_address": "AA:BB:CC:DD:EE:FF",
                    "ip_addresses": [
                        {"ip_address": "10.0.0.50", "prefix": 24, "ip_address_type": "ipv4"}
                    ],
                }
            ],
            diagnostic=None,
        )

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.fetch_qemu_guest_agent_network_interfaces",
        _fake_guest_ifaces_with_ip,
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


def test_vm_sync_guest_os_model_keeps_core_interface_name_by_default(monkeypatch):
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

    async def _fake_guest_ifaces_no_ip(*args, **kwargs):
        return GuestAgentFetchResult(
            interfaces=[{"name": "ens18", "mac_address": "AA:BB:CC:DD:EE:FF", "ip_addresses": []}],
            diagnostic=None,
        )

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.fetch_qemu_guest_agent_network_interfaces",
        _fake_guest_ifaces_no_ip,
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
    assert interface_payloads and interface_payloads[0]["name"] == "net0"


def test_vm_sync_legacy_rename_uses_guest_agent_interface_name(monkeypatch):
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

    async def _fake_guest_ifaces_no_ip(*args, **kwargs):
        return GuestAgentFetchResult(
            interfaces=[{"name": "ens18", "mac_address": "AA:BB:CC:DD:EE:FF", "ip_addresses": []}],
            diagnostic=None,
        )

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.fetch_qemu_guest_agent_network_interfaces",
        _fake_guest_ifaces_no_ip,
    )

    result = asyncio.run(
        create_virtual_machines(
            netbox_session=data["netbox_session"],
            pxs=data["pxs"],
            cluster_status=data["cluster_status"],
            cluster_resources=data["cluster_resources"],
            custom_fields=data["custom_fields"],
            tag=data["tag"],
            vm_interface_sync_strategy="legacy_rename",
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

    async def _fake_guest_helper(*args, **kwargs):
        helper_calls["count"] += 1
        return GuestAgentFetchResult(interfaces=[], diagnostic=None)

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.fetch_qemu_guest_agent_network_interfaces",
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

    async def _fake_guest_ifaces_no_ip_2(*args, **kwargs):
        return GuestAgentFetchResult(
            interfaces=[{"name": "ens18", "mac_address": "AA:BB:CC:DD:EE:FF", "ip_addresses": []}],
            diagnostic=None,
        )

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.fetch_qemu_guest_agent_network_interfaces",
        _fake_guest_ifaces_no_ip_2,
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
        return {"count": 1, "created": 2, "skipped": 0}

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.sync_all_virtual_machine_task_histories",
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
    assert len(task_history_calls) == 1
    assert task_history_calls[0]["netbox_vm_ids"] == [55]


def test_rest_selected_vm_sync_keeps_reused_vmid_on_requested_owner(monkeypatch):
    data = _vm_sync_inputs(
        {
            "agent": 0,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        }
    )
    data["pxs"][0].db_endpoint_id = 11
    data["pxs"].append(
        SimpleNamespace(
            name="other-lab",
            cluster_name="other-lab",
            db_endpoint_id=22,
            session=object(),
        )
    )
    data["cluster_status"].append(
        SimpleNamespace(
            name="other-lab",
            mode="cluster",
            node_list=[SimpleNamespace(name="pve02")],
        )
    )
    data["cluster_resources"].append(
        {
            "other-lab": [
                {
                    **data["cluster_resources"][0]["lab"][0],
                    "node": "pve02",
                }
            ]
        }
    )
    _install_common_sync_patches(
        monkeypatch,
        vm_config=data["vm_config"],
        ip_payloads=[],
    )
    fetched_nodes: list[str] = []
    task_history_calls: list[dict[str, object]] = []

    async def _selected_vm_list(_nb, path, *, query=None):
        assert path == "/api/virtualization/virtual-machines/"
        assert query == {"id": ["55"]}
        return [
            {
                "id": 55,
                "name": "vm01",
                "cluster": {"id": 1, "name": "LAB"},
                "custom_fields": {
                    "proxmox_endpoint_id": 11,
                    "proxmox_vm_id": 101,
                    "proxmox_vm_type": "qemu",
                },
            }
        ]

    async def _fake_get_vm_config(**kwargs):
        fetched_nodes.append(str(kwargs["node"]))
        return data["vm_config"]

    async def _fake_task_history(**kwargs):
        task_history_calls.append(kwargs)
        return {"count": 1, "created": 0, "skipped": 0}

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _selected_vm_list)
    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)
    monkeypatch.setattr(sync_vm, "sync_all_virtual_machine_task_histories", _fake_task_history)

    result = asyncio.run(
        create_virtual_machines(
            netbox_session=data["netbox_session"],
            pxs=data["pxs"],
            cluster_status=data["cluster_status"],
            cluster_resources=data["cluster_resources"],
            custom_fields=data["custom_fields"],
            tag=data["tag"],
            netbox_vm_ids="55",
        )
    )

    assert len(result) == 1
    assert fetched_nodes == ["pve01"]
    assert len(task_history_calls) == 1
    assert task_history_calls[0]["netbox_vm_ids"] == [55]


def test_vm_sync_propagates_owned_task_history_fatal_error(monkeypatch):
    data = _vm_sync_inputs(
        {
            "agent": 0,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        }
    )
    _install_common_sync_patches(monkeypatch, vm_config=data["vm_config"], ip_payloads=[])

    async def _fatal_task_history(**_kwargs):
        raise ProxboxException(
            message="Unable to verify VM identity for task-history sync",
            http_status_code=502,
        )

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.sync_all_virtual_machine_task_histories",
        _fatal_task_history,
    )

    with pytest.raises(ProxboxException, match="Unable to verify VM identity") as exc_info:
        asyncio.run(
            create_virtual_machines(
                netbox_session=data["netbox_session"],
                pxs=data["pxs"],
                cluster_status=data["cluster_status"],
                cluster_resources=data["cluster_resources"],
                custom_fields=data["custom_fields"],
                tag=data["tag"],
            )
        )

    assert exc_info.value.http_status_code == 502


def test_rest_vm_sync_with_network_raises_502_for_degraded_task_history(monkeypatch):
    data = _vm_sync_inputs(
        {
            "agent": 0,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        }
    )
    _install_common_sync_patches(monkeypatch, vm_config=data["vm_config"], ip_payloads=[])

    async def _degraded_task_history(**_kwargs):
        return {"count": 1, "created": 2, "skipped": 0, "degraded": True, "errors": 1}

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.sync_all_virtual_machine_task_histories",
        _degraded_task_history,
    )

    with pytest.raises(ProxboxException, match="degraded coverage") as exc_info:
        asyncio.run(
            create_virtual_machines(
                netbox_session=data["netbox_session"],
                pxs=data["pxs"],
                cluster_status=data["cluster_status"],
                cluster_resources=data["cluster_resources"],
                custom_fields=data["custom_fields"],
                tag=data["tag"],
            )
        )

    assert exc_info.value.http_status_code == 502
    assert exc_info.value.detail == {"errors": 1, "reconciled": 2, "skipped": 0}


def test_vm_sync_can_disable_task_history_for_dedicated_followup_stage(monkeypatch):
    data = _vm_sync_inputs(
        {
            "agent": 0,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        }
    )
    _install_common_sync_patches(monkeypatch, vm_config=data["vm_config"], ip_payloads=[])

    async def _unexpected_task_history(**_kwargs):
        raise AssertionError("disabled task history must not run")

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.sync_all_virtual_machine_task_histories",
        _unexpected_task_history,
        raising=False,
    )

    result = asyncio.run(
        create_virtual_machines(
            netbox_session=data["netbox_session"],
            pxs=data["pxs"],
            cluster_status=data["cluster_status"],
            cluster_resources=data["cluster_resources"],
            custom_fields=data["custom_fields"],
            tag=data["tag"],
            sync_task_history=False,
        )
    )

    assert len(result) == 1


def test_vm_sync_skips_guest_agent_call_when_disabled(monkeypatch):
    data = _vm_sync_inputs(
        {
            "agent": 0,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,ip=10.0.0.22/24",
        }
    )
    ip_payloads: list[dict] = []
    _install_common_sync_patches(monkeypatch, vm_config=data["vm_config"], ip_payloads=ip_payloads)

    async def _should_not_be_called(*args, **kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.fetch_qemu_guest_agent_network_interfaces",
        _should_not_be_called,
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

    async def _fake_guest_fe80(*args, **kwargs):
        return GuestAgentFetchResult(
            interfaces=[
                {
                    "name": "ens18",
                    "mac_address": "AA:BB:CC:DD:EE:FF",
                    "ip_addresses": [
                        {"ip_address": "fe80::1", "prefix": 64, "ip_address_type": "ipv6"}
                    ],
                }
            ],
            diagnostic=None,
        )

    _install_common_sync_patches(monkeypatch, vm_config=data["vm_config"], ip_payloads=ip_payloads)
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.fetch_qemu_guest_agent_network_interfaces",
        _fake_guest_fe80,
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

    async def _fake_guest_fe80(*args, **kwargs):
        return GuestAgentFetchResult(
            interfaces=[
                {
                    "name": "ens18",
                    "mac_address": "AA:BB:CC:DD:EE:FF",
                    "ip_addresses": [
                        {"ip_address": "fe80::1", "prefix": 64, "ip_address_type": "ipv6"}
                    ],
                }
            ],
            diagnostic=None,
        )

    _install_common_sync_patches(monkeypatch, vm_config=data["vm_config"], ip_payloads=ip_payloads)
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.fetch_qemu_guest_agent_network_interfaces",
        _fake_guest_fe80,
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
    captured_payloads: list[dict] = []

    def _fake_get_vm_config(*args, **kwargs):
        return data["vm_config"]

    async def _fake_bulk_reconcile(nb, payloads, **_kwargs):
        # Capture payloads to verify VM ID is included
        captured_payloads.extend(payloads)
        # Return mock interface records with the expected ID and VM mapping
        return (
            [{"id": 66, "name": "net0", "virtual_machine": 55, "mac_address": "AA:BB:CC:DD:EE:FF"}],
            {},  # name_vm_to_id mapping
        )

    async def _fake_resolve_netbox_vm(*args, **kwargs):
        return {"id": 55, "name": "vm01"}

    async def _fake_load_snapshot(nb):
        return [{"id": 55, "name": "vm01", "custom_fields": {"proxmox_vm_id": 101}}]

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
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._load_netbox_virtual_machine_snapshot",
        _fake_load_snapshot,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.bulk_reconcile_vm_interfaces",
        _fake_bulk_reconcile,
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

    # Verify the result contains the interface record
    assert len(result) == 1
    assert result[0]["id"] == 66
    assert result[0]["mac_address"] == "AA:BB:CC:DD:EE:FF"

    # Verify that bulk reconciliation received payloads with the correct VM ID
    assert len(captured_payloads) == 1
    assert captured_payloads[0]["virtual_machine"] == 55


def test_vm_only_interface_sync_uses_vm_id_for_bridge_lookup(monkeypatch):
    data = _vm_sync_inputs(
        {
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        }
    )

    def _fake_get_vm_config(*args, **kwargs):
        return data["vm_config"]

    async def _fake_bulk_reconcile(nb, payloads, **_kwargs):
        # Mock should return interface records with correct IDs
        return (
            [{"id": 66, "name": "net0", "virtual_machine": 55, "mac_address": "AA:BB:CC:DD:EE:FF"}],
            {},  # name_vm_to_id mapping
        )

    async def _fake_resolve_netbox_vm(*args, **kwargs):
        return {"id": 55, "name": "vm01"}

    async def _fake_load_snapshot(nb):
        return [{"id": 55, "name": "vm01", "custom_fields": {"proxmox_vm_id": 101}}]

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
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._load_netbox_virtual_machine_snapshot",
        _fake_load_snapshot,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.bulk_reconcile_vm_interfaces",
        _fake_bulk_reconcile,
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

    # Verify interface sync succeeds with VM ID resolution
    assert result and result[0]["id"] == 66


def test_vm_only_ip_sync_uses_resolved_netbox_vm_id(monkeypatch):
    data = _vm_sync_inputs(
        {
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,ip=10.0.0.20/24",
        }
    )
    primary_ip_calls: list[dict] = []

    def _fake_get_vm_config(*args, **kwargs):
        return data["vm_config"]

    async def _fake_bulk_reconcile_ips(nb, payloads, **_kwargs):
        # Return mock IP records with correct IDs
        return [{"id": 77, "address": "10.0.0.20/24"}]

    async def _fake_rest_list(*args, **kwargs):
        # Return mock interface for the VM
        return [{"id": 66, "name": "net0", "virtual_machine": 55}]

    async def _fake_rest_first(*args, **kwargs):
        # Return mock IP record for primary IP lookup
        return {"id": 77, "address": "10.0.0.20/24"}

    async def _fake_set_primary_ip(**kwargs):
        primary_ip_calls.append(kwargs)
        return True

    async def _fake_guest_plugin_first(*args, **kwargs):
        return None

    async def _fake_guest_plugin_create(_nb, path, payload, **_kwargs):
        if path == "/api/plugins/proxbox/guest-vm-interfaces/":
            return {"id": 901, **payload}
        if path == "/api/plugins/proxbox/guest-vm-interface-addresses/":
            return {"id": 902, **payload}
        return {"id": 999, **payload}

    async def _fake_guest_plugin_patch(_nb, path, record_id, payload, **_kwargs):
        return {"id": record_id, **payload}

    async def _fake_resolve_netbox_vm(*args, **kwargs):
        return {"id": 55, "name": "vm01"}

    async def _fake_load_snapshot(nb):
        return [{"id": 55, "name": "vm01", "custom_fields": {"proxmox_vm_id": 101}}]

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
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._load_netbox_virtual_machine_snapshot",
        _fake_load_snapshot,
    )
    monkeypatch.setattr(
        "proxbox_api.netbox_rest.rest_list_async",
        _fake_rest_list,
    )
    monkeypatch.setattr(
        "proxbox_api.netbox_rest.rest_first_async",
        _fake_rest_first,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.bulk_reconcile_vm_interface_ips",
        _fake_bulk_reconcile_ips,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.set_primary_ip",
        _fake_set_primary_ip,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_first_async",
        _fake_guest_plugin_first,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_create_async",
        _fake_guest_plugin_create,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_patch_async",
        _fake_guest_plugin_patch,
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

    # Verify IP sync result contains correct IP record
    assert len(result) == 1
    assert result[0]["ip_id"] == 77
    assert result[0]["address"] == "10.0.0.20/24"

    # Verify set_primary_ip was called with the resolved VM
    assert len(primary_ip_calls) == 1
    assert primary_ip_calls[0]["virtual_machine"]["id"] == 55
    assert primary_ip_calls[0]["primary_ip_id"] == 77


def test_vm_only_ip_sync_prefers_ipv4_primary_when_guest_reports_ipv6_first(monkeypatch):
    data = _vm_sync_inputs(
        {
            "agent": 1,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        }
    )
    primary_ip_calls: list[dict[str, object]] = []

    def _fake_get_vm_config(*args, **kwargs):
        return data["vm_config"]

    async def _fake_guest_ifaces(*args, **kwargs):
        return [
            {
                "name": "ens18",
                "mac_address": "AA:BB:CC:DD:EE:FF",
                "ip_addresses": [
                    {
                        "ip_address": "2804:2cac:1030:0:428f:a69c:d3b5:c794",
                        "prefix": 64,
                        "ip_address_type": "ipv6",
                    },
                    {
                        "ip_address": "10.0.0.20",
                        "prefix": 24,
                        "ip_address_type": "ipv4",
                    },
                ],
            }
        ]

    async def _fake_bulk_reconcile_ips(nb, payloads, **_kwargs):
        return [
            {
                "id": 99,
                "address": "2804:2cac:1030:0:428f:a69c:d3b5:c794/64",
            },
            {"id": 77, "address": "10.0.0.20/24"},
        ]

    async def _fake_rest_list(*args, **kwargs):
        return [{"id": 66, "name": "net0", "virtual_machine": 55}]

    async def _fake_rest_first(*args, **kwargs):
        query = kwargs.get("query") or {}
        address = str(query.get("address") or "")
        if address == "10.0.0.20/24":
            return {"id": 77, "address": address}
        if address == "2804:2cac:1030:0:428f:a69c:d3b5:c794/64":
            return {"id": 99, "address": address}
        return None

    async def _fake_set_primary_ip(**kwargs):
        primary_ip_calls.append(kwargs)
        return True

    async def _fake_guest_plugin_first(*args, **kwargs):
        return None

    async def _fake_guest_plugin_create(_nb, path, payload, **_kwargs):
        if path == "/api/plugins/proxbox/guest-vm-interfaces/":
            return {"id": 901, **payload}
        if path == "/api/plugins/proxbox/guest-vm-interface-addresses/":
            return {"id": 902, **payload}
        return {"id": 999, **payload}

    async def _fake_guest_plugin_patch(_nb, path, record_id, payload, **_kwargs):
        return {"id": record_id, **payload}

    async def _fake_resolve_netbox_vm(*args, **kwargs):
        return {"id": 55, "name": "vm01"}

    async def _fake_load_snapshot(nb):
        return [{"id": 55, "name": "vm01", "custom_fields": {"proxmox_vm_id": 101}}]

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.get_vm_config",
        _fake_get_vm_config,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.get_qemu_guest_agent_network_interfaces",
        _fake_guest_ifaces,
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
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._load_netbox_virtual_machine_snapshot",
        _fake_load_snapshot,
    )
    monkeypatch.setattr(
        "proxbox_api.netbox_rest.rest_list_async",
        _fake_rest_list,
    )
    monkeypatch.setattr(
        "proxbox_api.netbox_rest.rest_first_async",
        _fake_rest_first,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.bulk_reconcile_vm_interface_ips",
        _fake_bulk_reconcile_ips,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.set_primary_ip",
        _fake_set_primary_ip,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_first_async",
        _fake_guest_plugin_first,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_create_async",
        _fake_guest_plugin_create,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_patch_async",
        _fake_guest_plugin_patch,
    )

    asyncio.run(
        create_only_vm_ip_addresses(
            netbox_session=data["netbox_session"],
            pxs=data["pxs"],
            cluster_status=data["cluster_status"],
            cluster_resources=data["cluster_resources"],
            custom_fields=data["custom_fields"],
            tag=data["tag"],
        )
    )

    # Both IPv4 and IPv6 primaries are set independently (dual-stack fix).
    assert len(primary_ip_calls) == 2
    called_ids = {c["primary_ip_id"] for c in primary_ip_calls}
    assert called_ids == {77, 99}  # 77 = IPv4, 99 = IPv6
    # IPv4 is listed first because ipv4 is the preferred family.
    assert primary_ip_calls[0]["primary_ip_id"] == 77
    assert primary_ip_calls[0]["primary_ip_preference"] == "ipv4"


def test_vm_only_ip_sync_guest_links_same_address_by_interface_scope(monkeypatch):
    data = _vm_sync_inputs({})
    data["cluster_resources"][0]["lab"] = [
        {
            "type": "qemu",
            "name": "vm01",
            "node": "pve01",
            "vmid": 101,
            "status": "running",
        },
        {
            "type": "qemu",
            "name": "vm02",
            "node": "pve01",
            "vmid": 102,
            "status": "running",
        },
    ]
    guest_interface_payloads: list[dict[str, object]] = []
    guest_address_payloads: list[dict[str, object]] = []

    def _fake_get_vm_config(*args, **kwargs):
        vmid = int(kwargs.get("vmid") or args[3])
        mac = "AA:BB:CC:DD:EE:01" if vmid == 101 else "AA:BB:CC:DD:EE:02"
        return {"agent": 1, "net0": f"virtio={mac},bridge=vmbr0"}

    async def _fake_guest_ifaces(*args, **kwargs):
        vmid = int(args[2] if len(args) >= 3 else kwargs["vmid"])
        mac = "AA:BB:CC:DD:EE:01" if vmid == 101 else "AA:BB:CC:DD:EE:02"
        name = "ens18" if vmid == 101 else "ens19"
        return [
            {
                "name": name,
                "mac_address": mac,
                "ip_addresses": [
                    {"ip_address": "10.0.0.50", "prefix": 24, "ip_address_type": "ipv4"}
                ],
            }
        ]

    async def _fake_rest_list(*args, **kwargs):
        query = kwargs.get("query") or {}
        vm_id = int(query.get("virtual_machine_id") or 0)
        if vm_id == 55:
            return [{"id": 66, "name": "net0", "virtual_machine": 55}]
        if vm_id == 56:
            return [{"id": 67, "name": "net0", "virtual_machine": 56}]
        return []

    async def _fake_bulk_reconcile_ips(nb, payloads, **_kwargs):
        records = []
        for payload in payloads:
            interface_id = int(payload["assigned_object_id"])
            records.append(
                {
                    "id": 77 if interface_id == 66 else 78,
                    "address": payload["address"],
                    "assigned_object_id": interface_id,
                }
            )
        return records

    async def _fake_rest_first(*args, **kwargs):
        return {"id": 77, "address": "10.0.0.50/24"}

    async def _fake_set_primary_ip(**kwargs):
        return True

    async def _fake_load_snapshot(nb):
        return [
            {"id": 55, "name": "vm01", "custom_fields": {"proxmox_vm_id": 101}},
            {"id": 56, "name": "vm02", "custom_fields": {"proxmox_vm_id": 102}},
        ]

    async def _fake_guest_plugin_first(*args, **kwargs):
        return None

    async def _fake_guest_plugin_create(_nb, path, payload, **_kwargs):
        if path == "/api/plugins/proxbox/guest-vm-interfaces/":
            guest_interface_payloads.append(payload)
            guest_id = 901 if payload["virtual_machine"] == 55 else 902
            return {"id": guest_id, **payload}
        if path == "/api/plugins/proxbox/guest-vm-interface-addresses/":
            guest_address_payloads.append(payload)
            return {"id": 990 + int(payload["guest_interface"]), **payload}
        return {"id": 999, **payload}

    async def _fake_guest_plugin_patch(*args, **kwargs):
        raise AssertionError("guest plugin test expects create path only")

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.get_vm_config",
        _fake_get_vm_config,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.get_qemu_guest_agent_network_interfaces",
        _fake_guest_ifaces,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.resolve_vm_sync_concurrency",
        lambda: 1,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._load_netbox_virtual_machine_snapshot",
        _fake_load_snapshot,
    )
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _fake_rest_list)
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_first_async", _fake_rest_first)
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.bulk_reconcile_vm_interface_ips",
        _fake_bulk_reconcile_ips,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.set_primary_ip",
        _fake_set_primary_ip,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_first_async",
        _fake_guest_plugin_first,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_create_async",
        _fake_guest_plugin_create,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_patch_async",
        _fake_guest_plugin_patch,
    )

    asyncio.run(
        create_only_vm_ip_addresses(
            netbox_session=data["netbox_session"],
            pxs=data["pxs"],
            cluster_status=data["cluster_status"],
            cluster_resources=data["cluster_resources"],
            custom_fields=data["custom_fields"],
            tag=data["tag"],
        )
    )

    assert {
        (payload["virtual_machine"], payload["vm_interface"], payload["name"])
        for payload in guest_interface_payloads
    } == {(55, 66, "ens18"), (56, 67, "ens19")}
    assert {
        (payload["guest_interface"], payload["ip_address"]) for payload in guest_address_payloads
    } == {
        (901, 77),
        (902, 78),
    }


def _install_ip_only_patches(monkeypatch, *, vm_config: dict, primary_ip_calls: list):
    """Install the minimal set of monkeypatches needed by create_only_vm_ip_addresses."""

    def _fake_get_vm_config(*args, **kwargs):
        return vm_config

    async def _fake_bulk_reconcile_ips(nb, payloads, **_kwargs):
        return [{"id": 77, "address": p.get("address", "")} for p in payloads]

    async def _fake_rest_list(*args, **kwargs):
        return [{"id": 66, "name": "net0", "virtual_machine": 55}]

    async def _fake_rest_first(*args, **kwargs):
        return {"id": 77, "address": "10.0.0.20/24"}

    async def _fake_set_primary_ip(**kwargs):
        primary_ip_calls.append(kwargs)
        return True

    async def _fake_resolve_netbox_vm(*args, **kwargs):
        return {"id": 55, "name": "vm01"}

    async def _fake_load_snapshot(nb):
        return [{"id": 55, "name": "vm01", "custom_fields": {"proxmox_vm_id": 101}}]

    for attr, val in [
        ("get_vm_config", _fake_get_vm_config),
        ("resolve_vm_sync_concurrency", lambda: 1),
        ("_resolve_netbox_virtual_machine_by_proxmox_id", _fake_resolve_netbox_vm),
        ("_load_netbox_virtual_machine_snapshot", _fake_load_snapshot),
    ]:
        monkeypatch.setattr(
            f"proxbox_api.routes.virtualization.virtual_machines.sync_vm.{attr}",
            val,
        )
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _fake_rest_list)
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_first_async", _fake_rest_first)
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.bulk_reconcile_vm_interface_ips",
        _fake_bulk_reconcile_ips,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.set_primary_ip",
        _fake_set_primary_ip,
    )


def test_agent_kv_flag_disabled_skips_guest_agent_fetch(monkeypatch):
    """agent='0,fstrim_cloned_disks=1' must NOT trigger guest-agent fetch (closes #491)."""
    data = _vm_sync_inputs(
        {
            "agent": "0,fstrim_cloned_disks=1",
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,ip=10.0.0.20/24",
        }
    )
    guest_agent_calls: list = []
    primary_ip_calls: list = []
    _install_ip_only_patches(
        monkeypatch, vm_config=data["vm_config"], primary_ip_calls=primary_ip_calls
    )

    async def _spy_guest_ifaces(*args, **kwargs):
        guest_agent_calls.append(args)
        return []

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.get_qemu_guest_agent_network_interfaces",
        _spy_guest_ifaces,
    )

    asyncio.run(
        create_only_vm_ip_addresses(
            netbox_session=data["netbox_session"],
            pxs=data["pxs"],
            cluster_status=data["cluster_status"],
            cluster_resources=data["cluster_resources"],
            custom_fields=data["custom_fields"],
            tag=data["tag"],
        )
    )

    assert guest_agent_calls == [], "Guest agent must not be fetched when agent KV flag head is '0'"


def test_agent_kv_flag_enabled_calls_guest_agent_fetch(monkeypatch):
    """agent='1,fstrim_cloned_disks=1' MUST trigger guest-agent fetch (closes #491)."""
    data = _vm_sync_inputs(
        {
            "agent": "1,fstrim_cloned_disks=1",
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        }
    )
    guest_agent_calls: list = []
    primary_ip_calls: list = []
    _install_ip_only_patches(
        monkeypatch, vm_config=data["vm_config"], primary_ip_calls=primary_ip_calls
    )

    async def _spy_guest_ifaces(*args, **kwargs):
        guest_agent_calls.append(args)
        return []

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.get_qemu_guest_agent_network_interfaces",
        _spy_guest_ifaces,
    )

    asyncio.run(
        create_only_vm_ip_addresses(
            netbox_session=data["netbox_session"],
            pxs=data["pxs"],
            cluster_status=data["cluster_status"],
            cluster_resources=data["cluster_resources"],
            custom_fields=data["custom_fields"],
            tag=data["tag"],
        )
    )

    assert len(guest_agent_calls) == 1, "Guest agent must be fetched when agent KV flag head is '1'"


def test_vm_only_ip_sync_surfaces_missing_interface_skip(monkeypatch):
    """When a NIC's interface is absent from NetBox the IP is skipped visibly.

    Regression: the skip used to be an invisible ``logger.debug`` + ``continue``,
    so an IP-only run whose interfaces were stale/missing reconciled no IPs with
    no error. It must now emit a ``phase_summary`` with a non-zero skip count.
    """
    from proxbox_api.utils.streaming import WebSocketSSEBridge

    data = _vm_sync_inputs(
        {
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,ip=10.0.0.20/24",
        }
    )

    def _fake_get_vm_config(*args, **kwargs):
        return data["vm_config"]

    async def _fake_bulk_reconcile_ips(nb, payloads, **_kwargs):
        return []

    async def _fake_rest_list(*args, **kwargs):
        # NetBox has an interface for this VM, but under a different name than
        # the Proxmox NIC (net0) resolves to — so the IP cannot be attached.
        return [{"id": 66, "name": "some-other-iface", "virtual_machine": 55}]

    async def _fake_rest_first(*args, **kwargs):
        return None

    async def _fake_resolve_netbox_vm(*args, **kwargs):
        return {"id": 55, "name": "vm01"}

    async def _fake_load_snapshot(nb):
        return [{"id": 55, "name": "vm01", "custom_fields": {"proxmox_vm_id": 101}}]

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
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm._load_netbox_virtual_machine_snapshot",
        _fake_load_snapshot,
    )
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _fake_rest_list)
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_first_async", _fake_rest_first)
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.bulk_reconcile_vm_interface_ips",
        _fake_bulk_reconcile_ips,
    )

    bridge = WebSocketSSEBridge()

    result = asyncio.run(
        create_only_vm_ip_addresses(
            netbox_session=data["netbox_session"],
            pxs=data["pxs"],
            cluster_status=data["cluster_status"],
            cluster_resources=data["cluster_resources"],
            custom_fields=data["custom_fields"],
            tag=data["tag"],
            websocket=bridge,
            use_websocket=True,
        )
    )

    # No IP could be attached because the interface was missing.
    assert result == []

    # Drain the bridge queue and confirm a phase summary surfaced the skip.
    summaries: list[dict] = []
    while not bridge._queue.empty():
        item = bridge._queue.get_nowait()
        if item is None:
            continue
        event, payload = item
        if event == "phase_summary":
            summaries.append(payload)

    assert any((s.get("result") or {}).get("skipped") for s in summaries), (
        f"expected a phase_summary with a non-zero skip count, got {summaries!r}"
    )
    assert any("interface" in str(s.get("message", "")).lower() for s in summaries), (
        f"expected the skip message to mention the missing interface, got {summaries!r}"
    )
