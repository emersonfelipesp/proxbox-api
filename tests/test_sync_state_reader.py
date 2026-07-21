"""Tests for sidecar-aware sync-state reads with legacy custom-field fallback."""

from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from typing import Any

import pytest

from proxbox_api.constants import DISCOVERY_TAG_VM_QEMU
from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import _extract_payload
from proxbox_api.proxmox_to_netbox.models import ProxmoxVmConfigInput
from proxbox_api.routes.virtualization.virtual_machines import sync_vm
from proxbox_api.services import custom_fields as custom_fields_service
from proxbox_api.services.sync import orphan_sweep, sync_state_reader
from proxbox_api.services.sync.sync_state_reader import (
    VIRTUAL_MACHINES_PATH,
    VM_SYNC_STATE_PATH,
    SidecarVMOrphanScan,
    list_stale_vm_sidecar_candidates,
    resolve_virtual_machine_by_sync_state,
)
from proxbox_api.services.sync.sync_state_writer import _is_sidecar_unavailable
from proxbox_api.services.sync.vm_helpers import LAST_RUN_ID_CUSTOM_FIELD


class _FakeResponse:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.text = body

    def json(self) -> object:
        return _json.loads(self.text)


@pytest.fixture(autouse=True)
def enable_legacy_custom_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        custom_fields_service,
        "get_plugin_bool",
        lambda *, settings_key, default: True,
    )


def test_sidecar_unavailable_detection_is_limited_to_absent_route_statuses() -> None:
    assert _is_sidecar_unavailable(
        ProxboxException(message="Not found", detail="Not found.", http_status_code=404)
    )
    assert _is_sidecar_unavailable(
        ProxboxException(message="Not implemented", detail="Not implemented.", http_status_code=501)
    )
    assert not _is_sidecar_unavailable(ProxboxException(message="Not found", detail="HTTP 404"))
    assert not _is_sidecar_unavailable(
        ProxboxException(
            message="NetBox REST request failed",
            detail="HTTP 503 Service Unavailable",
            http_status_code=503,
        )
    )
    assert not _is_sidecar_unavailable(RuntimeError("sidecar endpoint unavailable"))
    assert not _is_sidecar_unavailable(RuntimeError("route not found without HTTP status"))


def test_extract_payload_preserves_sidecar_absent_statuses() -> None:
    for status in (404, 501):
        with pytest.raises(ProxboxException) as exc_info:
            _extract_payload(_FakeResponse(status, _json.dumps({"detail": "Not found."})))

        assert exc_info.value.http_status_code == status
        assert _is_sidecar_unavailable(exc_info.value)


@pytest.mark.asyncio
async def test_vm_identity_resolver_prefers_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def _fake_list(_nb: object, path: str, *, query: dict[str, object] | None = None):
        calls.append((path, dict(query or {})))
        if path == VM_SYNC_STATE_PATH:
            return [
                {
                    "id": 1,
                    "virtual_machine": {"id": 42},
                    "proxmox_vm_id": 101,
                    "proxmox_endpoint_raw_id": 500,
                }
            ]
        assert path == VIRTUAL_MACHINES_PATH
        assert query == {
            "cf_proxmox_vm_id": 101,
            "cf_proxmox_endpoint_id": 500,
            "limit": 2,
        }
        return []

    async def _fake_first(_nb: object, path: str, *, query: dict[str, object] | None = None):
        calls.append((path, dict(query or {})))
        assert path == VIRTUAL_MACHINES_PATH
        assert query == {"id": 42, "limit": 2}
        return {"id": 42, "name": "vm-101", "custom_fields": {}}

    monkeypatch.setattr(sync_state_reader, "rest_list_async", _fake_list)
    monkeypatch.setattr(sync_state_reader, "rest_first_async", _fake_first)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    resolved = await resolve_virtual_machine_by_sync_state(
        object(),
        proxmox_vm_id=101,
        endpoint_id=500,
    )

    assert resolved is not None
    assert resolved.record_id == 42
    assert resolved.source == "sidecar"
    assert calls[0] == (
        VM_SYNC_STATE_PATH,
        {"proxmox_vm_id": 101, "proxmox_endpoint_raw_id": 500, "limit": 2},
    )
    assert calls[1] == (
        VIRTUAL_MACHINES_PATH,
        {"cf_proxmox_vm_id": 101, "cf_proxmox_endpoint_id": 500, "limit": 2},
    )


