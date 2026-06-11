"""Regression tests for two-phase full-update VM config fetching."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from proxbox_api.routes.virtualization.virtual_machines import sync_vm
from proxbox_api.schemas.sync import SyncBehaviorFlags, SyncOverwriteFlags
from proxbox_api.utils.streaming import WebSocketSSEBridge
from tests.fixtures import PROXMOX_VM_CONFIG, PROXMOX_VM_RESOURCE


class _CapturingBridge(WebSocketSSEBridge):
    def __init__(self) -> None:
        super().__init__()
        self.phase_summaries: list[dict[str, object]] = []

    async def emit_phase_summary(self, **kwargs) -> None:
        self.phase_summaries.append(kwargs)


def _resource(vmid: int) -> dict[str, object]:
    return {
        "type": "qemu",
        "name": f"vm-{vmid}",
        "vmid": vmid,
        "node": "pve01",
        "status": "running",
        "maxcpu": 2,
        "maxmem": 2_147_483_648,
        "maxdisk": 10_737_418_240,
    }


def _install_full_update_stubs(monkeypatch, *, payload_side_effect=None) -> list[int]:
    fetch_calls: list[int] = []

    async def _fake_detect_netbox_version(_nb):
        return (4, 5, 0)

    async def _fake_rest_list(*_args, **_kwargs):
        return []

    async def _fake_reconcile(*_args, **kwargs):
        payload = kwargs.get("payload") or {}
        lookup = kwargs.get("lookup") or {}
        return SimpleNamespace(id=33, name=payload.get("name"), slug=lookup.get("slug"))

    async def _fake_ensure(*_args, **_kwargs):
        return SimpleNamespace(id=1)

    def _fake_build_payload(**kwargs):
        if payload_side_effect is not None:
            payload_side_effect(kwargs)
        resource = kwargs["proxmox_resource"]
        vmid = int(resource["vmid"])
        return {
            "name": f"vm-{vmid}",
            "status": "active",
            "cluster": kwargs["cluster_id"],
            "device": kwargs["device_id"],
            "role": kwargs["role_id"],
            "vcpus": 1,
            "memory": 1024,
            "disk": 0,
            "tags": kwargs["tag_ids"],
            "custom_fields": {
                "proxmox_vm_id": vmid,
                "proxmox_vm_type": resource.get("type"),
            },
            "description": "Synced from Proxmox node pve01",
        }

    async def _fake_rest_create(_nb, _path, payload, *, lookup=None):
        vmid = int((lookup or {})["cf_proxmox_vm_id"])
        return {"id": vmid, **payload}

    async def _fake_stamp(*_args, **_kwargs):
        return None

    async def _fake_task_history(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(sync_vm, "detect_netbox_version", _fake_detect_netbox_version)
    monkeypatch.setattr(sync_vm, "rest_list_async", _fake_rest_list)
    monkeypatch.setattr(sync_vm, "rest_reconcile_async", _fake_reconcile)
    monkeypatch.setattr(sync_vm, "resolve_vm_sync_concurrency", lambda: 4)
    monkeypatch.setattr(sync_vm, "resolve_netbox_write_concurrency", lambda: 4)
    for name in (
        "_ensure_cluster_type",
        "_ensure_cluster",
        "_ensure_manufacturer",
        "_ensure_device_type",
        "_ensure_site",
        "_resolve_tenant",
        "_ensure_device",
        "_ensure_proxmox_node_role",
        "ensure_vm_type",
    ):
        monkeypatch.setattr(sync_vm, name, _fake_ensure)
    monkeypatch.setattr(sync_vm, "build_netbox_virtual_machine_payload", _fake_build_payload)
    monkeypatch.setattr(sync_vm, "rest_create_async", _fake_rest_create)
    monkeypatch.setattr(sync_vm, "stamp_vm_last_run_id", _fake_stamp)
    monkeypatch.setattr(sync_vm, "sync_virtual_machine_task_history", _fake_task_history)

    return fetch_calls


def test_prepare_vm_from_config_builds_prepared_state_from_fetched_config(monkeypatch):
    captured_payload_kwargs: dict[str, object] = {}
    ensure_device_calls: list[dict[str, object]] = []
    role_reconcile_calls: list[dict[str, object]] = []
    resolved_vm_types: list[str] = []
    resolved_tag_inputs: list[tuple[str, dict[str, object]]] = []

    def _fake_build_payload(**kwargs):
        captured_payload_kwargs.update(kwargs)
        return {
            "name": "db-vm-01",
            "status": "active",
            "cluster": kwargs["cluster_id"],
            "device": kwargs["device_id"],
            "role": kwargs["role_id"],
            "vcpus": 4,
            "memory": 8192,
            "disk": 0,
            "tags": kwargs["tag_ids"],
            "custom_fields": {"proxmox_vm_id": 101, "proxmox_vm_type": "qemu"},
            "description": "Synced from Proxmox node pve01",
        }

    async def _fake_ensure_device(*_args, **kwargs):
        ensure_device_calls.append(kwargs)
        return SimpleNamespace(id=22)

    async def _fake_reconcile(*_args, **kwargs):
        role_reconcile_calls.append(kwargs)
        return SimpleNamespace(id=33)

    async def _resolve_vm_type(vm_type_key: str):
        resolved_vm_types.append(vm_type_key)
        return None

    async def _resolve_tags(cluster_name: str, vm_config: dict[str, object]):
        resolved_tag_inputs.append((cluster_name, vm_config))
        return [7, 0]

    monkeypatch.setattr(sync_vm, "build_netbox_virtual_machine_payload", _fake_build_payload)
    monkeypatch.setattr(sync_vm, "_ensure_device", _fake_ensure_device)
    monkeypatch.setattr(sync_vm, "rest_reconcile_async", _fake_reconcile)

    resource = dict(PROXMOX_VM_RESOURCE)
    vm_config = {**PROXMOX_VM_CONFIG, "tags": "critical;prod"}
    context = sync_vm._VMPreparationContext(
        nb=object(),
        tag=SimpleNamespace(id=5),
        overwrite_flags=SyncOverwriteFlags(),
        behavior_flags=SyncBehaviorFlags(),
        effective_vm_overwrite_flags=SyncOverwriteFlags(),
        cluster_dependency_cache={
            "cluster-a": {
                "cluster": SimpleNamespace(id=11),
                "site": SimpleNamespace(id=44),
                "tenant": SimpleNamespace(id=55),
                "device_type": SimpleNamespace(id=66),
                "device_role": SimpleNamespace(id=77),
            }
        },
        node_device_cache={},
        vm_role_cache={},
        vm_role_mapping=sync_vm.VM_ROLE_MAPPINGS,
        tag_refs=[{"name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        proxmox_url_by_cluster={"cluster-a": "https://pve.example:8006"},
        resolve_vm_type=_resolve_vm_type,
        resolve_vm_proxmox_tag_ids=_resolve_tags,
    )

    prepared = asyncio.run(
        sync_vm._prepare_vm_from_config("cluster-a", resource, vm_config, context)
    )

    assert prepared.cluster_name == "cluster-a"
    assert prepared.resource is resource
    assert prepared.vm_config is vm_config
    assert prepared.vm_config_obj.qemu_agent_enabled is True
    assert prepared.lookup == {"cf_proxmox_vm_id": 101, "cluster_id": 11}
    assert prepared.desired_payload["custom_fields"]["proxmox_vm_id"] == 101
    assert captured_payload_kwargs["proxmox_resource"] is resource
    assert captured_payload_kwargs["proxmox_config"] is vm_config
    assert captured_payload_kwargs["cluster_id"] == 11
    assert captured_payload_kwargs["device_id"] == 22
    assert captured_payload_kwargs["role_id"] == 33
    assert captured_payload_kwargs["site_id"] == 44
    assert captured_payload_kwargs["tenant_id"] == 55
    assert captured_payload_kwargs["tag_ids"] == [5, 7]
    assert captured_payload_kwargs["proxmox_url"] == "https://pve.example:8006"
    assert ensure_device_calls
    assert role_reconcile_calls
    assert context.node_device_cache[("cluster-a", "pve01")].id == 22
    assert context.vm_role_cache["qemu"].id == 33
    assert resolved_vm_types == ["qemu"]
    assert resolved_tag_inputs == [("cluster-a", vm_config)]


def test_full_update_fetch_failure_isolated_and_counted(monkeypatch):
    fetch_calls = _install_full_update_stubs(monkeypatch)

    async def _fake_get_vm_config(**kwargs):
        vmid = int(kwargs["vmid"])
        fetch_calls.append(vmid)
        if vmid == 102:
            raise RuntimeError("spurious timeout")
        return dict(PROXMOX_VM_CONFIG)

    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)
    bridge = _CapturingBridge()

    result = asyncio.run(
        sync_vm.create_virtual_machines(
            netbox_session=object(),
            pxs=[],
            cluster_status=[SimpleNamespace(name="cluster-a", mode="cluster")],
            cluster_resources=[
                {"cluster-a": [_resource(101), _resource(102)]},
            ],
            custom_fields=[],
            tag=SimpleNamespace(id=5, name="Proxbox", slug="proxbox", color="ff5722"),
            websocket=bridge,
            sync_vm_network=False,
        )
    )

    assert [record["id"] for record in result] == [101]
    assert sorted(fetch_calls) == [101, 102]
    assert bridge.phase_summaries[-1]["created"] == 1
    assert bridge.phase_summaries[-1]["failed"] == 1


def test_full_update_finishes_all_config_fetches_before_processing(monkeypatch):
    events: list[str] = []

    def _record_process(kwargs):
        vmid = int(kwargs["proxmox_resource"]["vmid"])
        events.append(f"process-{vmid}")

    fetch_calls = _install_full_update_stubs(
        monkeypatch,
        payload_side_effect=_record_process,
    )

    async def _fake_get_vm_config(**kwargs):
        vmid = int(kwargs["vmid"])
        fetch_calls.append(vmid)
        events.append(f"fetch-start-{vmid}")
        await asyncio.sleep(0)
        events.append(f"fetch-end-{vmid}")
        return dict(PROXMOX_VM_CONFIG)

    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)

    result = asyncio.run(
        sync_vm.create_virtual_machines(
            netbox_session=object(),
            pxs=[],
            cluster_status=[SimpleNamespace(name="cluster-a", mode="cluster")],
            cluster_resources=[
                {"cluster-a": [_resource(101), _resource(102), _resource(103)]},
            ],
            custom_fields=[],
            tag=SimpleNamespace(id=5, name="Proxbox", slug="proxbox", color="ff5722"),
            sync_vm_network=False,
        )
    )

    assert [record["id"] for record in result] == [101, 102, 103]
    assert sorted(fetch_calls) == [101, 102, 103]
    first_process_index = next(index for index, event in enumerate(events) if event.startswith("process-"))
    fetch_end_indexes = [
        index for index, event in enumerate(events) if event.startswith("fetch-end-")
    ]
    assert len(fetch_end_indexes) == 3
    assert all(index < first_process_index for index in fetch_end_indexes)
