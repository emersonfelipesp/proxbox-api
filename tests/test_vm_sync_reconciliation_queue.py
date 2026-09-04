"""Tests for VM reconciliation queue processing."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from proxbox_api.exception import ProxboxException
from proxbox_api.proxmox_to_netbox.models import ProxmoxVmConfigInput
from proxbox_api.routes.virtualization.virtual_machines import sync_vm
from proxbox_api.services.sync import role_resolution
from proxbox_api.services.sync.sync_state_reader import VMRoleSnapshotScan


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
        "description": "Synced from Proxmox node pve01",
    }
    return sync_vm._PreparedVMState(
        cluster_name=cluster_name,
        resource={"name": f"vm-{vmid}", "vmid": vmid, "type": "qemu"},
        vm_config={},
        vm_config_obj=ProxmoxVmConfigInput.model_validate({}),
        desired_payload=desired_payload,
        lookup={"id": 0},
        now=datetime.now(timezone.utc),
        vm_type="qemu",
        sync_state_fields={"proxmox_endpoint_id": endpoint_id, "proxmox_vm_id": vmid},
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
            "proxmox_endpoint_id": 500,
            "proxmox_vm_id": 102,
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
            "proxmox_endpoint_id": 500,
            "proxmox_vm_id": 103,
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
            "proxmox_endpoint_id": 1,
            "proxmox_vm_id": 105,
            "proxmox_vm_type": "qemu",
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
            "proxmox_endpoint_id": 2,
            "proxmox_vm_id": 105,
            "proxmox_vm_type": "qemu",
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
            "proxmox_endpoint_id": 500,
            "proxmox_vm_id": 104,
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
            "proxmox_endpoint_id": 500,
            "proxmox_vm_id": 105,
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
        netbox_snapshot=[{"id": 2106, "proxmox_endpoint_id": 500, "proxmox_vm_id": 106}],
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
        calls.append(f"create:{payload['name']}")
        create_lookups.append(lookup)
        vmid = int(str(payload["name"]).removeprefix("vm-"))
        return {"id": 3000 + vmid, **payload}

    async def _fake_patch(nb, path, record_id, payload):
        calls.append(f"patch:{record_id}")
        return {"id": record_id, **payload}

    async def _fake_resolve(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sync_vm, "rest_create_async", _fake_create)
    monkeypatch.setattr(sync_vm, "rest_patch_async", _fake_patch)
    monkeypatch.setattr(sync_vm, "resolve_virtual_machine_by_sync_state", _fake_resolve)

    prepared_create = _prepared_vm(cluster_name="cluster-a", vmid=201, memory=2048)
    prepared_get = _prepared_vm(cluster_name="cluster-a", vmid=202, memory=2048)
    prepared_update = _prepared_vm(cluster_name="cluster-a", vmid=203, memory=4096)

    queue = [
        sync_vm._NetBoxVMOperation(method="CREATE", prepared=prepared_create),
        sync_vm._NetBoxVMOperation(
            method="GET",
            prepared=prepared_get,
            existing_record={
                "id": 4202,
                "role": {"id": 20},
            },
        ),
        sync_vm._NetBoxVMOperation(
            method="UPDATE",
            prepared=prepared_update,
            existing_record={
                "id": 4203,
                "role": {"id": 20},
            },
            patch_payload={"memory": 4096},
        ),
    ]

    resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(object(), queue)
    assert failed_keys == set()

    assert calls == ["create:vm-201", "patch:4203"]
    assert create_lookups == [{"id": 0}]
    assert resolved[("cluster-a", 202, "qemu")]["id"] == 4202
    assert resolved[("cluster-a", 201, "qemu")]["id"] == 3201
    assert resolved[("cluster-a", 203, "qemu")]["id"] == 4203


@pytest.mark.asyncio
async def test_dispatch_vm_role_snapshot_policy_preserves_operator_edit_and_backfills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patches: dict[int, dict[str, object]] = {}

    async def _fake_role_snapshots(*_args, **_kwargs) -> VMRoleSnapshotScan:
        return VMRoleSnapshotScan(
            values={7101: 11, 7103: 20},
            unverified_vm_ids=frozenset({7104}),
        )

    async def _fake_patch(_nb, _path, record_id, payload):
        patches[record_id] = dict(payload)
        return {"id": record_id, **payload}

    monkeypatch.setattr(sync_vm, "scan_vm_last_synced_role_ids", _fake_role_snapshots)
    monkeypatch.setattr(sync_vm, "rest_patch_async", _fake_patch)
    monkeypatch.setattr(sync_vm, "resolve_netbox_write_concurrency", lambda: 1)

    operator_edit = _prepared_vm(cluster_name="cluster-a", vmid=701, memory=4096)
    missing_snapshot = _prepared_vm(cluster_name="cluster-a", vmid=702, memory=2048)
    managed_roll_forward = _prepared_vm(cluster_name="cluster-a", vmid=703, memory=2048)
    managed_roll_forward.desired_payload["role"] = 30
    unverified_snapshot = _prepared_vm(cluster_name="cluster-a", vmid=704, memory=2048)
    unverified_snapshot.desired_payload["role"] = 30

    queue = [
        sync_vm._NetBoxVMOperation(
            method="UPDATE",
            prepared=operator_edit,
            existing_record={"id": 7101, "role": {"id": 42}},
            patch_payload={"memory": 4096, "role": 20},
        ),
        sync_vm._NetBoxVMOperation(
            method="GET",
            prepared=missing_snapshot,
            existing_record={"id": 7102, "role": {"id": 42}},
        ),
        sync_vm._NetBoxVMOperation(
            method="GET",
            prepared=managed_roll_forward,
            existing_record={"id": 7103, "role": {"id": 20}},
        ),
        sync_vm._NetBoxVMOperation(
            method="GET",
            prepared=unverified_snapshot,
            existing_record={"id": 7104, "role": {"id": 20}},
        ),
    ]

    resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(
        object(),
        queue,
        overwrite_vm_role=False,
    )

    assert failed_keys == set()
    assert patches == {
        7101: {"memory": 4096},
        7103: {"role": 30},
    }
    assert resolved[("cluster-a", 702, "qemu")]["role"] == {"id": 42}
    assert queue[0].role_snapshot_id_to_write is None
    assert queue[1].role_snapshot_id_to_write == 42
    assert queue[2].role_snapshot_id_to_write == 30
    assert queue[3].role_snapshot_id_to_write is None


@pytest.mark.asyncio
async def test_snapshot_failure_rollback_allows_next_sync_to_retry_role_roll_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed ownership write cannot turn the just-written role into an operator lock."""
    current = {"id": 7150, "role": {"id": 11}}
    current_snapshot_id = 11
    role_patches: list[int | None] = []

    async def _fake_role_snapshots(*_args: object, **_kwargs: object) -> VMRoleSnapshotScan:
        return VMRoleSnapshotScan(values={7150: 11})

    async def _fake_patch(
        _nb: object,
        _path: str,
        _record_id: int,
        payload: dict[str, object],
        **_kwargs: object,
    ) -> dict[str, object]:
        role_id = payload.get("role")
        role_patches.append(role_id if isinstance(role_id, int) else None)
        current["role"] = {"id": role_id} if role_id is not None else None
        return dict(current)

    async def _fake_read(*_args: object, **_kwargs: object) -> dict[str, object]:
        return dict(current)

    async def _fake_snapshot_read(*_args: object, **_kwargs: object):
        return role_resolution.VMRoleSnapshotRead(
            snapshot_id=current_snapshot_id,
            verified=True,
        )

    async def _fake_snapshot_restore(
        *_args: object,
        snapshot_id: int | None,
        **_kwargs: object,
    ) -> None:
        nonlocal current_snapshot_id
        current_snapshot_id = snapshot_id

    async def _failed_snapshot_write() -> None:
        raise RuntimeError("sidecar unavailable")

    monkeypatch.setattr(sync_vm, "scan_vm_last_synced_role_ids", _fake_role_snapshots)
    monkeypatch.setattr(sync_vm, "rest_patch_async", _fake_patch)
    monkeypatch.setattr(sync_vm, "resolve_netbox_write_concurrency", lambda: 1)
    monkeypatch.setattr(role_resolution, "rest_patch_async", _fake_patch)
    monkeypatch.setattr(role_resolution, "rest_first_async", _fake_read)
    monkeypatch.setattr(role_resolution, "read_vm_last_synced_role", _fake_snapshot_read)
    monkeypatch.setattr(role_resolution, "write_vm_role_snapshot_exact", _fake_snapshot_restore)

    first = sync_vm._NetBoxVMOperation(
        method="GET",
        prepared=_prepared_vm(cluster_name="cluster-a", vmid=750, memory=2048),
        existing_record=dict(current),
    )
    _resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(
        object(),
        [first],
        overwrite_vm_role=False,
    )

    assert failed_keys == set()
    assert current["role"] == {"id": 20}
    assert first.role_previous_id == 11
    assert first.role_snapshot_id_to_write == 20
    assert first.role_write_applied

    with pytest.raises(RuntimeError, match="sidecar unavailable"):
        await role_resolution.persist_sync_state_with_role_compensation(
            object(),
            persistence=_failed_snapshot_write(),
            virtual_machine_id=7150,
            previous_role_id=first.role_previous_id,
            previous_snapshot_id=first.role_snapshot_previous_id,
            expected_snapshot_id=first.role_snapshot_id_to_write,
            role_write_applied=first.role_write_applied,
        )

    assert current["role"] == {"id": 11}
    assert current_snapshot_id == 11

    second = sync_vm._NetBoxVMOperation(
        method="GET",
        prepared=_prepared_vm(cluster_name="cluster-a", vmid=750, memory=2048),
        existing_record=dict(current),
    )
    _resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(
        object(),
        [second],
        overwrite_vm_role=False,
    )

    assert failed_keys == set()
    assert current["role"] == {"id": 20}
    assert second.role_snapshot_id_to_write == 20
    assert second.role_write_applied
    assert role_patches == [20, 11, 20]