@pytest.mark.asyncio
async def test_vm_identity_resolver_returns_none_when_sidecar_and_cf_union_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_list(_nb: object, path: str, *, query: dict[str, object] | None = None):
        if path == VM_SYNC_STATE_PATH:
            assert query == {
                "proxmox_vm_id": 101,
                "proxmox_endpoint_raw_id": 500,
                "limit": 2,
            }
            return [
                {
                    "id": 1,
                    "virtual_machine": {"id": 42},
                    "proxmox_vm_id": 101,
                    "proxmox_endpoint_raw_id": 500,
                }
            ]
        assert path == VIRTUAL_MACHINES_PATH
        assert query == {
            "cf_proxmox_vm_id": 101,
            "cf_proxmox_endpoint_id": 500,
            "limit": 2,
        }
        return [
            {
                "id": 43,
                "name": "cf-only-vm-101",
                "custom_fields": {"proxmox_vm_id": 101, "proxmox_endpoint_id": 500},
            }
        ]

    async def _unexpected_first(*_args: Any, **_kwargs: Any):
        raise AssertionError("ambiguous sidecar/CF union must not fetch or bind a VM")

    monkeypatch.setattr(sync_state_reader, "rest_list_async", _fake_list)
    monkeypatch.setattr(sync_state_reader, "rest_first_async", _unexpected_first)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    resolved = await resolve_virtual_machine_by_sync_state(
        object(),
        proxmox_vm_id=101,
        endpoint_id=500,
    )

    assert resolved is None


@pytest.mark.asyncio
async def test_vm_identity_resolver_adopts_cluster_verified_sidecar_when_cf_lookup_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def _fake_list(_nb: object, path: str, *, query: dict[str, object] | None = None):
        calls.append((path, dict(query or {})))
        if path == VM_SYNC_STATE_PATH:
            return [
                {
                    "id": 1,
                    "virtual_machine": {"id": 42},
                    "proxmox_vm_id": 101,
                    "proxmox_endpoint_raw_id": 500,
                }
            ]
        assert path == VIRTUAL_MACHINES_PATH
        assert query == {
            "cf_proxmox_vm_id": 101,
            "cf_proxmox_endpoint_id": 500,
            "limit": 2,
        }
        raise ProxboxException(
            message="NetBox REST request failed",
            detail="unknown filter/field: cf_proxmox_vm_id",
        )

    async def _fake_first(_nb: object, path: str, *, query: dict[str, object] | None = None):
        calls.append((path, dict(query or {})))
        assert path == VIRTUAL_MACHINES_PATH
        assert query == {"id": 42, "limit": 2}
        return {"id": 42, "name": "vm-101", "cluster": {"id": 10}, "custom_fields": {}}

    monkeypatch.setattr(sync_state_reader, "rest_list_async", _fake_list)
    monkeypatch.setattr(sync_state_reader, "rest_first_async", _fake_first)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    resolved = await resolve_virtual_machine_by_sync_state(
        object(),
        proxmox_vm_id=101,
        endpoint_id=500,
        cluster_id=10,
    )

    assert resolved is not None
    assert resolved.record_id == 42
    assert resolved.source == "sidecar"
    assert calls == [
        (
            VM_SYNC_STATE_PATH,
            {"proxmox_vm_id": 101, "proxmox_endpoint_raw_id": 500, "limit": 50},
        ),
        (VIRTUAL_MACHINES_PATH, {"id": 42, "limit": 2}),
        (
            VIRTUAL_MACHINES_PATH,
            {"cf_proxmox_vm_id": 101, "cf_proxmox_endpoint_id": 500, "limit": 2},
        ),
    ]


