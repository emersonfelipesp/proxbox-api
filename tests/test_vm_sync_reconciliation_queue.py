"""Tests for VM reconciliation queue processing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from proxbox_api.proxmox_to_netbox.models import ProxmoxVmConfigInput
from proxbox_api.routes.virtualization.virtual_machines import sync_vm


def _prepared_vm(*, cluster_name: str, vmid: int, memory: int) -> sync_vm._PreparedVMState:
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
        "custom_fields": {"proxmox_vm_id": vmid},
        "description": "Synced from Proxmox node pve01",
    }
    return sync_vm._PreparedVMState(
        cluster_name=cluster_name,
        resource={"name": f"vm-{vmid}", "vmid": vmid, "type": "qemu"},
        vm_config={},
        vm_config_obj=ProxmoxVmConfigInput.model_validate({}),
        desired_payload=desired_payload,
        lookup={"cf_proxmox_vm_id": vmid, "cluster_id": 1},
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
            "custom_fields": {"proxmox_vm_id": 102},
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
            "custom_fields": {"proxmox_vm_id": 103},
            "description": "Synced from Proxmox node pve01",
        },
    ]

    queue = sync_vm._build_vm_operation_queue(prepared, snapshot)

    assert [op.method for op in queue] == ["CREATE", "GET", "UPDATE"]
    assert queue[2].patch_payload["memory"] == 8192


@pytest.mark.asyncio
async def test_dispatch_vm_operation_queue_runs_writes_sequentially(monkeypatch):
    calls: list[str] = []

    monkeypatch.setattr(sync_vm, "resolve_netbox_write_concurrency", lambda: 2)

    async def _fake_create(nb, path, payload):
        calls.append(f"create:{payload['custom_fields']['proxmox_vm_id']}")
        vmid = payload["custom_fields"]["proxmox_vm_id"]
        return {"id": 3000 + vmid, **payload}

    async def _fake_patch(nb, path, record_id, payload):
        calls.append(f"patch:{record_id}")
        return {"id": record_id, **payload}

    async def _fake_first(nb, path, query):
        return None

    monkeypatch.setattr(sync_vm, "rest_create_async", _fake_create)
    monkeypatch.setattr(sync_vm, "rest_patch_async", _fake_patch)
    monkeypatch.setattr(sync_vm, "rest_first_async", _fake_first)

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

    resolved = await sync_vm._dispatch_vm_operation_queue(object(), queue)

    assert calls == ["create:201", "patch:4203"]
    assert resolved[("cluster-a", 202)]["id"] == 4202
    assert resolved[("cluster-a", 201)]["id"] == 3201
    assert resolved[("cluster-a", 203)]["id"] == 4203
