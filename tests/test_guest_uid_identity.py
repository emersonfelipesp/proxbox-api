"""Proxmox VE 9.3 cluster and guest UID identity tests."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ConfigDict, Field

from proxbox_api.proxmox_to_netbox.models import ProxmoxVmConfigInput
from proxbox_api.routes.proxmox.cluster import _parse_cluster_status
from proxbox_api.routes.virtualization.virtual_machines import sync_vm
from proxbox_api.services.sync import guest_uid as guest_uid_helpers
from proxbox_api.services.sync import orphan_sweep, sync_state_reader
from proxbox_api.services.sync.guest_uid import extract_cluster_uid, extract_guest_uid
from proxbox_api.services.sync.sync_state_reader import resolve_virtual_machine_by_sync_state

CLUSTER_UID = "cluster-uid-a"
VM_UID = "a234567a234567a23456"
OTHER_VM_UID = "b234567b234567b23456"


class _ExtraRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    name: str = ""
    local: int = 0


class _TypedRow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: str
    name: str = ""
    local: int = 0
    uid: str | None = None
    cluster_id: str | None = Field(default=None, alias="cluster-id")


def _session(*, name: str = "cluster-a", mode: str = "cluster") -> SimpleNamespace:
    return SimpleNamespace(name=name, mode=mode)


def _prepared(*, vmid: int = 401, endpoint_id: int = 500) -> sync_vm._PreparedVMState:
    return sync_vm._PreparedVMState(
        cluster_name="cluster-a",
        resource={"name": f"vm-{vmid}", "vmid": vmid, "type": "qemu"},
        vm_config={},
        vm_config_obj=ProxmoxVmConfigInput.model_validate({}),
        desired_payload={
            "name": f"vm-{vmid}",
            "status": "active",
            "cluster": 1,
            "device": 10,
            "role": 20,
            "vcpus": 2,
            "memory": 2048,
            "disk": 30,
            "tags": [],
            "description": "Synced from Proxmox node pve01",
        },
        lookup={"id": 0},
        now=datetime.now(timezone.utc),
        vm_type="qemu",
        sync_state_fields={"proxmox_endpoint_id": endpoint_id, "proxmox_vm_id": vmid},
        cluster_uid=CLUSTER_UID,
        vm_uid=VM_UID,
    )


def _snapshot(*, vmid: int = 401, endpoint_id: int = 500) -> dict[str, object]:
    return {
        "id": 7001,
        "name": f"vm-{vmid}",
        "status": "active",
        "cluster": {"id": 1, "name": "cluster-a"},
        "device": {"id": 10},
        "role": {"id": 20},
        "vcpus": 2,
        "memory": 2048,
        "disk": 30,
        "tags": [],
        "description": "Synced from Proxmox node pve01",
        "proxmox_endpoint_id": endpoint_id,
        "proxmox_vm_id": vmid,
        "proxmox_vm_type": "qemu",
        "proxmox_cluster_uid": CLUSTER_UID,
        "proxmox_vm_uid": VM_UID,
    }


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (
            [
                _ExtraRow.model_validate(
                    {"type": "cluster", "name": "cluster-a", "cluster-id": CLUSTER_UID}
                ),
                _ExtraRow(type="node", name="pve01", local=1),
            ],
            CLUSTER_UID,
        ),
        (
            [
                _ExtraRow.model_validate(
                    {
                        "type": "node",
                        "name": "pve01",
                        "local": 1,
                        "cluster-id": "standalone-a",
                    }
                )
            ],
            "standalone-a",
        ),
        ([_ExtraRow(type="node", name="pve01", local=1)], None),
    ],
)
def test_cluster_status_parses_cluster_uid_for_all_supported_shapes(rows, expected):
    parsed = _parse_cluster_status(_session(), rows)

    assert parsed.cluster_uid == expected


def test_uid_extractors_support_sdk_extras_and_typed_alias_fields():
    extras = _ExtraRow.model_validate({"type": "qemu", "uid": VM_UID, "cluster-id": CLUSTER_UID})
    typed = _TypedRow(type="qemu", uid=VM_UID, cluster_id=CLUSTER_UID)

    assert extract_guest_uid(extras) == VM_UID
    assert extract_cluster_uid(extras) == CLUSTER_UID
    assert extract_guest_uid(typed) == VM_UID
    assert extract_cluster_uid(typed) == CLUSTER_UID


def test_malformed_guest_uid_is_ignored(monkeypatch):
    messages: list[str] = []

    def _debug(message: str, *args: object) -> None:
        messages.append(message % args)

    monkeypatch.setattr("proxbox_api.services.sync.guest_uid.logger.debug", _debug)

    assert extract_guest_uid(_ExtraRow.model_validate({"type": "qemu", "uid": "bad"})) is None
    assert "Ignoring malformed Proxmox guest uid='bad'" in messages


def test_uid_match_rebinds_after_vmid_change_on_the_same_endpoint():
    prepared = _prepared(vmid=902, endpoint_id=500)
    existing = _snapshot(vmid=401, endpoint_id=500)

    operation = sync_vm._build_vm_operation_queue([prepared], [existing])[0]

    assert operation.method != "CREATE"
    assert operation.existing_record is existing


def test_uid_match_does_not_cross_endpoints_on_its_own():
    # A UID row stored under another endpoint is left to the legacy chain and
    # its stale-endpoint adoption rules; the UID alone never re-binds it.
    prepared = _prepared(vmid=902, endpoint_id=888)
    existing = _snapshot(vmid=401, endpoint_id=500)

    operation = sync_vm._build_vm_operation_queue([prepared], [existing])[0]

    assert operation.existing_record is not existing


def test_vmid_reuse_with_different_uid_creates_a_new_vm():
    prepared = _prepared()
    existing = _snapshot()
    existing["proxmox_vm_uid"] = OTHER_VM_UID

    operation = sync_vm._build_vm_operation_queue([prepared], [existing])[0]

    assert operation.method == "CREATE"
    assert operation.existing_record is None


def test_missing_uid_preserves_legacy_vmid_binding():
    prepared = _prepared()
    prepared.vm_uid = None
    prepared.cluster_uid = None
    existing = _snapshot()
    existing.pop("proxmox_vm_uid")
    existing.pop("proxmox_cluster_uid")

    operation = sync_vm._build_vm_operation_queue([prepared], [existing])[0]

    assert operation.method != "CREATE"
    assert operation.existing_record is existing


def test_cluster_uid_change_keeps_legacy_binding_for_joined_standalone():
    prepared = _prepared()
    existing = _snapshot()
    existing["proxmox_cluster_uid"] = "old-standalone-id"
    existing["proxmox_vm_uid"] = OTHER_VM_UID

    operation = sync_vm._build_vm_operation_queue([prepared], [existing])[0]

    assert operation.method != "CREATE"
    assert operation.existing_record is existing


@pytest.mark.asyncio
async def test_uid_lookup_verifies_unfiltered_server_results_client_side(monkeypatch):
    async def _unfiltered(*_args, **_kwargs):
        return [
            {
                "virtual_machine": {"id": 42},
                "proxmox_cluster_uid": "different-cluster",
                "proxmox_vm_uid": OTHER_VM_UID,
            }
        ]

    async def _legacy_empty(*_args, **_kwargs):
        return []

    async def _must_not_fetch(*_args, **_kwargs):
        raise AssertionError("a client-side UID mismatch must not fetch or bind its VM")

    monkeypatch.setattr(sync_state_reader, "rest_list_paginated_async", _unfiltered)
    monkeypatch.setattr(sync_state_reader, "rest_list_async", _legacy_empty)
    monkeypatch.setattr(sync_state_reader, "rest_first_async", _must_not_fetch)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    resolved = await resolve_virtual_machine_by_sync_state(
        object(),
        proxmox_vm_id=101,
        endpoint_id=500,
        cluster_id=1,
        cluster_uid=CLUSTER_UID,
        vm_uid=VM_UID,
    )

    assert resolved is None


@pytest.mark.asyncio
async def test_filtered_uid_miss_does_not_downgrade_positive_feature_evidence(monkeypatch):
    nb = object()

    async def _empty(*_args, **_kwargs):
        return []

    guest_uid_helpers.reset_vm_uid_feature_cache()
    guest_uid_helpers.note_vm_uid_feature_support(nb, [{"proxmox_vm_uid": VM_UID}])
    monkeypatch.setattr(sync_state_reader, "rest_list_paginated_async", _empty)

    candidates, refused = await sync_state_reader._list_sidecar_vm_uid_candidates(
        nb,
        cluster_uid=CLUSTER_UID,
        vm_uid=VM_UID,
        endpoint_id=500,
        cluster_id=1,
    )

    assert candidates == []
    assert refused is False
    assert guest_uid_helpers.cached_vm_uid_feature_support(nb) is True


@pytest.mark.asyncio
async def test_vmid_reuse_dispatch_and_persistence_keep_live_uid_identity(monkeypatch):
    prepared = _prepared()
    prepared.sync_state_fields.update(
        {
            "proxmox_cluster_uid": CLUSTER_UID,
            "proxmox_vm_uid": VM_UID,
        }
    )
    existing = _snapshot()
    existing["proxmox_vm_uid"] = OTHER_VM_UID
    queue = sync_vm._build_vm_operation_queue([prepared], [existing])
    assert [operation.method for operation in queue] == ["CREATE"]

    async def _not_found(*_args, **_kwargs):
        return None

    async def _create(_nb, _path, payload, *, lookup=None):
        assert lookup == {"id": 0}
        return {"id": 9001, **payload}

    async def _role_snapshots(*_args, **_kwargs):
        return sync_state_reader.VMRoleSnapshotScan(values={})

    monkeypatch.setattr(sync_vm, "resolve_virtual_machine_by_sync_state", _not_found)
    monkeypatch.setattr(sync_vm, "rest_create_async", _create)
    monkeypatch.setattr(sync_vm, "scan_vm_last_synced_role_ids", _role_snapshots)
    resolved, failed = await sync_vm._dispatch_vm_operation_queue(object(), queue)
    assert failed == set()

    captured: dict[str, object] = {}

    async def _write_state(_nb, **kwargs):
        captured.update(kwargs)
        return {"id": 7001}

    async def _persist(_nb, *, persistence, **_kwargs):
        await persistence

    async def _stamp(*_args, **_kwargs):
        return None

    async def _clear(*_args, **_kwargs):
        return True

    monkeypatch.setattr(sync_vm, "write_virtual_machine_sync_state", _write_state)
    monkeypatch.setattr(sync_vm, "persist_sync_state_with_role_compensation", _persist)
    monkeypatch.setattr(sync_vm, "stamp_vm_last_run_id", _stamp)
    monkeypatch.setattr(sync_vm, "_clear_reappeared_vm_marker", _clear)
    context = SimpleNamespace(
        nb=object(),
        overwrite_vm_custom_fields=True,
        effective_run_id="run-uid-reuse",
    )
    record = resolved[("cluster-a", 401, "qemu")]

    assert await sync_vm._persist_one_full_update_vm(context, queue[0], record) is True
    assert captured["custom_fields"]["proxmox_cluster_uid"] == CLUSTER_UID
    assert captured["custom_fields"]["proxmox_vm_uid"] == VM_UID


def test_orphan_presence_distinguishes_reused_vmid_by_uid():
    candidate = {
        "_proxmox_cluster_name": "cluster-a",
        "_proxmox_vm_id": 101,
        "_proxmox_vm_type": "qemu",
        "_proxmox_vm_uid": VM_UID,
    }
    live = {("cluster-a", 101, "qemu", OTHER_VM_UID)}

    assert orphan_sweep._live_presence_skip_reason(candidate, live) is None
    candidate.pop("_proxmox_vm_uid")
    assert (
        orphan_sweep._live_presence_skip_reason(candidate, live)
        == orphan_sweep.ITEM_SKIP_STILL_PRESENT
    )


def _uid_sidecar(*, endpoint_id: object = 500) -> dict[str, object]:
    return {
        "virtual_machine": {"id": 42},
        "proxmox_endpoint_raw_id": endpoint_id,
        "proxmox_cluster_uid": CLUSTER_UID,
        "proxmox_vm_uid": VM_UID,
    }


@pytest.mark.parametrize(
    ("stored", "live", "expected"),
    [
        (999, 500, True),
        (500, 500, False),
        ("500", 500, False),
        (None, 500, False),
        ("", 500, False),
        (999, None, False),
        ({"id": 999}, 500, True),
        (True, 500, False),
    ],
)
def test_uid_candidate_endpoint_conflict(stored, live, expected):
    assert guest_uid_helpers.uid_candidate_endpoint_conflict(stored, live) is expected


@pytest.mark.asyncio
async def test_uid_match_never_binds_a_row_of_another_endpoint(monkeypatch):
    async def _uid_rows(*_args, **_kwargs):
        return [_uid_sidecar(endpoint_id=999)]

    async def _legacy_empty(*_args, **_kwargs):
        return []

    async def _must_not_fetch(*_args, **_kwargs):
        raise AssertionError("a UID row of another endpoint must not be fetched or bound")

    monkeypatch.setattr(sync_state_reader, "rest_list_paginated_async", _uid_rows)
    monkeypatch.setattr(sync_state_reader, "rest_list_async", _legacy_empty)
    monkeypatch.setattr(sync_state_reader, "rest_first_async", _must_not_fetch)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    resolved = await resolve_virtual_machine_by_sync_state(
        object(),
        proxmox_vm_id=101,
        endpoint_id=500,
        cluster_id=1,
        cluster_uid=CLUSTER_UID,
        vm_uid=VM_UID,
    )

    assert resolved is None


@pytest.mark.asyncio
async def test_uid_match_with_unverifiable_cluster_is_refused(monkeypatch):
    async def _uid_rows(*_args, **_kwargs):
        return [_uid_sidecar()]

    async def _record_without_cluster(*_args, **_kwargs):
        return {"id": 42, "name": "vm-101"}

    monkeypatch.setattr(sync_state_reader, "rest_list_paginated_async", _uid_rows)
    monkeypatch.setattr(sync_state_reader, "rest_first_async", _record_without_cluster)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    resolution, refused = await sync_state_reader._resolve_vm_uid_candidate(
        object(),
        cluster_uid=CLUSTER_UID,
        vm_uid=VM_UID,
        endpoint_id=500,
        cluster_id=1,
    )

    assert resolution is None
    assert refused is True


@pytest.mark.asyncio
async def test_uid_lookup_requires_a_verified_netbox_cluster(monkeypatch):
    async def _must_not_list(*_args, **_kwargs):
        raise AssertionError("UID lookup must not run without a NetBox cluster id")

    monkeypatch.setattr(sync_state_reader, "rest_list_paginated_async", _must_not_list)

    resolution, refused = await sync_state_reader._resolve_vm_uid_candidate(
        object(),
        cluster_uid=CLUSTER_UID,
        vm_uid=VM_UID,
        endpoint_id=500,
        cluster_id=None,
    )

    assert resolution is None
    assert refused is False


def test_queue_uid_selection_skips_snapshot_rows_of_another_endpoint():
    from proxbox_api.services.sync.reconciliation import vm_queue

    prepared = _prepared(endpoint_id=500)
    same_endpoint = _snapshot(endpoint_id=500)
    other_endpoint = _snapshot(endpoint_id=999)

    index = vm_queue.build_vm_uid_snapshot_index([other_endpoint])
    assert vm_queue._select_uid_vm_record(prepared, index, cluster_id=1) is None

    index = vm_queue.build_vm_uid_snapshot_index([same_endpoint])
    assert vm_queue._select_uid_vm_record(prepared, index, cluster_id=1) is same_endpoint