@pytest.mark.asyncio
async def test_vm_identity_resolver_refuses_cluster_verified_sidecar_on_distinct_cf_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_list(_nb: object, path: str, *, query: dict[str, object] | None = None):
        if path == VM_SYNC_STATE_PATH:
            return [
                {
                    "id": 1,
                    "virtual_machine": {"id": 42},
                    "proxmox_vm_id": 101,
                    "proxmox_endpoint_raw_id": 500,
                }
            ]
        assert path == VIRTUAL_MACHINES_PATH
        assert query == {
            "cf_proxmox_vm_id": 101,
            "cf_proxmox_endpoint_id": 500,
            "limit": 2,
        }
        return [
            {
                "id": 43,
                "name": "conflicting-cf-vm",
                "cluster": {"id": 10},
                "custom_fields": {"proxmox_vm_id": 101, "proxmox_endpoint_id": 500},
            }
        ]

    async def _fake_first(_nb: object, path: str, *, query: dict[str, object] | None = None):
        assert path == VIRTUAL_MACHINES_PATH
        assert query == {"id": 42, "limit": 2}
        return {"id": 42, "name": "sidecar-vm", "cluster": {"id": 10}, "custom_fields": {}}

    monkeypatch.setattr(sync_state_reader, "rest_list_async", _fake_list)
    monkeypatch.setattr(sync_state_reader, "rest_first_async", _fake_first)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    resolved = await resolve_virtual_machine_by_sync_state(
        object(),
        proxmox_vm_id=101,
        endpoint_id=500,
        cluster_id=10,
    )

    assert resolved is None


@pytest.mark.asyncio
async def test_vm_identity_resolver_rejects_mismatched_sidecar_raw_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def _fake_list(_nb: object, path: str, *, query: dict[str, object] | None = None):
        calls.append((path, dict(query or {})))
        if path == VM_SYNC_STATE_PATH:
            return [
                {
                    "id": 1,
                    "virtual_machine": {"id": 42},
                    "proxmox_vm_id": 101,
                    "proxmox_endpoint_raw_id": 999,
                }
            ]
        assert path == VIRTUAL_MACHINES_PATH
        assert query == {"cf_proxmox_vm_id": 101, "cf_proxmox_endpoint_id": 500, "limit": 2}
        return []

    async def _fake_first(_nb: object, path: str, *, query: dict[str, object] | None = None):
        raise AssertionError("mismatched sidecar with no CF match must not fetch a VM")

    monkeypatch.setattr(sync_state_reader, "rest_list_async", _fake_list)
    monkeypatch.setattr(sync_state_reader, "rest_first_async", _fake_first)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    resolved = await resolve_virtual_machine_by_sync_state(
        object(),
        proxmox_vm_id=101,
        endpoint_id=500,
    )

    assert resolved is None
    assert calls[0] == (
        VM_SYNC_STATE_PATH,
        {"proxmox_vm_id": 101, "proxmox_endpoint_raw_id": 500, "limit": 2},
    )


@pytest.mark.asyncio
async def test_vm_identity_resolver_rejects_sidecar_candidate_from_other_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def _fake_list(_nb: object, path: str, *, query: dict[str, object] | None = None):
        calls.append((path, dict(query or {})))
        if path == VM_SYNC_STATE_PATH:
            return [{"id": 1, "virtual_machine": {"id": 42}, "proxmox_vm_id": 101}]
        assert path == VIRTUAL_MACHINES_PATH
        assert query == {"cf_proxmox_vm_id": 101, "cluster_id": 10, "limit": 2}
        return []

    async def _fake_first(_nb: object, path: str, *, query: dict[str, object] | None = None):
        calls.append((path, dict(query or {})))
        assert path == VIRTUAL_MACHINES_PATH
        assert query == {"id": 42, "limit": 2}
        return {"id": 42, "name": "other-cluster-vm", "cluster": {"id": 99}}

    monkeypatch.setattr(sync_state_reader, "rest_list_async", _fake_list)
    monkeypatch.setattr(sync_state_reader, "rest_first_async", _fake_first)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    resolved = await resolve_virtual_machine_by_sync_state(
        object(),
        proxmox_vm_id=101,
        endpoint_id=None,
        cluster_id=10,
        fallback_query={"cf_proxmox_vm_id": 101, "cluster_id": 10},
    )

    assert resolved is None
    assert calls[0] == (VM_SYNC_STATE_PATH, {"proxmox_vm_id": 101, "limit": 50})
    assert calls[1] == (VIRTUAL_MACHINES_PATH, {"id": 42, "limit": 2})


