"""Tests for VM reconciliation queue processing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from proxbox_api.exception import ProxboxException
from proxbox_api.proxmox_to_netbox.models import ProxmoxVmConfigInput
from proxbox_api.routes.virtualization.virtual_machines import sync_vm


def _prepared_vm(
    *,
    cluster_name: str,
    vmid: int,
    memory: int,
    endpoint_id: int = 500,
) -> sync_vm._PreparedVMState:
    desired_payload = {
        "name": f"vm-{vmid}",
        "status": "active",
        "cluster": 1,
        "device": 10,
        "role": 20,
        "vcpus": 2,
        "memory": memory,
        "disk": 30,
        "tags": [99],
        "custom_fields": {
            "proxmox_endpoint_id": endpoint_id,
            "proxmox_vm_id": vmid,
        },
        "description": "Synced from Proxmox node pve01",
    }
    return sync_vm._PreparedVMState(
        cluster_name=cluster_name,
        resource={"name": f"vm-{vmid}", "vmid": vmid, "type": "qemu"},
        vm_config={},
        vm_config_obj=ProxmoxVmConfigInput.model_validate({}),
        desired_payload=desired_payload,
        lookup={"cf_proxmox_vm_id": vmid, "cf_proxmox_endpoint_id": endpoint_id},
        now=datetime.now(timezone.utc),
        vm_type="qemu",
    )


def test_build_vm_operation_queue_classifies_ok_create_update():
    prepared = [
        _prepared_vm(cluster_name="cluster-a", vmid=101, memory=2048),
        _prepared_vm(cluster_name="cluster-a", vmid=102, memory=4096),
        _prepared_vm(cluster_name="cluster-a", vmid=103, memory=8192),
    ]

    snapshot = [
        {
            "id": 2002,
            "name": "vm-102",
            "status": "active",
            "cluster": {"id": 1, "name": "cluster-a"},
            "device": {"id": 10},
            "role": {"id": 20},
            "vcpus": 2,
            "memory": 4096,
            "disk": 30,
            "tags": [{"id": 99}],
            "custom_fields": {"proxmox_endpoint_id": 500, "proxmox_vm_id": 102},
            "description": "Synced from Proxmox node pve01",
        },
        {
            "id": 2003,
            "name": "vm-103",
            "status": "active",
            "cluster": {"id": 1, "name": "cluster-a"},
            "device": {"id": 10},
            "role": {"id": 20},
            "vcpus": 2,
            "memory": 2048,
            "disk": 30,
            "tags": [{"id": 99}],
            "custom_fields": {"proxmox_endpoint_id": 500, "proxmox_vm_id": 103},
            "description": "Synced from Proxmox node pve01",
        },
    ]

    queue = sync_vm._build_vm_operation_queue(prepared, snapshot)

    assert [op.method for op in queue] == ["CREATE", "GET", "UPDATE"]
    assert queue[2].patch_payload["memory"] == 8192


def test_build_vm_operation_queue_keeps_same_vmid_endpoints_separate():
    prepared = [
        _prepared_vm(cluster_name="standalone-a", vmid=105, memory=2048, endpoint_id=1),
        _prepared_vm(cluster_name="standalone-b", vmid=105, memory=2048, endpoint_id=2),
    ]

    snapshot = [
        {
            "id": 5105,
            "name": "vm-105-a",
            "status": "active",
            "cluster": {"id": 1, "name": "standalone-a"},
            "device": {"id": 10},
            "role": {"id": 20},
            "vcpus": 2,
            "memory": 2048,
            "disk": 30,
            "tags": [{"id": 99}],
            "custom_fields": {
                "proxmox_endpoint_id": 1,
                "proxmox_vm_id": 105,
                "proxmox_vm_type": "qemu",
            },
            "description": "Synced from Proxmox node pve01",
        },
        {
            "id": 5205,
            "name": "vm-105-b",
            "status": "active",
            "cluster": {"id": 1, "name": "standalone-b"},
            "device": {"id": 10},
            "role": {"id": 20},
            "vcpus": 2,
            "memory": 2048,
            "disk": 30,
            "tags": [{"id": 99}],
            "custom_fields": {
                "proxmox_endpoint_id": 2,
                "proxmox_vm_id": 105,
                "proxmox_vm_type": "qemu",
            },
            "description": "Synced from Proxmox node pve01",
        },
    ]

    queue = sync_vm._build_vm_operation_queue(prepared, snapshot)

    assert [op.method for op in queue] == ["UPDATE", "UPDATE"]
    assert [op.existing_record["id"] for op in queue if op.existing_record] == [5105, 5205]


def test_build_vm_operation_queue_omits_vm_type_when_overwrite_disabled():
    prepared = [_prepared_vm(cluster_name="cluster-a", vmid=104, memory=8192)]
    prepared[0].desired_payload["virtual_machine_type"] = 99

    snapshot = [
        {
            "id": 2004,
            "name": "vm-104",
            "status": "active",
            "cluster": {"id": 1, "name": "cluster-a"},
            "device": {"id": 10},
            "virtual_machine_type": {"id": 88},
            "role": {"id": 20},
            "vcpus": 2,
            "memory": 4096,
            "disk": 30,
            "tags": [{"id": 99}],
            "custom_fields": {"proxmox_endpoint_id": 500, "proxmox_vm_id": 104},
            "description": "Synced from Proxmox node pve01",
        }
    ]

    queue = sync_vm._build_vm_operation_queue(
        prepared,
        snapshot,
        overwrite_vm_type=False,
    )

    assert [op.method for op in queue] == ["UPDATE"]
    assert queue[0].patch_payload == {"memory": 8192}


def test_build_vm_operation_queue_omits_vm_type_when_netbox_lacks_native_field():
    prepared = [_prepared_vm(cluster_name="cluster-a", vmid=105, memory=8192)]
    prepared[0].desired_payload["virtual_machine_type"] = 99

    snapshot = [
        {
            "id": 2005,
            "name": "vm-105",
            "status": "active",
            "cluster": {"id": 1, "name": "cluster-a"},
            "device": {"id": 10},
            "role": {"id": 20},
            "vcpus": 2,
            "memory": 4096,
            "disk": 30,
            "tags": [{"id": 99}],
            "custom_fields": {"proxmox_endpoint_id": 500, "proxmox_vm_id": 105},
            "description": "Synced from Proxmox node pve01",
        }
    ]

    queue = sync_vm._build_vm_operation_queue(
        prepared,
        snapshot,
        supports_virtual_machine_type_field=False,
    )

    assert [op.method for op in queue] == ["UPDATE"]
    assert queue[0].patch_payload == {"memory": 8192}


def test_log_vm_reconciliation_measurement_includes_gate_fields(monkeypatch):
    prepared_qemu = _prepared_vm(cluster_name="cluster-a", vmid=106, memory=2048)
    prepared_lxc = _prepared_vm(cluster_name="cluster-a", vmid=107, memory=2048)
    prepared_lxc.resource["type"] = "lxc"
    prepared_lxc.vm_type = "lxc"
    queue = [
        sync_vm._NetBoxVMOperation(method="GET", prepared=prepared_qemu),
        sync_vm._NetBoxVMOperation(method="CREATE", prepared=prepared_lxc),
    ]
    messages: list[str] = []

    def _capture_info(message: str, *args: object) -> None:
        messages.append(message % args)

    monkeypatch.setattr(sync_vm.logger, "info", _capture_info)

    operation_counts = sync_vm._log_vm_reconciliation_measurement(
        operation_queue=queue,
        prepared_vms=[prepared_qemu, prepared_lxc],
        netbox_snapshot=[
            {"id": 2106, "custom_fields": {"proxmox_endpoint_id": 500, "proxmox_vm_id": 106}}
        ],
        duration_ms=12.34,
        supports_virtual_machine_type_field=True,
    )

    assert operation_counts == {"GET": 1, "CREATE": 1, "UPDATE": 0}
    assert len(messages) == 1
    message = messages[0]
    assert "reconciliation_ms=12.34" in message
    assert "vm_count=2" in message
    assert "snapshot_count=1" in message
    assert "qemu_count=1" in message
    assert "lxc_count=1" in message
    assert "supports_virtual_machine_type_field=True" in message
    assert "GET=1" in message
    assert "CREATE=1" in message
    assert "UPDATE=0" in message


@pytest.mark.asyncio
async def test_dispatch_vm_operation_queue_runs_writes_sequentially(monkeypatch):
    calls: list[str] = []
    create_lookups: list[dict[str, object] | None] = []

    monkeypatch.setattr(sync_vm, "resolve_netbox_write_concurrency", lambda: 2)

    async def _fake_create(nb, path, payload, *, lookup=None):
        calls.append(f"create:{payload['custom_fields']['proxmox_vm_id']}")
        create_lookups.append(lookup)
        vmid = payload["custom_fields"]["proxmox_vm_id"]
        return {"id": 3000 + vmid, **payload}

    async def _fake_patch(nb, path, record_id, payload):
        calls.append(f"patch:{record_id}")
        return {"id": record_id, **payload}

    async def _fake_first(nb, path, query):
        return None

    async def _fake_resolve(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sync_vm, "rest_create_async", _fake_create)
    monkeypatch.setattr(sync_vm, "rest_patch_async", _fake_patch)
    monkeypatch.setattr(sync_vm, "rest_first_async", _fake_first)
    monkeypatch.setattr(sync_vm, "resolve_virtual_machine_by_sync_state", _fake_resolve)

    prepared_create = _prepared_vm(cluster_name="cluster-a", vmid=201, memory=2048)
    prepared_get = _prepared_vm(cluster_name="cluster-a", vmid=202, memory=2048)
    prepared_update = _prepared_vm(cluster_name="cluster-a", vmid=203, memory=4096)

    queue = [
        sync_vm._NetBoxVMOperation(method="CREATE", prepared=prepared_create),
        sync_vm._NetBoxVMOperation(
            method="GET",
            prepared=prepared_get,
            existing_record={"id": 4202, "custom_fields": {"proxmox_vm_id": 202}},
        ),
        sync_vm._NetBoxVMOperation(
            method="UPDATE",
            prepared=prepared_update,
            existing_record={"id": 4203, "custom_fields": {"proxmox_vm_id": 203}},
            patch_payload={"memory": 4096},
        ),
    ]

    resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(object(), queue)
    assert failed_keys == set()

    assert calls == ["create:201", "patch:4203"]
    assert create_lookups == [{"cf_proxmox_vm_id": 201, "cf_proxmox_endpoint_id": 500}]
    assert resolved[("cluster-a", 202, "qemu")]["id"] == 4202
    assert resolved[("cluster-a", 201, "qemu")]["id"] == 3201
    assert resolved[("cluster-a", 203, "qemu")]["id"] == 4203


@pytest.mark.asyncio
async def test_dispatch_create_re_adopts_sidecar_vm_when_custom_fields_absent(monkeypatch):
    calls: list[str] = []

    async def _unexpected_create(*_args, **_kwargs):
        raise AssertionError("sidecar-adopted VM must not be created again")

    async def _fake_resolver(*_args, **_kwargs):
        return type(
            "Resolution",
            (),
            {
                "record": {
                    "id": 6101,
                    "name": "vm-301",
                    "status": "active",
                    "cluster": {"id": 1},
                    "device": {"id": 10},
                    "role": {"id": 20},
                    "vcpus": 2,
                    "memory": 1024,
                    "disk": 30,
                    "tags": [{"id": 99}],
                    "custom_fields": {},
                    "description": "old",
                },
                "record_id": 6101,
                "source": "sidecar",
            },
        )()

    async def _fake_reconcile(*_args, **kwargs):
        calls.append(kwargs["existing_record"]["id"])
        return {"id": kwargs["existing_record"]["id"], **kwargs["payload"]}

    monkeypatch.setattr(sync_vm, "rest_create_async", _unexpected_create)
    monkeypatch.setattr(sync_vm, "rest_reconcile_async", _fake_reconcile)
    monkeypatch.setattr(sync_vm, "resolve_virtual_machine_by_sync_state", _fake_resolver)
    monkeypatch.setattr(sync_vm, "resolve_netbox_write_concurrency", lambda: 1)

    prepared = _prepared_vm(cluster_name="cluster-a", vmid=301, memory=2048)
    queue = [sync_vm._NetBoxVMOperation(method="CREATE", prepared=prepared)]

    resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(object(), queue)

    assert failed_keys == set()
    assert calls == [6101]
    assert resolved[("cluster-a", 301, "qemu")]["id"] == 6101
    assert resolved[("cluster-a", 301, "qemu")]["memory"] == 2048


@pytest.mark.asyncio
async def test_sidecar_hydration_prevents_name_prepass_rename_for_cf_absent_vm(monkeypatch):
    prepared = _prepared_vm(cluster_name="cluster-a", vmid=301, memory=2048)
    snapshot = [
        {
            "id": 6101,
            "name": "vm-301",
            "cluster": {"id": 1, "name": "cluster-a"},
            "custom_fields": {},
        }
    ]

    async def _fake_resolver(*_args, **kwargs):
        assert kwargs["proxmox_vm_id"] == 301
        assert kwargs["endpoint_id"] == 500
        return type(
            "Resolution",
            (),
            {"record": snapshot[0], "record_id": 6101, "source": "sidecar"},
        )()

    monkeypatch.setattr(sync_vm, "resolve_virtual_machine_by_sync_state", _fake_resolver)

    hydrated = await sync_vm._hydrate_vm_snapshot_with_sidecar_identity(
        object(),
        prepared_vms=[prepared],
        netbox_snapshot=snapshot,
    )
    resolutions = await sync_vm._resolve_vm_names_pre_pass([prepared], snapshot, None)

    assert hydrated == 1
    assert resolutions == []
    assert prepared.desired_payload["name"] == "vm-301"
    assert snapshot[0]["custom_fields"] == {
        "proxmox_endpoint_id": 500,
        "proxmox_vm_id": 301,
        "proxmox_vm_type": "qemu",
    }


@pytest.mark.asyncio
async def test_sidecar_hydration_makes_reconciliation_queue_adopt_cf_absent_vm(monkeypatch):
    prepared = _prepared_vm(cluster_name="cluster-a", vmid=302, memory=2048)
    snapshot = [
        {
            "id": 6102,
            "name": "vm-302",
            "status": "active",
            "cluster": {"id": 1, "name": "cluster-a"},
            "device": {"id": 10},
            "role": {"id": 20},
            "vcpus": 2,
            "memory": 1024,
            "disk": 30,
            "tags": [{"id": 99}],
            "custom_fields": {},
            "description": "Synced from Proxmox node pve01",
        }
    ]

    async def _fake_resolver(*_args, **kwargs):
        assert kwargs["proxmox_vm_id"] == 302
        assert kwargs["endpoint_id"] == 500
        return type(
            "Resolution",
            (),
            {"record": snapshot[0], "record_id": 6102, "source": "sidecar"},
        )()

    monkeypatch.setattr(sync_vm, "resolve_virtual_machine_by_sync_state", _fake_resolver)

    await sync_vm._hydrate_vm_snapshot_with_sidecar_identity(
        object(),
        prepared_vms=[prepared],
        netbox_snapshot=snapshot,
    )
    queue = sync_vm._build_vm_operation_queue([prepared], snapshot)

    assert [op.method for op in queue] == ["UPDATE"]
    assert queue[0].existing_record and queue[0].existing_record["id"] == 6102
    assert queue[0].patch_payload["memory"] == 2048


@pytest.mark.asyncio
async def test_load_netbox_virtual_machine_snapshot_can_bypass_stale_cache(monkeypatch):
    cleared_paths: list[str] = []
    queries: list[dict[str, object]] = []

    def _fake_clear(nb, path):
        cleared_paths.append(path)

    async def _fake_list(nb, path, *, query=None):
        queries.append(dict(query or {}))
        return [{"id": 55, "name": "vm01", "custom_fields": {"proxmox_vm_id": 101}}]

    monkeypatch.setattr(sync_vm, "clear_rest_get_cache_for_path", _fake_clear)
    monkeypatch.setattr(sync_vm, "rest_list_async", _fake_list)

    snapshot = await sync_vm._load_netbox_virtual_machine_snapshot(object(), fresh=True)

    assert cleared_paths == ["/api/virtualization/virtual-machines/"]
    assert queries == [{"limit": 200, "offset": 0}]
    assert snapshot == [{"id": 55, "name": "vm01", "custom_fields": {"proxmox_vm_id": 101}}]


@pytest.mark.asyncio
async def test_dispatch_vm_operation_queue_retries_disk_aggregate_validation(monkeypatch):
    patch_payloads: list[dict[str, object]] = []

    monkeypatch.setattr(sync_vm, "resolve_netbox_write_concurrency", lambda: 1)

    async def _fake_patch(nb, path, record_id, payload):
        patch_payloads.append(dict(payload))
        if len(patch_payloads) == 1:
            raise ProxboxException(
                message="NetBox REST request failed",
                detail=(
                    '{"disk":["The specified disk size (2252) must match the aggregate size '
                    'of assigned virtual disks (2256)."]}'
                ),
            )
        return {"id": record_id, **payload}

    monkeypatch.setattr(sync_vm, "rest_patch_async", _fake_patch)

    prepared_update = _prepared_vm(cluster_name="cluster-a", vmid=204, memory=4096)
    queue = [
        sync_vm._NetBoxVMOperation(
            method="UPDATE",
            prepared=prepared_update,
            existing_record={
                "id": 4204,
                "custom_fields": {"proxmox_vm_id": 204},
                "disk": 2256,
            },
            patch_payload={"memory": 4096, "disk": 2252},
        )
    ]

    resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(object(), queue)
    assert failed_keys == set()

    assert patch_payloads == [
        {"memory": 4096, "disk": 2252},
        {"memory": 4096, "disk": 2256},
    ]
    assert resolved[("cluster-a", 204, "qemu")]["disk"] == 2256


@pytest.mark.asyncio
async def test_dispatch_vm_operation_queue_keeps_same_vmid_types_separate(monkeypatch):
    monkeypatch.setattr(sync_vm, "resolve_netbox_write_concurrency", lambda: 2)

    prepared_qemu = _prepared_vm(cluster_name="cluster-a", vmid=300, memory=2048)
    prepared_lxc = _prepared_vm(cluster_name="cluster-a", vmid=300, memory=2048)
    prepared_lxc.resource["type"] = "lxc"
    prepared_lxc.vm_type = "lxc"

    queue = [
        sync_vm._NetBoxVMOperation(
            method="GET",
            prepared=prepared_qemu,
            existing_record={"id": 5300, "custom_fields": {"proxmox_vm_id": 300}},
        ),
        sync_vm._NetBoxVMOperation(
            method="GET",
            prepared=prepared_lxc,
            existing_record={"id": 6300, "custom_fields": {"proxmox_vm_id": 300}},
        ),
    ]

    resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(object(), queue)
    assert failed_keys == set()

    assert resolved[("cluster-a", 300, "qemu")]["id"] == 5300
    assert resolved[("cluster-a", 300, "lxc")]["id"] == 6300


@pytest.mark.asyncio
async def test_dispatch_vm_operation_queue_isolates_failed_operation(monkeypatch):
    # One operation failing must not abort the whole queue: its key is reported
    # in failed_keys and the remaining operations still resolve.
    monkeypatch.setattr(sync_vm, "resolve_netbox_write_concurrency", lambda: 4)

    async def _fake_create(nb, path, payload, *, lookup=None):
        vmid = payload["custom_fields"]["proxmox_vm_id"]
        if vmid == 401:
            raise RuntimeError("netbox create failed")
        return {"id": 3000 + vmid, **payload}

    async def _fake_first(nb, path, query):
        return None

    async def _fake_resolve(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sync_vm, "rest_create_async", _fake_create)
    monkeypatch.setattr(sync_vm, "rest_first_async", _fake_first)
    monkeypatch.setattr(sync_vm, "resolve_virtual_machine_by_sync_state", _fake_resolve)

    prepared_bad = _prepared_vm(cluster_name="cluster-a", vmid=401, memory=2048)
    prepared_ok = _prepared_vm(cluster_name="cluster-a", vmid=402, memory=2048)

    queue = [
        sync_vm._NetBoxVMOperation(method="CREATE", prepared=prepared_bad),
        sync_vm._NetBoxVMOperation(method="CREATE", prepared=prepared_ok),
    ]

    resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(object(), queue)

    assert failed_keys == {("cluster-a", 401, "qemu")}
    assert ("cluster-a", 401, "qemu") not in resolved
    assert resolved[("cluster-a", 402, "qemu")]["id"] == 3402


@pytest.mark.asyncio
async def test_dispatch_vm_operation_queue_empty_queue_returns_empty_results():
    # Empty queue must return immediately with empty resolved_records and failed_keys.
    resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(object(), [])

    assert resolved == {}
    assert failed_keys == set()
