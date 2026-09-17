"""Regression tests for two-phase full-update VM config fetching."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from proxbox_api.exception import ProxboxException
from proxbox_api.routes.virtualization.virtual_machines import sync_vm
from proxbox_api.schemas.sync import SyncBehaviorFlags, SyncOverwriteFlags
from proxbox_api.services.sync import sync_state_reader, sync_state_writer
from proxbox_api.utils.streaming import WebSocketSSEBridge
from tests.fixtures import PROXMOX_VM_CONFIG, PROXMOX_VM_RESOURCE


@pytest.fixture(autouse=True)
def _bridge_vm_snapshot_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _inline_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)

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

    monkeypatch.setattr(sync_vm, "rest_list_paginated_async", _legacy_vm_snapshot_bridge)


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


def _existing_vm_snapshot(*, name: str, vmid: int = 101, record_id: int = 55) -> dict[str, object]:
    return {
        "id": record_id,
        "name": name,
        "status": "active",
        "cluster": {"id": 1, "name": "cluster-a"},
        "device": {"id": 1},
        "role": None,
        "vcpus": 1,
        "memory": 1024,
        "disk": 0,
        "tags": [{"id": 5}],
        "description": "Synced from Proxmox node pve01",
    }


class _FakeConfigResource:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def get(self) -> dict[str, object]:
        return dict(self._payload)


class _FakeVMResource:
    def __init__(self, payload_factory, vmid: int) -> None:
        self.config = _FakeConfigResource(payload_factory(vmid))


class _FakeNodeResource:
    def __init__(self, payload_factory) -> None:
        self._payload_factory = payload_factory

    def qemu(self, vmid: int) -> _FakeVMResource:
        return _FakeVMResource(self._payload_factory, vmid)


class _FakeSDKClient:
    def __init__(self, payload_factory) -> None:
        self._payload_factory = payload_factory

    def nodes(self, _node: str) -> _FakeNodeResource:
        return _FakeNodeResource(self._payload_factory)


class _MixedBatchHarness:
    def __init__(self) -> None:
        self.prepared: list[tuple[str, int, str]] = []
        self.sync_identities: list[tuple[int | None, int, str]] = []
        self.vm_create_payloads: list[dict[str, object]] = []
        self.vm_patch_calls: list[tuple[int, dict[str, object]]] = []
        self.sidecar_patch_calls: list[tuple[int, dict[str, object]]] = []
        self.created_ids = iter(range(10_000, 11_000))
        self.sync_state_builder = sync_vm.build_virtual_machine_sync_state_fields
        self.existing_vm = _existing_vm_snapshot(name="vm-105", vmid=105, record_id=55)
        self.existing_sidecar = {
            "id": 700,
            "virtual_machine": {"id": 55},
            "proxmox_endpoint_raw_id": 11,
            "proxmox_vm_id": 105,
            "proxmox_vm_type": "qemu",
            "proxmox_node_name": "pve-old-a",
            "proxmox_cluster_name": "cluster-a",
            "proxmox_vm_name": "vm-105",
        }

    def capture_prepared(self, kwargs: dict[str, object]) -> None:
        resource = kwargs["proxmox_resource"]
        self.prepared.append((str(resource["name"]), int(resource["vmid"]), str(resource["node"])))

    def capture_sync_state(self, **kwargs) -> dict[str, object]:
        state = self.sync_state_builder(**kwargs)
        self.sync_identities.append(
            (
                state.get("proxmox_endpoint_id"),
                int(state["proxmox_vm_id"]),
                str(state["proxmox_node"]),
            )
        )
        return state

    async def rest_create(self, _nb, _path, payload, *, lookup=None):
        assert lookup == {"id": 0}
        self.vm_create_payloads.append(dict(payload))
        return {"id": next(self.created_ids), **payload}

    async def rest_patch(self, _nb, path, record_id, payload):
        assert path == "/api/virtualization/virtual-machines/"
        self.vm_patch_calls.append((record_id, dict(payload)))
        return {**self.existing_vm, **payload, "id": record_id}

    async def sidecar_patch(self, _nb, path, record_id, payload):
        assert path == sync_state_reader.VM_SYNC_STATE_PATH
        self.sidecar_patch_calls.append((record_id, dict(payload)))
        return {**self.existing_sidecar, **payload, "id": record_id}


def _fake_vm_config_session(*, name: str, endpoint_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        cluster_name=name,
        domain=f"{name}.example.test",
        http_port=8006,
        db_endpoint_id=endpoint_id,
        session=_FakeSDKClient(_mixed_batch_upstream_payload),
    )


def _mixed_batch_upstream_payload(vmid: int) -> dict[str, object]:
    payload = {**PROXMOX_VM_CONFIG, "digest": "test", "memory": 4096, "agent": 1}
    if vmid == 999:
        payload["memory"] = True
    return payload


def _mixed_endpoint_resources() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    endpoint_a = [{**_resource(vmid), "node": "pve-new-a"} for vmid in range(1, 175)]
    endpoint_b = [{**_resource(vmid), "node": "pve-new-b"} for vmid in range(175, 348)]
    endpoint_b.extend(
        [
            {**_resource(105), "node": "pve-new-b"},
            {**_resource(999), "name": "vm-invalid", "node": "pve-new-b"},
        ]
    )
    return endpoint_a, endpoint_b


def _assert_mixed_batch_identity(
    result: list[dict[str, object]],
    harness: _MixedBatchHarness,
    bridge: _CapturingBridge,
) -> None:
    assert len(result) == 348
    assert len(harness.prepared) == 348
    assert ("vm-105", 105, "pve-new-a") in harness.prepared
    assert ("vm-105", 105, "pve-new-b") in harness.prepared
    assert (11, 105, "pve-new-a") in harness.sync_identities
    assert (22, 105, "pve-new-b") in harness.sync_identities
    assert len(set(harness.sync_identities)) == 348
    assert bridge.phase_summaries[-1]["created"] == 348
    assert bridge.phase_summaries[-1]["failed"] == 1


def _assert_mixed_batch_adoption(harness: _MixedBatchHarness) -> None:
    assert len(harness.vm_create_payloads) == 347
    assert len(harness.vm_patch_calls) == 1
    assert harness.vm_patch_calls[0][0] == 55
    assert harness.vm_patch_calls[0][1]["description"] == "Synced from Proxmox node pve-new-a"
    assert len(harness.sidecar_patch_calls) == 1
    assert harness.sidecar_patch_calls[0][0] == 700
    assert harness.sidecar_patch_calls[0][1]["proxmox_node_name"] == "pve-new-a"


def _install_full_update_stubs(
    monkeypatch,
    *,
    payload_side_effect=None,
    netbox_snapshot: list[dict[str, object]] | None = None,
    sidecar_rows: list[dict[str, object]] | None = None,
) -> list[int]:
    fetch_calls: list[int] = []

    async def _fake_detect_netbox_version(_nb):
        return (4, 5, 0)

    async def _fake_rest_list(_nb, path, *, query=None, **_kwargs):
        if path == "/api/virtualization/virtual-machines/" and netbox_snapshot is not None:
            limit = int((query or {}).get("limit", len(netbox_snapshot)) or len(netbox_snapshot))
            offset = int((query or {}).get("offset", 0) or 0)
            return [dict(record) for record in netbox_snapshot[offset : offset + limit]]
        if path == sync_state_reader.VM_SYNC_STATE_PATH:
            rows = [dict(row) for row in sidecar_rows or []]
            for field in ("proxmox_vm_id", "proxmox_endpoint_raw_id"):
                if field in (query or {}):
                    rows = [row for row in rows if row.get(field) == query[field]]
            return rows
        return []

    async def _fake_sidecar_paginated(_nb, path, *, base_query, page_size):
        assert path == sync_state_reader.VM_SYNC_STATE_PATH
        assert base_query == {}
        assert page_size == 500
        return [dict(row) for row in sidecar_rows or []]

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
            "name": str(resource.get("name") or f"vm-{vmid}"),
            "status": "active",
            "cluster": kwargs["cluster_id"],
            "device": kwargs["device_id"],
            "role": kwargs["role_id"],
            "vcpus": 1,
            "memory": 1024,
            "disk": 0,
            "tags": kwargs["tag_ids"],
            "description": f"Synced from Proxmox node {resource.get('node')}",
        }

    async def _fake_rest_create(_nb, _path, payload, *, lookup=None):
        assert lookup == {"id": 0}
        vmid = int(str(payload["name"]).rsplit("-", maxsplit=1)[-1])
        return {"id": vmid, **payload}

    async def _fake_sidecar_first(_nb, _path, *, query=None):
        parent_id = int((query or {}).get("virtual_machine_id", 0) or 0)
        return next(
            (
                dict(row)
                for row in sidecar_rows or []
                if int((row.get("virtual_machine") or {}).get("id", 0) or 0) == parent_id
            ),
            None,
        )

    async def _fake_vm_first(_nb, path, *, query=None):
        if path == sync_state_reader.VIRTUAL_MACHINES_PATH:
            requested_id = int((query or {}).get("id", 0) or 0)
            return next(
                (
                    dict(record)
                    for record in netbox_snapshot or []
                    if int(record.get("id", 0) or 0) == requested_id
                ),
                None,
            )
        return None

    async def _fake_sidecar_create(_nb, _path, payload, *, lookup=None):
        return {"id": 900, **payload}

    async def _fake_sidecar_patch(_nb, _path, record_id, payload):
        return {"id": record_id, **payload}

    async def _fake_stamp(*_args, **_kwargs):
        return None

    async def _fake_task_history(*_args, **_kwargs):
        return {"count": 0, "created": 0, "skipped": 0}

    monkeypatch.setattr(sync_vm, "detect_netbox_version", _fake_detect_netbox_version)
    monkeypatch.setattr(sync_vm, "rest_list_async", _fake_rest_list)
    monkeypatch.setattr(
        "proxbox_api.services.sync.sync_state_reader.rest_list_async",
        _fake_rest_list,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.sync_state_reader.rest_list_paginated_async",
        _fake_sidecar_paginated,
    )
    monkeypatch.setattr(sync_state_reader, "rest_first_async", _fake_vm_first)
    sync_state_reader.reset_sidecar_reader_availability_cache()
    monkeypatch.setattr(sync_state_writer, "rest_first_async", _fake_sidecar_first)
    monkeypatch.setattr(sync_state_writer, "rest_create_async", _fake_sidecar_create)
    monkeypatch.setattr(sync_state_writer, "rest_patch_async", _fake_sidecar_patch)
    sync_state_writer.reset_sidecar_availability_cache()
    monkeypatch.setattr(sync_vm, "rest_reconcile_async", _fake_reconcile)
    monkeypatch.setattr(sync_vm, "resolve_vm_sync_concurrency", lambda: 4)
    monkeypatch.setattr(sync_vm, "resolve_vm_config_fetch_timeout_seconds", lambda: 30)
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
    monkeypatch.setattr(
        sync_vm,
        "sync_all_virtual_machine_task_histories",
        _fake_task_history,
        raising=False,
    )

    return fetch_calls


def _run_full_update_name_case(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing_name: str,
    incoming_name: str,
    sidecar_rows: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    existing_vm = _existing_vm_snapshot(name=existing_name)
    patch_payloads: list[dict[str, object]] = []
    _install_full_update_stubs(
        monkeypatch,
        netbox_snapshot=[existing_vm],
        sidecar_rows=sidecar_rows,
    )

    async def _fake_get_vm_config(**_kwargs):
        return dict(PROXMOX_VM_CONFIG)

    async def _fake_patch(_nb, path, record_id, payload):
        assert path == "/api/virtualization/virtual-machines/"
        assert record_id == 55
        patch_payload = dict(payload)
        patch_payloads.append(patch_payload)
        return {"id": record_id, **existing_vm, **patch_payload}

    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)
    monkeypatch.setattr(sync_vm, "rest_patch_async", _fake_patch)

    result = asyncio.run(
        sync_vm.create_virtual_machines(
            netbox_session=object(),
            pxs=[],
            cluster_status=[SimpleNamespace(name="cluster-a", mode="cluster")],
            cluster_resources=[
                {"cluster-a": [{**_resource(101), "name": incoming_name}]},
            ],
            tag=SimpleNamespace(id=5, name="Proxbox", slug="proxbox", color="ff5722"),
            sync_vm_network=False,
        )
    )

    return result, patch_payloads


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
        endpoint_id_by_cluster={"cluster-a": 1},
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
    assert prepared.lookup == {"id": 0}
    assert prepared.sync_state_fields["proxmox_vm_id"] == 101
    assert prepared.sync_state_fields["proxmox_vm_type"] == "qemu"
    assert captured_payload_kwargs["proxmox_resource"] is resource
    assert captured_payload_kwargs["proxmox_config"] is vm_config
    assert captured_payload_kwargs["cluster_id"] == 11
    assert captured_payload_kwargs["device_id"] == 22
    assert captured_payload_kwargs["role_id"] == 33
    assert captured_payload_kwargs["site_id"] == 44
    assert captured_payload_kwargs["tenant_id"] == 55
    assert captured_payload_kwargs["tag_ids"] == [5, 7]
    assert prepared.sync_state_fields["proxmox_link"] == "https://pve.example:8006/#v1:0:=qemu/101"
    assert prepared.sync_state_fields["proxmox_endpoint_id"] == 1
    assert ensure_device_calls
    assert role_reconcile_calls
    assert context.node_device_cache[("cluster-a", "pve01")].id == 22
    assert context.vm_role_cache["qemu"].id == 33
    assert resolved_vm_types == ["qemu"]
    assert resolved_tag_inputs == [("cluster-a", vm_config)]


def test_full_update_batch_applies_proxmox_rename_when_sidecar_matches_stored_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, patch_payloads = _run_full_update_name_case(
        monkeypatch,
        existing_name="web-01",
        incoming_name="web-renamed",
        sidecar_rows=[
            {
                "id": 1,
                "virtual_machine": {"id": 55},
                "proxmox_vm_id": 101,
                "proxmox_vm_type": "qemu",
                "proxmox_vm_name": "web-01",
            },
        ],
    )

    assert len(result) == 1
    assert result[0]["name"] == "web-renamed"
    assert patch_payloads
    assert patch_payloads[-1]["name"] == "web-renamed"


def test_full_update_batch_preserves_operator_rename_when_sidecar_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, patch_payloads = _run_full_update_name_case(
        monkeypatch,
        existing_name="gateway-prod",
        incoming_name="web-renamed",
        sidecar_rows=[
            {
                "id": 1,
                "virtual_machine": {"id": 55},
                "proxmox_vm_id": 101,
                "proxmox_vm_type": "qemu",
                "proxmox_vm_name": "web-01",
            },
        ],
    )

    assert len(result) == 1
    assert result[0]["name"] == "gateway-prod"
    assert all("name" not in payload for payload in patch_payloads)


def test_full_update_batch_preserves_netbox_name_when_sidecar_name_is_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, patch_payloads = _run_full_update_name_case(
        monkeypatch,
        existing_name="web-01",
        incoming_name="web-renamed",
        sidecar_rows=[
            {
                "id": 1,
                "virtual_machine": {"id": 55},
                "proxmox_vm_id": 101,
                "proxmox_vm_type": "qemu",
                "proxmox_vm_name": "",
            },
        ],
    )

    assert len(result) == 1
    assert result[0]["name"] == "web-01"
    assert all("name" not in payload for payload in patch_payloads)


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
            tag=SimpleNamespace(id=5, name="Proxbox", slug="proxbox", color="ff5722"),
            websocket=bridge,
            sync_vm_network=False,
        )
    )

    assert [record["id"] for record in result] == [101]
    assert sorted(fetch_calls) == [101, 102]
    assert bridge.phase_summaries[-1]["created"] == 1
    assert bridge.phase_summaries[-1]["failed"] == 1


def test_mixed_endpoint_batch_preserves_identity_live_node_and_failure_isolation(monkeypatch):
    harness = _MixedBatchHarness()
    _install_full_update_stubs(
        monkeypatch,
        payload_side_effect=harness.capture_prepared,
        netbox_snapshot=[harness.existing_vm],
        sidecar_rows=[harness.existing_sidecar],
    )
    monkeypatch.setattr(sync_vm, "rest_create_async", harness.rest_create)
    monkeypatch.setattr(sync_vm, "rest_patch_async", harness.rest_patch)
    monkeypatch.setattr(sync_state_writer, "rest_patch_async", harness.sidecar_patch)
    monkeypatch.setattr(
        sync_vm,
        "build_virtual_machine_sync_state_fields",
        harness.capture_sync_state,
    )
    bridge = _CapturingBridge()
    endpoint_a = _fake_vm_config_session(name="cluster-a", endpoint_id=11)
    endpoint_b = _fake_vm_config_session(name="cluster-b", endpoint_id=22)
    endpoint_a_resources, endpoint_b_resources = _mixed_endpoint_resources()

    result = asyncio.run(
        sync_vm.create_virtual_machines(
            netbox_session=object(),
            pxs=[endpoint_a, endpoint_b],
            cluster_status=[
                SimpleNamespace(name="cluster-a", mode="cluster"),
                SimpleNamespace(name="cluster-b", mode="cluster"),
            ],
            cluster_resources=[
                {"cluster-a": endpoint_a_resources},
                {"cluster-b": endpoint_b_resources},
            ],
            tag=SimpleNamespace(id=5, name="Proxbox", slug="proxbox", color="ff5722"),
            websocket=bridge,
            sync_vm_network=False,
        )
    )

    _assert_mixed_batch_identity(result, harness, bridge)
    _assert_mixed_batch_adoption(harness)


def test_full_update_fetch_timeout_isolated_and_stage_completes(monkeypatch):
    fetch_calls = _install_full_update_stubs(monkeypatch)

    async def _fake_get_vm_config(**kwargs):
        vmid = int(kwargs["vmid"])
        fetch_calls.append(vmid)
        await asyncio.Event().wait()
        return dict(PROXMOX_VM_CONFIG)

    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)
    monkeypatch.setattr(sync_vm, "resolve_vm_config_fetch_timeout_seconds", lambda: 0.01)
    bridge = _CapturingBridge()

    result = asyncio.run(
        sync_vm.create_virtual_machines(
            netbox_session=object(),
            pxs=[],
            cluster_status=[SimpleNamespace(name="cluster-a", mode="cluster")],
            cluster_resources=[{"cluster-a": [_resource(102)]}],
            tag=SimpleNamespace(id=5, name="Proxbox", slug="proxbox", color="ff5722"),
            websocket=bridge,
            sync_vm_network=False,
        )
    )

    assert result == []
    assert fetch_calls == [102]
    assert bridge.phase_summaries[-1]["created"] == 0
    assert bridge.phase_summaries[-1]["failed"] == 1


def test_batch_vm_sync_runs_one_scoped_task_history_aggregate(monkeypatch):
    _install_full_update_stubs(monkeypatch)
    task_history_calls: list[dict[str, object]] = []

    async def _fake_get_vm_config(**_kwargs):
        return dict(PROXMOX_VM_CONFIG)

    async def _fake_task_history(**kwargs):
        task_history_calls.append(kwargs)
        return {"count": 2, "created": 0, "skipped": 0}

    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)
    monkeypatch.setattr(
        sync_vm,
        "sync_all_virtual_machine_task_histories",
        _fake_task_history,
        raising=False,
    )

    result = asyncio.run(
        sync_vm.create_virtual_machines(
            netbox_session=object(),
            pxs=[],
            cluster_status=[SimpleNamespace(name="cluster-a", mode="cluster")],
            cluster_resources=[{"cluster-a": [_resource(101), _resource(102)]}],
            tag=SimpleNamespace(id=5, name="Proxbox", slug="proxbox", color="ff5722"),
            sync_vm_network=False,
        )
    )

    assert [record["id"] for record in result] == [101, 102]
    assert len(task_history_calls) == 1
    assert task_history_calls[0]["netbox_vm_ids"] == [101, 102]


def test_selected_full_update_vm_batch_keeps_exact_owner_and_task_history_id(monkeypatch):
    processed_nodes: list[str] = []

    async def _inline_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    def _capture_owner(kwargs):
        processed_nodes.append(str(kwargs["proxmox_resource"]["node"]))

    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)
    _install_full_update_stubs(monkeypatch, payload_side_effect=_capture_owner)
    task_history_calls: list[dict[str, object]] = []

    async def _selected_vm_list(_nb, path, *, query=None):
        assert path == "/api/virtualization/virtual-machines/"
        assert query == {"id": ["501"]}
        return [
            {
                "id": 501,
                "name": "shared-name",
                "cluster": {"id": 41, "name": "cluster-a"},
            }
        ]

    async def _sidecar_scan(_nb):
        return SimpleNamespace(
            rows=(
                {
                    "virtual_machine": {"id": 501},
                    "proxmox_cluster_name": "cluster-a",
                    "proxmox_endpoint_raw_id": 11,
                    "proxmox_vm_id": 101,
                    "proxmox_vm_type": "qemu",
                },
            ),
            sidecar_unavailable=False,
            sidecar_read_failed=False,
        )

    async def _fake_get_vm_config(**_kwargs):
        return dict(PROXMOX_VM_CONFIG)

    async def _fake_rest_create(_nb, _path, payload, *, lookup=None):
        assert lookup == {"id": 0}
        return {"id": 501, **payload}

    async def _fake_task_history(**kwargs):
        task_history_calls.append(kwargs)
        return {"count": 1, "created": 0, "skipped": 0}

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _selected_vm_list)
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_filter.load_vm_sync_state_identities",
        _sidecar_scan,
    )
    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)
    monkeypatch.setattr(sync_vm, "rest_create_async", _fake_rest_create)
    monkeypatch.setattr(sync_vm, "sync_all_virtual_machine_task_histories", _fake_task_history)

    resource_a = {**_resource(101), "name": "shared-name", "node": "pve-a"}
    resource_b = {**_resource(101), "name": "shared-name", "node": "pve-b"}
    px_a = SimpleNamespace(
        name="cluster-a",
        cluster_name="cluster-a",
        db_endpoint_id=11,
    )
    px_b = SimpleNamespace(
        name="cluster-b",
        cluster_name="cluster-b",
        db_endpoint_id=22,
    )

    result = asyncio.run(
        sync_vm.create_virtual_machines(
            netbox_session=object(),
            pxs=[px_a, px_b],
            cluster_status=[
                SimpleNamespace(name="cluster-a", mode="cluster"),
                SimpleNamespace(name="cluster-b", mode="cluster"),
            ],
            cluster_resources=[{"cluster-a": [resource_a]}, {"cluster-b": [resource_b]}],
            tag=SimpleNamespace(id=5, name="Proxbox", slug="proxbox", color="ff5722"),
            netbox_vm_ids="501",
            sync_vm_network=False,
        )
    )

    assert [record["id"] for record in result] == [501]
    assert processed_nodes == ["pve-a"]
    assert len(task_history_calls) == 1
    assert task_history_calls[0]["netbox_vm_ids"] == [501]


def test_rest_vm_sync_without_network_raises_502_for_degraded_task_history(monkeypatch):
    _install_full_update_stubs(monkeypatch)

    async def _fake_get_vm_config(**_kwargs):
        return dict(PROXMOX_VM_CONFIG)

    async def _degraded_task_history(**_kwargs):
        return {"count": 1, "created": 3, "skipped": 2, "degraded": True, "errors": 1}

    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)
    monkeypatch.setattr(
        sync_vm,
        "sync_all_virtual_machine_task_histories",
        _degraded_task_history,
    )

    with pytest.raises(ProxboxException, match="degraded coverage") as exc_info:
        asyncio.run(
            sync_vm.create_virtual_machines(
                netbox_session=object(),
                pxs=[],
                cluster_status=[SimpleNamespace(name="cluster-a", mode="cluster")],
                cluster_resources=[{"cluster-a": [_resource(101)]}],
                tag=SimpleNamespace(id=5, name="Proxbox", slug="proxbox", color="ff5722"),
                sync_vm_network=False,
            )
        )

    assert exc_info.value.http_status_code == 502
    assert exc_info.value.detail == {"errors": 1, "reconciled": 3, "skipped": 2}


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
            tag=SimpleNamespace(id=5, name="Proxbox", slug="proxbox", color="ff5722"),
            sync_vm_network=False,
        )
    )

    assert [record["id"] for record in result] == [101, 102, 103]
    assert sorted(fetch_calls) == [101, 102, 103]
    first_process_index = next(
        index for index, event in enumerate(events) if event.startswith("process-")
    )
    fetch_end_indexes = [
        index for index, event in enumerate(events) if event.startswith("fetch-end-")
    ]
    assert len(fetch_end_indexes) == 3
    assert all(index < first_process_index for index in fetch_end_indexes)


def test_full_update_precomputes_both_clusters_when_two_clusters_present(monkeypatch):
    """Both clusters in a multi-cluster resource set must have their dependencies precomputed."""
    ensure_device_calls: list[str] = []

    _install_full_update_stubs(monkeypatch)

    async def _tracking_ensure_device(*args, **kwargs):
        node_name = kwargs.get("device_name", "unknown")
        ensure_device_calls.append(str(node_name))
        return SimpleNamespace(id=1)

    monkeypatch.setattr(sync_vm, "_ensure_device", _tracking_ensure_device)

    async def _fake_get_vm_config(**kwargs):
        return dict(PROXMOX_VM_CONFIG)

    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)

    result = asyncio.run(
        sync_vm.create_virtual_machines(
            netbox_session=object(),
            pxs=[],
            cluster_status=[
                SimpleNamespace(name="cluster-a", mode="cluster"),
                SimpleNamespace(name="cluster-b", mode="cluster"),
            ],
            cluster_resources=[
                {"cluster-a": [_resource(101)]},
                {"cluster-b": [_resource(201)]},
            ],
            tag=SimpleNamespace(id=5, name="Proxbox", slug="proxbox", color="ff5722"),
            sync_vm_network=False,
        )
    )

    assert sorted(r["id"] for r in result) == [101, 201]
    # _ensure_device called for both clusters' node "pve01" (from PROXMOX_VM_RESOURCE).
    assert len(ensure_device_calls) == 2


def test_full_update_uses_reconciled_cluster_site_scope(monkeypatch):
    """VM-stage node devices and VM payloads must use the cluster's actual site scope."""
    ensure_device_calls: list[dict[str, object]] = []
    payload_site_ids: list[int | None] = []

    def _record_payload_site(kwargs: dict[str, object]) -> None:
        site_id = kwargs.get("site_id")
        payload_site_ids.append(site_id if isinstance(site_id, int) else None)

    _install_full_update_stubs(monkeypatch, payload_side_effect=_record_payload_site)

    async def _fake_ensure_site(*_args, **_kwargs):
        return SimpleNamespace(id=44)

    async def _fake_ensure_cluster(*_args, **_kwargs):
        return SimpleNamespace(id=11, scope_type="dcim.site", scope_id=88)

    async def _tracking_ensure_device(*_args, **kwargs):
        ensure_device_calls.append(kwargs)
        return SimpleNamespace(id=22)

    async def _fake_get_vm_config(**_kwargs):
        return dict(PROXMOX_VM_CONFIG)

    monkeypatch.setattr(sync_vm, "_ensure_site", _fake_ensure_site)
    monkeypatch.setattr(sync_vm, "_ensure_cluster", _fake_ensure_cluster)
    monkeypatch.setattr(sync_vm, "_ensure_device", _tracking_ensure_device)
    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)

    result = asyncio.run(
        sync_vm.create_virtual_machines(
            netbox_session=object(),
            pxs=[],
            cluster_status=[SimpleNamespace(name="cluster-a", mode="cluster")],
            cluster_resources=[{"cluster-a": [_resource(101)]}],
            tag=SimpleNamespace(id=5, name="Proxbox", slug="proxbox", color="ff5722"),
            sync_vm_network=False,
        )
    )

    assert [record["id"] for record in result] == [101]
    assert ensure_device_calls[0]["site_id"] == 88
    assert payload_site_ids == [88]


def test_full_update_cluster_precompute_failure_propagates_as_proxbox_exception(monkeypatch):
    """A failure in one cluster's precompute phase must surface as a ProxboxException."""
    _install_full_update_stubs(monkeypatch)

    async def _failing_ensure_cluster_type(*args, **kwargs):
        raise RuntimeError("dependency resolution failed")

    monkeypatch.setattr(sync_vm, "_ensure_cluster_type", _failing_ensure_cluster_type)

    async def _fake_get_vm_config(**kwargs):
        return dict(PROXMOX_VM_CONFIG)

    monkeypatch.setattr(sync_vm, "get_vm_config", _fake_get_vm_config)

    with pytest.raises(ProxboxException, match="cluster, device, tag and role"):
        asyncio.run(
            sync_vm.create_virtual_machines(
                netbox_session=object(),
                pxs=[],
                cluster_status=[SimpleNamespace(name="cluster-a", mode="cluster")],
                cluster_resources=[{"cluster-a": [_resource(101)]}],
                tag=SimpleNamespace(id=5, name="Proxbox", slug="proxbox", color="ff5722"),
                sync_vm_network=False,
            )
        )