@pytest.mark.asyncio
async def test_vm_identity_resolver_falls_back_to_custom_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def _fake_list(_nb: object, path: str, *, query: dict[str, object] | None = None):
        calls.append((path, dict(query or {})))
        if path == VM_SYNC_STATE_PATH:
            return []
        assert path == VIRTUAL_MACHINES_PATH
        assert query == {"cf_proxmox_vm_id": 101, "cf_proxmox_endpoint_id": 500, "limit": 2}
        return [
            {
                "id": 43,
                "name": "vm-101",
                "custom_fields": {"proxmox_vm_id": 101, "proxmox_endpoint_id": 500},
            }
        ]

    async def _fake_first(_nb: object, path: str, *, query: dict[str, object] | None = None):
        raise AssertionError("CF list match already includes the VM record")

    monkeypatch.setattr(sync_state_reader, "rest_list_async", _fake_list)
    monkeypatch.setattr(sync_state_reader, "rest_first_async", _fake_first)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    resolved = await resolve_virtual_machine_by_sync_state(
        object(),
        proxmox_vm_id=101,
        endpoint_id=500,
    )

    assert resolved is not None
    assert resolved.record_id == 43
    assert resolved.source == "custom_fields"


@pytest.mark.asyncio
async def test_vm_identity_resolver_returns_none_when_sidecar_and_cf_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_list(_nb: object, _path: str, *, query: dict[str, object] | None = None):
        return []

    async def _fake_first(_nb: object, _path: str, *, query: dict[str, object] | None = None):
        return None

    monkeypatch.setattr(sync_state_reader, "rest_list_async", _fake_list)
    monkeypatch.setattr(sync_state_reader, "rest_first_async", _fake_first)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    resolved = await resolve_virtual_machine_by_sync_state(
        object(),
        proxmox_vm_id=101,
        endpoint_id=500,
    )

    assert resolved is None


@pytest.mark.asyncio
async def test_orphan_sweep_sidecar_current_run_overrides_stale_custom_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_vm = {
        "id": 10,
        "name": "vm-stale-cf",
        "custom_fields": {LAST_RUN_ID_CUSTOM_FIELD: "old-run", "proxmox_vm_id": 101},
        "tags": [{"slug": DISCOVERY_TAG_VM_QEMU}],
    }

    async def _fake_sidecar_scan(*_args: Any, **_kwargs: Any):
        return SidecarVMOrphanScan(stale_candidates=[], current_vm_ids=set())

    async def _fake_last_run(*_args: Any, **_kwargs: Any):
        return "current-run"

    async def _fake_cf_list(
        _nb: object,
        _path: str,
        *,
        base_query: dict[str, object],
        **_: Any,
    ):
        if base_query.get(f"cf_{LAST_RUN_ID_CUSTOM_FIELD}__nie") == "current-run":
            return [stale_vm]
        return []

    monkeypatch.setattr(orphan_sweep, "scan_vm_sidecar_orphan_candidates", _fake_sidecar_scan)
    monkeypatch.setattr(orphan_sweep, "resolve_vm_last_run_id", _fake_last_run)
    monkeypatch.setattr(orphan_sweep, "rest_list_paginated_async", _fake_cf_list)

    candidates = await orphan_sweep.find_orphan_vms(object(), "current-run")

    assert candidates == []