@pytest.mark.asyncio
async def test_commit_then_error_confirmation_avoids_rollback_and_false_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost response after snapshot commit is authoritatively accepted as success."""
    current = {"id": 7151, "role": {"id": 11}}
    current_snapshot_id = 11
    role_patches: list[int | None] = []
    snapshot_restore_calls = 0

    async def _fake_role_snapshots(*_args: object, **_kwargs: object) -> VMRoleSnapshotScan:
        return VMRoleSnapshotScan(values={7151: current_snapshot_id})

    async def _fake_patch(
        _nb: object,
        _path: str,
        _record_id: int,
        payload: dict[str, object],
        **_kwargs: object,
    ) -> dict[str, object]:
        role_id = payload.get("role")
        role_patches.append(role_id if isinstance(role_id, int) else None)
        current["role"] = {"id": role_id} if role_id is not None else None
        return dict(current)

    async def _snapshot_read(*_args: object, **_kwargs: object):
        return role_resolution.VMRoleSnapshotRead(
            snapshot_id=current_snapshot_id,
            verified=True,
        )

    async def _unexpected_snapshot_restore(*_args: object, **_kwargs: object) -> None:
        nonlocal snapshot_restore_calls
        snapshot_restore_calls += 1

    async def _commit_then_error() -> None:
        nonlocal current_snapshot_id
        current_snapshot_id = 20
        raise RuntimeError("response lost after commit")

    monkeypatch.setattr(sync_vm, "scan_vm_last_synced_role_ids", _fake_role_snapshots)
    monkeypatch.setattr(sync_vm, "rest_patch_async", _fake_patch)
    monkeypatch.setattr(sync_vm, "resolve_netbox_write_concurrency", lambda: 1)
    monkeypatch.setattr(role_resolution, "read_vm_last_synced_role", _snapshot_read)
    monkeypatch.setattr(
        role_resolution,
        "write_vm_role_snapshot_exact",
        _unexpected_snapshot_restore,
    )

    first = sync_vm._NetBoxVMOperation(
        method="GET",
        prepared=_prepared_vm(cluster_name="cluster-a", vmid=751, memory=2048),
        existing_record=dict(current),
    )
    _resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(
        object(),
        [first],
        overwrite_vm_role=False,
    )

    assert failed_keys == set()
    assert current["role"] == {"id": 20}
    assert first.role_snapshot_previous_id == 11

    await role_resolution.persist_sync_state_with_role_compensation(
        object(),
        persistence=_commit_then_error(),
        virtual_machine_id=7151,
        previous_role_id=first.role_previous_id,
        previous_snapshot_id=first.role_snapshot_previous_id,
        expected_snapshot_id=first.role_snapshot_id_to_write,
        role_write_applied=first.role_write_applied,
    )

    assert current_snapshot_id == 20
    assert snapshot_restore_calls == 0

    second = sync_vm._NetBoxVMOperation(
        method="GET",
        prepared=_prepared_vm(cluster_name="cluster-a", vmid=751, memory=2048),
        existing_record=dict(current),
    )
    _resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(
        object(),
        [second],
        overwrite_vm_role=False,
    )

    assert failed_keys == set()
    assert not second.role_write_applied
    assert second.role_snapshot_id_to_write is None
    assert role_patches == [20]


@pytest.mark.asyncio
async def test_dispatch_create_re_adopts_sidecar_vm(monkeypatch):
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
                    "role": {"id": 42},
                    "vcpus": 2,
                    "memory": 1024,
                    "disk": 30,
                    "tags": [{"id": 99}],
                    "description": "old",
                },
                "record_id": 6101,
                "source": "sidecar",
            },
        )()

    async def _fake_reconcile(*_args, **kwargs):
        assert "role" not in kwargs["patchable_fields"]
        calls.append(kwargs["existing_record"]["id"])
        return {"id": kwargs["existing_record"]["id"], **kwargs["payload"]}

    async def _fake_role_snapshots(*_args, **_kwargs) -> VMRoleSnapshotScan:
        return VMRoleSnapshotScan(values={6101: 11})

    monkeypatch.setattr(sync_vm, "rest_create_async", _unexpected_create)
    monkeypatch.setattr(sync_vm, "rest_reconcile_async", _fake_reconcile)
    monkeypatch.setattr(sync_vm, "resolve_virtual_machine_by_sync_state", _fake_resolver)
    monkeypatch.setattr(sync_vm, "scan_vm_last_synced_role_ids", _fake_role_snapshots)
    monkeypatch.setattr(sync_vm, "resolve_netbox_write_concurrency", lambda: 1)

    prepared = _prepared_vm(cluster_name="cluster-a", vmid=301, memory=2048)
    queue = [sync_vm._NetBoxVMOperation(method="CREATE", prepared=prepared)]

    resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(
        object(),
        queue,
        overwrite_vm_role=False,
    )

    assert failed_keys == set()
    assert calls == [6101]
    assert resolved[("cluster-a", 301, "qemu")]["id"] == 6101
    assert resolved[("cluster-a", 301, "qemu")]["memory"] == 2048
    assert queue[0].role_snapshot_id_to_write is None


@pytest.mark.asyncio
async def test_sidecar_hydration_prevents_name_prepass_rename_for_cf_absent_vm(monkeypatch):
    prepared = _prepared_vm(cluster_name="cluster-a", vmid=301, memory=2048)
    snapshot = [
        {
            "id": 6101,
            "name": "vm-301",
            "cluster": {"id": 1, "name": "cluster-a"},
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
    assert snapshot[0]["proxmox_endpoint_id"] == 500
    assert snapshot[0]["proxmox_vm_id"] == 301
    assert snapshot[0]["proxmox_vm_type"] == "qemu"


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
    page_sizes: list[int | None] = []

    def _fake_clear(nb, path):
        cleared_paths.append(path)

    async def _fake_list(nb, path, *, page_size=None):
        page_sizes.append(page_size)
        return [{"id": 55, "name": "vm01"}]

    monkeypatch.setattr(sync_vm, "clear_rest_get_cache_for_path", _fake_clear)
    monkeypatch.setattr(sync_vm, "rest_list_paginated_async", _fake_list)

    snapshot = await sync_vm._load_netbox_virtual_machine_snapshot(object(), fresh=True)

    assert cleared_paths == ["/api/virtualization/virtual-machines/"]
    assert page_sizes == [200]
    assert snapshot == [{"id": 55, "name": "vm01"}]


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
                "role": {"id": 20},
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
            existing_record={
                "id": 5300,
                "role": {"id": 20},
            },
        ),
        sync_vm._NetBoxVMOperation(
            method="GET",
            prepared=prepared_lxc,
            existing_record={
                "id": 6300,
                "role": {"id": 20},
            },
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
        vmid = int(str(payload["name"]).removeprefix("vm-"))
        if vmid == 401:
            raise RuntimeError("netbox create failed")
        return {"id": 3000 + vmid, **payload}

    async def _fake_resolve(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sync_vm, "rest_create_async", _fake_create)
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