@pytest.mark.asyncio
async def test_stale_sidecar_candidates_filter_last_run_client_side(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def _fake_paginated(
        _nb: object,
        path: str,
        *,
        base_query: dict[str, object],
        **_: Any,
    ):
        assert path == VM_SYNC_STATE_PATH
        calls.append(dict(base_query))
        return [
            {"id": 1, "virtual_machine": {"id": 10}, "last_run_id": "current-run"},
            {"id": 2, "virtual_machine": {"id": 11}, "last_run_id": "old-run"},
            {"id": 3, "virtual_machine": {"id": 12}, "last_run_id": ""},
            {"id": 4, "virtual_machine": {"id": 13}},
        ]

    async def _fake_first(_nb: object, path: str, *, query: dict[str, object] | None = None):
        assert path == VIRTUAL_MACHINES_PATH
        vm_id = query["id"] if query is not None else None
        return {
            "id": vm_id,
            "name": f"vm-{vm_id}",
            "tags": [{"slug": DISCOVERY_TAG_VM_QEMU}],
            "custom_fields": {},
        }

    monkeypatch.setattr(sync_state_reader, "rest_list_paginated_async", _fake_paginated)
    monkeypatch.setattr(sync_state_reader, "rest_first_async", _fake_first)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    candidates = await list_stale_vm_sidecar_candidates(
        object(),
        run_id="current-run",
        vm_slugs=(DISCOVERY_TAG_VM_QEMU,),
    )

    assert calls == [{}]
    assert [candidate["id"] for candidate in candidates or []] == [11, 12]


@pytest.mark.asyncio
async def test_dispatch_create_does_not_create_when_sync_state_resolution_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_resolver(*_args: Any, **kwargs: Any):
        assert kwargs["fail_on_ambiguous"] is True
        raise ProxboxException(
            message="Refusing to create or bind a VM from ambiguous sync-state identity."
        )

    async def _unexpected_create(*_args: Any, **_kwargs: Any):
        raise AssertionError("ambiguous/refused sync-state lookup must not create")

    monkeypatch.setattr(sync_vm, "resolve_virtual_machine_by_sync_state", _fake_resolver)
    monkeypatch.setattr(sync_vm, "rest_create_async", _unexpected_create)
    monkeypatch.setattr(sync_vm, "resolve_netbox_write_concurrency", lambda: 1)

    prepared = sync_vm._PreparedVMState(
        cluster_name="cluster-a",
        resource={"name": "vm-101", "vmid": 101, "type": "qemu"},
        vm_config={},
        vm_config_obj=ProxmoxVmConfigInput.model_validate({}),
        desired_payload={
            "name": "vm-101",
            "status": "active",
            "cluster": 10,
            "device": 20,
            "role": 30,
            "vcpus": 2,
            "memory": 2048,
            "disk": 30,
            "tags": [99],
            "custom_fields": {"proxmox_endpoint_id": 500, "proxmox_vm_id": 101},
        },
        lookup={"cf_proxmox_vm_id": 101, "cf_proxmox_endpoint_id": 500},
        now=datetime.now(timezone.utc),
        vm_type="qemu",
    )

    resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(
        object(),
        [sync_vm._NetBoxVMOperation(method="CREATE", prepared=prepared)],
    )

    assert resolved == {}
    assert failed_keys == {("cluster-a", 101, "qemu")}


@pytest.mark.asyncio
async def test_dispatch_create_refuses_when_old_plugin_cf_lookup_is_unverifiable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_list(_nb: object, path: str, *, query: dict[str, object] | None = None):
        if path == VM_SYNC_STATE_PATH:
            _extract_payload(_FakeResponse(404, _json.dumps({"detail": "Not found."})))
        assert path == VIRTUAL_MACHINES_PATH
        assert query == {
            "cf_proxmox_vm_id": 101,
            "cf_proxmox_endpoint_id": 500,
            "limit": 2,
        }
        raise ProxboxException(
            message="NetBox REST request failed",
            detail="HTTP 503 Service Unavailable",
            http_status_code=503,
        )

    async def _unexpected_create(*_args: Any, **_kwargs: Any):
        raise AssertionError("unverifiable legacy CF lookup must not create")

    monkeypatch.setattr(sync_state_reader, "rest_list_async", _fake_list)
    monkeypatch.setattr(sync_vm, "rest_create_async", _unexpected_create)
    monkeypatch.setattr(sync_vm, "resolve_netbox_write_concurrency", lambda: 1)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    prepared = sync_vm._PreparedVMState(
        cluster_name="cluster-a",
        resource={"name": "vm-101", "vmid": 101, "type": "qemu"},
        vm_config={},
        vm_config_obj=ProxmoxVmConfigInput.model_validate({}),
        desired_payload={
            "name": "vm-101",
            "status": "active",
            "cluster": 10,
            "device": 20,
            "role": 30,
            "vcpus": 2,
            "memory": 2048,
            "disk": 30,
            "tags": [99],
            "custom_fields": {"proxmox_endpoint_id": 500, "proxmox_vm_id": 101},
        },
        lookup={"cf_proxmox_vm_id": 101, "cf_proxmox_endpoint_id": 500},
        now=datetime.now(timezone.utc),
        vm_type="qemu",
    )

    resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(
        object(),
        [sync_vm._NetBoxVMOperation(method="CREATE", prepared=prepared)],
    )

    assert resolved == {}
    assert failed_keys == {("cluster-a", 101, "qemu")}


@pytest.mark.asyncio
async def test_dispatch_create_proceeds_when_sidecar_404_enters_legacy_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_list(_nb: object, path: str, *, query: dict[str, object] | None = None):
        if path == VM_SYNC_STATE_PATH:
            _extract_payload(_FakeResponse(404, _json.dumps({"detail": "Not found."})))
        assert path == VIRTUAL_MACHINES_PATH
        assert query == {
            "cf_proxmox_vm_id": 101,
            "cf_proxmox_endpoint_id": 500,
            "limit": 2,
        }
        return []

    async def _fake_create(
        _nb: object,
        path: str,
        payload: dict[str, object],
        *,
        lookup: dict[str, object],
    ):
        assert path == VIRTUAL_MACHINES_PATH
        assert lookup == {"cf_proxmox_vm_id": 101, "cf_proxmox_endpoint_id": 500}
        return {"id": 88, **payload}

    async def _unexpected_first(*_args: Any, **_kwargs: Any):
        raise AssertionError("first-time old-plugin create should not need a fallback GET")

    monkeypatch.setattr(sync_state_reader, "rest_list_async", _fake_list)
    monkeypatch.setattr(sync_vm, "rest_create_async", _fake_create)
    monkeypatch.setattr(sync_vm, "rest_first_async", _unexpected_first)
    monkeypatch.setattr(sync_vm, "resolve_netbox_write_concurrency", lambda: 1)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    prepared = sync_vm._PreparedVMState(
        cluster_name="cluster-a",
        resource={"name": "vm-101", "vmid": 101, "type": "qemu"},
        vm_config={},
        vm_config_obj=ProxmoxVmConfigInput.model_validate({}),
        desired_payload={
            "name": "vm-101",
            "status": "active",
            "cluster": 10,
            "device": 20,
            "role": 30,
            "vcpus": 2,
            "memory": 2048,
            "disk": 30,
            "tags": [99],
            "custom_fields": {"proxmox_endpoint_id": 500, "proxmox_vm_id": 101},
        },
        lookup={"cf_proxmox_vm_id": 101, "cf_proxmox_endpoint_id": 500},
        now=datetime.now(timezone.utc),
        vm_type="qemu",
    )

    resolved, failed_keys = await sync_vm._dispatch_vm_operation_queue(
        object(),
        [sync_vm._NetBoxVMOperation(method="CREATE", prepared=prepared)],
    )

    assert failed_keys == set()
    assert resolved[("cluster-a", 101, "qemu")]["id"] == 88


@pytest.mark.asyncio
async def test_role_snapshot_resolver_uses_legacy_custom_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected_list(*_args: Any, **_kwargs: Any):
        raise AssertionError("VM sidecar has no role-ownership field to read")

    monkeypatch.setattr(sync_state_reader, "rest_list_async", _unexpected_list)

    result = await sync_state_reader.resolve_vm_last_synced_role_id(
        object(),
        vm_record={"id": 77, "custom_fields": {"proxmox_last_synced_role_id": 11}},
        custom_field_name="proxmox_last_synced_role_id",
    )

    assert result == 11
