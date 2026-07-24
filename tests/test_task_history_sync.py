"""Regression tests for VM task history synchronization."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import clear_rest_get_cache
from proxbox_api.services.proxmox_helpers import get_node_tasks, get_vm_tasks_individual
from proxbox_api.services.sync import sync_state_reader
from proxbox_api.services.sync import task_history as task_history_service
from proxbox_api.services.sync.sync_state_reader import VMSyncStateIdentityScan
from proxbox_api.services.sync.task_history import (
    sync_all_virtual_machine_task_histories,
    sync_virtual_machine_task_history,
)


@pytest.fixture(autouse=True)
def allow_legacy_vm_identity_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep existing CF-based cases explicit while sidecar-only cases opt out."""

    async def _unavailable_sidecar(_nb: object) -> VMSyncStateIdentityScan:
        return VMSyncStateIdentityScan(rows=(), sidecar_unavailable=True)

    monkeypatch.setattr(task_history_service, "custom_fields_enabled", lambda: True)
    monkeypatch.setattr(task_history_service, "warn_legacy_custom_fields", lambda *_args: None)
    monkeypatch.setattr(
        task_history_service,
        "load_vm_sync_state_identities",
        _unavailable_sidecar,
    )


def test_selected_vm_list_uses_repeated_chunked_id_filters_and_dedupes(monkeypatch):
    queries: list[dict[str, object]] = []

    async def _list(_nb, path, *, query=None):
        assert path == "/api/virtualization/virtual-machines/"
        captured = dict(query or {})
        queries.append(captured)
        repeated_ids = captured["id"]
        assert isinstance(repeated_ids, list)
        records = [{"id": int(vm_id)} for vm_id in reversed(repeated_ids)]
        return [*records, records[0]]

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _list)
    selected_ids = [*range(102, 0, -1), 2, 1, 0, -1]

    records = asyncio.run(
        task_history_service._list_all_vms_with_proxmox_id(
            object(),
            netbox_vm_ids=selected_ids,
        )
    )

    assert [len(query["id"]) for query in queries] == [100, 2]
    assert all(isinstance(vm_id, str) for query in queries for vm_id in query["id"])
    assert [vm_id for query in queries for vm_id in query["id"]] == [
        str(vm_id) for vm_id in range(1, 103)
    ]
    assert [record["id"] for record in records] == list(range(1, 103))


@pytest.mark.parametrize("fetch_max_concurrency", [0, -1])
def test_all_task_history_rejects_invalid_fetch_concurrency_before_io(
    monkeypatch,
    fetch_max_concurrency,
):
    async def _unexpected_list(*_args, **_kwargs):
        raise AssertionError("invalid concurrency must fail before NetBox I/O")

    monkeypatch.setattr(task_history_service, "_list_all_vms_with_proxmox_id", _unexpected_list)

    with pytest.raises(ProxboxException, match="fetch concurrency") as exc_info:
        asyncio.run(
            sync_all_virtual_machine_task_histories(
                netbox_session=object(),
                pxs=[],
                cluster_status=[],
                fetch_max_concurrency=fetch_max_concurrency,
            )
        )

    assert exc_info.value.http_status_code == 422


def test_sync_virtual_machine_task_history_builds_human_readable_payload(monkeypatch):
    bulk_calls: list[dict[str, object]] = []
    expected_pstart = 2222

    async def _fake_rest_bulk_reconcile(
        _nb,
        _path,
        *,
        payloads,
        lookup_fields,
        schema,
        current_normalizer,
        patchable_fields=None,
        base_query=None,
        fallback_to_individual=True,
    ):
        normalized_payloads = [
            schema.model_validate(payload).model_dump(
                mode="python", by_alias=True, exclude_none=True
            )
            for payload in payloads
        ]
        bulk_calls.append(
            {
                "lookup_fields": lookup_fields,
                "payloads": normalized_payloads,
                "base_query": base_query,
                "fallback_to_individual": fallback_to_individual,
            }
        )
        assert patchable_fields == {
            "virtual_machine",
            "vm_type",
            "end_time",
            "status",
            "task_state",
            "exitstatus",
            "tags",
            "custom_fields",
        }
        return SimpleNamespace(created=1, updated=0, unchanged=1, failed=0, records=[])

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.dump_models",
        lambda items: items,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_tasks",
        lambda session, node, **kwargs: [
            {
                "upid": f"UPID:{node}:1",
                "node": node,
                "pid": 1111,
                "pstart": 2222,
                "id": "144",
                "type": "vzstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
        ],
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_task_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("archived tasks must not trigger per-UPID status requests")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.rest_bulk_reconcile_async",
        _fake_rest_bulk_reconcile,
    )

    result = asyncio.run(
        sync_virtual_machine_task_history(
            netbox_session=object(),
            pxs=[SimpleNamespace(name="lab", session=object())],
            cluster_status=[
                SimpleNamespace(
                    name="lab",
                    node_list=[
                        SimpleNamespace(name="pve01"),
                        SimpleNamespace(name="pve02"),
                    ],
                )
            ],
            virtual_machine_id=144,
            proxmox_vmid=144,
            vm_type="lxc",
            cluster_name="lab",
            tag_refs=[{"name": "Proxbox", "slug": "proxbox"}],
        )
    )

    assert result == 2
    assert len(bulk_calls) == 1
    assert bulk_calls[0]["lookup_fields"] == ["upid"]
    assert bulk_calls[0]["base_query"] is None
    assert bulk_calls[0]["fallback_to_individual"] is False
    payloads = bulk_calls[0]["payloads"]
    assert isinstance(payloads, list)
    first_payload = payloads[0]
    assert first_payload["upid"] == "UPID:pve01:1"
    assert first_payload["description"] == "CT 144 - Start"
    assert first_payload["status"] == "OK"
    assert first_payload["task_state"] == "stopped"
    assert first_payload["exitstatus"] == "OK"
    assert first_payload["end_time"].startswith("2024-03")
    assert first_payload["vm_type"] == "lxc"
    assert first_payload["start_time"].startswith("2024-03")
    assert first_payload["pstart"] == expected_pstart


def test_sync_virtual_machine_task_history_does_not_fan_out_on_bulk_failure(monkeypatch):
    per_item_calls: list[tuple[dict, dict]] = []

    async def _failing_bulk_reconcile(*_args, **_kwargs):
        raise RuntimeError("bulk unavailable")

    async def _fake_rest_reconcile(
        _nb,
        _path,
        lookup,
        payload,
        schema,
        current_normalizer,
        patchable_fields=None,
    ):
        desired_model = schema.model_validate(payload)
        per_item_calls.append(
            (
                lookup,
                desired_model.model_dump(mode="python", by_alias=True, exclude_none=True),
            )
        )
        assert patchable_fields == {
            "virtual_machine",
            "vm_type",
            "end_time",
            "status",
            "task_state",
            "exitstatus",
            "tags",
            "custom_fields",
        }
        return SimpleNamespace(id=1)

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.dump_models",
        lambda items: items,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_tasks",
        lambda session, node, **kwargs: [
            {
                "upid": f"UPID:{node}:1",
                "node": node,
                "pid": 1111,
                "pstart": 2222,
                "id": "144",
                "type": "vzstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
        ],
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.rest_bulk_reconcile_async",
        _failing_bulk_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.netbox_rest.rest_reconcile_async",
        _fake_rest_reconcile,
    )

    result = asyncio.run(
        sync_virtual_machine_task_history(
            netbox_session=object(),
            pxs=[SimpleNamespace(name="lab", session=object())],
            cluster_status=[
                SimpleNamespace(
                    name="lab",
                    node_list=[
                        SimpleNamespace(name="pve01"),
                        SimpleNamespace(name="pve02"),
                    ],
                )
            ],
            virtual_machine_id=144,
            proxmox_vmid=144,
            vm_type="lxc",
            cluster_name="lab",
            tag_refs=[{"name": "Proxbox", "slug": "proxbox"}],
        )
    )

    assert result == 0
    assert per_item_calls == []


def test_targeted_task_history_fails_closed_for_duplicate_cluster_name(monkeypatch):
    async def _unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("ambiguous targeted lookup must not fetch an arbitrary estate")

    monkeypatch.setattr("proxbox_api.services.sync.task_history.get_node_tasks", _unexpected_fetch)

    result = asyncio.run(
        sync_virtual_machine_task_history(
            netbox_session=object(),
            pxs=[SimpleNamespace(db_endpoint_id=11), SimpleNamespace(db_endpoint_id=22)],
            cluster_status=[
                SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")]),
                SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-b")]),
            ],
            virtual_machine_id=501,
            proxmox_vmid=101,
            vm_type="qemu",
            cluster_name="lab",
        )
    )

    assert result == 0


def test_task_history_reassigns_existing_upid_from_wrong_vm_and_type(monkeypatch):
    """A previously mis-associated UPID must self-heal in the bulk PATCH."""

    class _Response:
        status = 200
        text = "ok"

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    existing = {
        "id": 900,
        "virtual_machine": {"id": 999},
        "vm_type": "lxc",
        "upid": "UPID:pve-a:101",
        "node": "pve-a",
        "task_id": "101",
        "task_type": "qmstart",
        "username": "root@pam",
        "start_time": "2024-03-09T16:00:00+00:00",
        "end_time": "2024-03-09T16:05:00+00:00",
        "description": "QEMU 101 - Start",
        "status": "OK",
        "task_state": "stopped",
        "exitstatus": "OK",
        "tags": [],
        "custom_fields": {},
    }

    class _Client:
        def __init__(self):
            self.patches: list[object] = []
            self.get_queries: list[dict[str, object] | None] = []

        async def request(self, method, _path, *, query=None, payload=None, **_kwargs):
            if method == "GET":
                self.get_queries.append(query)
                if query and query.get("virtual_machine") is not None:
                    return _Response({"count": 0, "results": [], "next": None})
                return _Response({"count": 1, "results": [existing], "next": None})
            if method == "PATCH":
                self.patches.append(payload)
                return _Response(payload)
            raise AssertionError(f"unexpected NetBox method {method}")

    client = _Client()
    nb = SimpleNamespace(client=client)
    clear_rest_get_cache()
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_tasks",
        lambda *_args, **_kwargs: [
            {
                "upid": "UPID:pve-a:101",
                "node": "pve-a",
                "id": "101",
                "type": "qmstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
        ],
    )
    monkeypatch.setattr("proxbox_api.services.sync.task_history.dump_models", lambda items: items)

    result = asyncio.run(
        sync_virtual_machine_task_history(
            netbox_session=nb,
            pxs=[SimpleNamespace(db_endpoint_id=11)],
            cluster_status=[SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")])],
            virtual_machine_id=501,
            proxmox_vmid=101,
            vm_type="qemu",
            cluster_name="lab",
            proxmox_endpoint_id=11,
        )
    )

    assert result == 1
    assert client.get_queries == [{"limit": 200}]
    assert client.patches == [[{"id": 900, "virtual_machine": 501, "vm_type": "qemu"}]]


def test_all_task_history_walks_each_endpoint_node_once_and_reconciles_globally(monkeypatch):
    """Same-named clusters on different endpoints must never cross-map equal VMIDs."""

    px_a = SimpleNamespace(db_endpoint_id=11)
    px_b = SimpleNamespace(db_endpoint_id=22)
    cluster_a = SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")])
    cluster_b = SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-b")])
    vms = [
        {
            "id": 501,
            "name": "vm-a",
            "cluster": {"name": "lab"},
            "custom_fields": {
                "proxmox_endpoint_id": 11,
                "proxmox_vm_id": 101,
                "proxmox_vm_type": "qemu",
            },
        },
        {
            "id": 502,
            "name": "vm-b",
            "cluster": {"name": "lab"},
            "custom_fields": {
                "proxmox_endpoint_id": 22,
                "proxmox_vm_id": 101,
                "proxmox_vm_type": "lxc",
            },
        },
    ]
    fetch_calls: list[dict[str, object]] = []
    bulk_calls: list[dict[str, object]] = []

    async def _fake_list_vms(_nb, batch_size=500, *, netbox_vm_ids=None):
        assert batch_size == 500
        assert netbox_vm_ids is None
        return vms

    async def _fake_get_node_tasks(session, node, **kwargs):
        fetch_calls.append({"endpoint": session.db_endpoint_id, "node": node, **kwargs})
        endpoint = session.db_endpoint_id
        return [
            {
                "upid": f"UPID:{node}:{endpoint}",
                "node": node,
                "id": "101",
                "type": "qmstart" if endpoint == 11 else "vzstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
        ]

    async def _fake_bulk(_nb, _path, **kwargs):
        bulk_calls.append(kwargs)
        return SimpleNamespace(created=2, updated=0, unchanged=0, failed=0, records=[])

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history._list_all_vms_with_proxmox_id",
        _fake_list_vms,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_tasks",
        _fake_get_node_tasks,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.dump_models",
        lambda items: items,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_task_status",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("archived tasks already contain their final outcome")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.rest_bulk_reconcile_async",
        _fake_bulk,
    )

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(),
            pxs=[px_a, px_b],
            cluster_status=[cluster_a, cluster_b],
            tag_refs=[{"name": "Proxbox", "slug": "proxbox"}],
            fetch_max_concurrency=2,
        )
    )

    assert result == {"count": 2, "created": 2, "skipped": 0}
    assert len(fetch_calls) == 2
    assert {call["endpoint"] for call in fetch_calls} == {11, 22}
    assert all(call["vmid"] is None for call in fetch_calls)
    assert all(call["source"] == "archive" for call in fetch_calls)
    assert all(call["start"] == 0 and call["limit"] == 500 for call in fetch_calls)
    assert len({call["until"] for call in fetch_calls}) == 1
    assert len(bulk_calls) == 1
    assert bulk_calls[0]["base_query"] is None
    assert bulk_calls[0]["fallback_to_individual"] is False
    assert {"virtual_machine", "vm_type"} <= set(bulk_calls[0]["patchable_fields"])
    payloads = bulk_calls[0]["payloads"]
    assert {(payload["virtual_machine"], payload["vm_type"]) for payload in payloads} == {
        (501, "qemu"),
        (502, "lxc"),
    }


def test_all_task_history_uses_one_sidecar_scan_as_authoritative_identity(monkeypatch):
    """Default sidecar-only deployments must not depend on VM custom fields."""

    vms = [
        {"id": 501, "name": "vm-a", "cluster": {"name": "stale-a"}},
        {
            "id": 502,
            "name": "vm-b",
            "cluster": {"name": "stale-b"},
            "custom_fields": {
                "proxmox_endpoint_id": 999,
                "proxmox_vm_id": 999,
                "proxmox_vm_type": "qemu",
            },
        },
    ]
    sidecars = (
        {
            "id": 1,
            "virtual_machine": {"id": 501},
            "proxmox_vm_id": 101,
            "proxmox_endpoint_raw_id": 11,
            "proxmox_vm_type": "qemu",
            "proxmox_cluster_name": "lab-a",
        },
        {
            "id": 2,
            "virtual_machine": {"id": 502},
            "proxmox_vm_id": 102,
            "proxmox_endpoint_raw_id": 22,
            "proxmox_vm_type": "lxc",
            "proxmox_cluster_name": "lab-b",
        },
    )
    sidecar_scans = 0
    fetched_endpoints: list[int] = []
    payloads: list[dict[str, object]] = []

    async def _list_sidecars(_nb: object, path: str, *, base_query, page_size):
        nonlocal sidecar_scans
        sidecar_scans += 1
        assert path == "/api/plugins/proxbox/sync-state/virtual-machines/"
        assert base_query == {}
        assert page_size == 200
        return list(sidecars)

    async def _list_vms(*_args, **_kwargs):
        return vms

    async def _fetch(session, node, **_kwargs):
        fetched_endpoints.append(session.db_endpoint_id)
        vmid = 101 if session.db_endpoint_id == 11 else 102
        return [
            {
                "upid": f"UPID:{node}:{vmid}",
                "node": node,
                "id": str(vmid),
                "type": "qmstart" if vmid == 101 else "vzstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
        ]

    async def _bulk(_nb, _path, **kwargs):
        payloads.extend(kwargs["payloads"])
        return SimpleNamespace(
            created=len(kwargs["payloads"]),
            updated=0,
            unchanged=0,
            failed=0,
            records=[],
        )

    monkeypatch.setattr(task_history_service, "custom_fields_enabled", lambda: False)
    monkeypatch.setattr(sync_state_reader, "rest_list_paginated_async", _list_sidecars)
    monkeypatch.setattr(
        task_history_service,
        "load_vm_sync_state_identities",
        sync_state_reader.load_vm_sync_state_identities,
    )
    monkeypatch.setattr(task_history_service, "_list_all_vms_with_proxmox_id", _list_vms)
    monkeypatch.setattr(task_history_service, "get_node_tasks", _fetch)
    monkeypatch.setattr(task_history_service, "dump_models", lambda items: items)
    monkeypatch.setattr(task_history_service, "rest_bulk_reconcile_async", _bulk)

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(),
            pxs=[SimpleNamespace(db_endpoint_id=11), SimpleNamespace(db_endpoint_id=22)],
            cluster_status=[
                SimpleNamespace(name="lab-a", node_list=[SimpleNamespace(name="pve-a")]),
                SimpleNamespace(name="lab-b", node_list=[SimpleNamespace(name="pve-b")]),
            ],
        )
    )

    assert result == {"count": 2, "created": 2, "skipped": 0}
    assert sidecar_scans == 1
    assert fetched_endpoints == [11, 22]
    assert {(row["virtual_machine"], row["vm_type"]) for row in payloads} == {
        (501, "qemu"),
        (502, "lxc"),
    }


def test_selected_task_history_filters_vm_list_and_scans_sidecars_once(monkeypatch):
    vm_queries: list[dict[str, object]] = []
    sidecar_scans = 0
    fetched_endpoints: list[int] = []

    async def _list(_nb, path, *, query=None):
        assert path == "/api/virtualization/virtual-machines/"
        vm_queries.append(dict(query or {}))
        return [{"id": 502, "name": "vm-b", "cluster": {"name": "ignored"}}]

    async def _scan(_nb: object) -> VMSyncStateIdentityScan:
        nonlocal sidecar_scans
        sidecar_scans += 1
        return VMSyncStateIdentityScan(
            rows=(
                {
                    "virtual_machine": {"id": 501},
                    "proxmox_vm_id": 101,
                    "proxmox_endpoint_raw_id": 11,
                    "proxmox_vm_type": "qemu",
                    "proxmox_cluster_name": "lab-a",
                },
                {
                    "virtual_machine": {"id": 502},
                    "proxmox_vm_id": 102,
                    "proxmox_endpoint_raw_id": 22,
                    "proxmox_vm_type": "lxc",
                    "proxmox_cluster_name": "lab-b",
                },
            )
        )

    async def _fetch(session, node, **_kwargs):
        fetched_endpoints.append(session.db_endpoint_id)
        return [
            {
                "upid": f"UPID:{node}:102",
                "node": node,
                "id": "102",
                "type": "vzstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
        ]

    async def _bulk(_nb, _path, **_kwargs):
        return SimpleNamespace(created=1, updated=0, unchanged=0, failed=0, records=[])

    monkeypatch.setattr(task_history_service, "custom_fields_enabled", lambda: False)
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _list)
    monkeypatch.setattr(task_history_service, "load_vm_sync_state_identities", _scan)
    monkeypatch.setattr(task_history_service, "get_node_tasks", _fetch)
    monkeypatch.setattr(task_history_service, "dump_models", lambda items: items)
    monkeypatch.setattr(task_history_service, "rest_bulk_reconcile_async", _bulk)

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(),
            pxs=[SimpleNamespace(db_endpoint_id=11), SimpleNamespace(db_endpoint_id=22)],
            cluster_status=[
                SimpleNamespace(name="lab-a", node_list=[SimpleNamespace(name="pve-a")]),
                SimpleNamespace(name="lab-b", node_list=[SimpleNamespace(name="pve-b")]),
            ],
            netbox_vm_ids=[502],
        )
    )

    assert result["created"] == 1
    assert vm_queries == [{"id": ["502"]}]
    assert sidecar_scans == 1
    assert fetched_endpoints == [22]


@pytest.mark.parametrize(
    ("scan", "detail_fragment"),
    [
        (VMSyncStateIdentityScan(rows=(), sidecar_unavailable=True), "is unavailable"),
        (VMSyncStateIdentityScan(rows=(), sidecar_read_failed=True), "temporarily failed"),
    ],
)
def test_task_history_fails_when_sidecar_identity_is_unverifiable_and_cf_disabled(
    monkeypatch,
    scan,
    detail_fragment,
):
    async def _list_vms(*_args, **_kwargs):
        return [{"id": 501, "name": "vm-a", "cluster": {"name": "lab"}}]

    async def _scan(_nb: object) -> VMSyncStateIdentityScan:
        return scan

    monkeypatch.setattr(task_history_service, "custom_fields_enabled", lambda: False)
    monkeypatch.setattr(task_history_service, "_list_all_vms_with_proxmox_id", _list_vms)
    monkeypatch.setattr(task_history_service, "load_vm_sync_state_identities", _scan)

    with pytest.raises(ProxboxException, match="Unable to verify VM identity") as exc_info:
        asyncio.run(
            sync_all_virtual_machine_task_histories(
                netbox_session=object(),
                pxs=[],
                cluster_status=[],
            )
        )

    assert detail_fragment in str(exc_info.value.detail)


def test_unscoped_task_history_ignores_unmanaged_vm_after_successful_sidecar_scan(
    monkeypatch,
):
    vms = [
        {"id": 501, "name": "owned"},
        {"id": 999, "name": "manually-managed"},
    ]

    async def _list_vms(*_args, **_kwargs):
        return vms

    async def _scan(_nb: object) -> VMSyncStateIdentityScan:
        return VMSyncStateIdentityScan(
            rows=(
                {
                    "virtual_machine": {"id": 501},
                    "proxmox_vm_id": 101,
                    "proxmox_endpoint_raw_id": 11,
                    "proxmox_vm_type": "qemu",
                    "proxmox_cluster_name": "lab",
                },
            )
        )

    async def _fetch(_session, node, **_kwargs):
        return [
            {
                "upid": f"UPID:{node}:101",
                "id": "101",
                "type": "qmstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
        ]

    async def _bulk(_nb, _path, **kwargs):
        assert [payload["virtual_machine"] for payload in kwargs["payloads"]] == [501]
        return SimpleNamespace(created=1, updated=0, unchanged=0, failed=0, records=[])

    monkeypatch.setattr(task_history_service, "custom_fields_enabled", lambda: False)
    monkeypatch.setattr(task_history_service, "_list_all_vms_with_proxmox_id", _list_vms)
    monkeypatch.setattr(task_history_service, "load_vm_sync_state_identities", _scan)
    monkeypatch.setattr(task_history_service, "get_node_tasks", _fetch)
    monkeypatch.setattr(task_history_service, "dump_models", lambda items: items)
    monkeypatch.setattr(task_history_service, "rest_bulk_reconcile_async", _bulk)

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(),
            pxs=[SimpleNamespace(db_endpoint_id=11)],
            cluster_status=[SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")])],
        )
    )

    assert result == {"count": 1, "created": 1, "skipped": 1}


@pytest.mark.parametrize("legacy_fallback_enabled", [False, True])
def test_selected_task_history_missing_identity_is_fatal(
    monkeypatch,
    legacy_fallback_enabled,
):
    async def _list_vms(*_args, **_kwargs):
        return [{"id": 501, "name": "selected-unmanaged"}]

    async def _scan(_nb: object) -> VMSyncStateIdentityScan:
        return VMSyncStateIdentityScan(rows=())

    monkeypatch.setattr(
        task_history_service,
        "custom_fields_enabled",
        lambda: legacy_fallback_enabled,
    )
    monkeypatch.setattr(task_history_service, "_list_all_vms_with_proxmox_id", _list_vms)
    monkeypatch.setattr(task_history_service, "load_vm_sync_state_identities", _scan)

    with pytest.raises(ProxboxException, match="explicitly selected") as exc_info:
        asyncio.run(
            sync_all_virtual_machine_task_histories(
                netbox_session=object(),
                pxs=[],
                cluster_status=[],
                netbox_vm_ids=[501],
            )
        )

    assert exc_info.value.http_status_code == 502


@pytest.mark.parametrize(
    "sidecar_rows",
    [
        (
            {
                "virtual_machine": {"id": 501},
                "proxmox_vm_id": 101,
                "proxmox_vm_type": "qemu",
                "proxmox_cluster_name": "lab",
            },
        ),
        (
            {
                "virtual_machine": {"id": 501},
                "proxmox_vm_id": 101,
                "proxmox_endpoint_raw_id": 11,
                "proxmox_vm_type": "qemu",
                "proxmox_cluster_name": "lab",
            },
            {
                "virtual_machine": {"id": 501},
                "proxmox_vm_id": 101,
                "proxmox_endpoint_raw_id": 22,
                "proxmox_vm_type": "qemu",
                "proxmox_cluster_name": "lab",
            },
        ),
    ],
)
def test_present_invalid_sidecar_never_falls_back_to_enabled_custom_fields(
    monkeypatch,
    sidecar_rows,
):
    vm_with_valid_legacy_identity = {
        "id": 501,
        "name": "vm-a",
        "cluster": {"name": "lab"},
        "custom_fields": {
            "proxmox_endpoint_id": 11,
            "proxmox_vm_id": 101,
            "proxmox_vm_type": "qemu",
        },
    }

    async def _list_vms(*_args, **_kwargs):
        return [vm_with_valid_legacy_identity]

    async def _scan(_nb: object) -> VMSyncStateIdentityScan:
        return VMSyncStateIdentityScan(rows=sidecar_rows)

    async def _unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("invalid present sidecar must fail before archive fetch")

    monkeypatch.setattr(task_history_service, "custom_fields_enabled", lambda: True)
    monkeypatch.setattr(task_history_service, "_list_all_vms_with_proxmox_id", _list_vms)
    monkeypatch.setattr(task_history_service, "load_vm_sync_state_identities", _scan)
    monkeypatch.setattr(task_history_service, "get_node_tasks", _unexpected_fetch)

    with pytest.raises(ProxboxException, match="Unable to verify VM identity") as exc_info:
        asyncio.run(
            sync_all_virtual_machine_task_histories(
                netbox_session=object(),
                pxs=[SimpleNamespace(db_endpoint_id=11)],
                cluster_status=[
                    SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")])
                ],
            )
        )

    assert "Refusing legacy custom-field fallback" in str(exc_info.value.detail)


def test_selected_task_history_ignores_unrelated_malformed_sidecar(monkeypatch):
    selected_vm = {"id": 501, "name": "selected"}

    async def _list_vms(*_args, **_kwargs):
        return [selected_vm]

    async def _scan(_nb: object) -> VMSyncStateIdentityScan:
        return VMSyncStateIdentityScan(
            rows=(
                {
                    "virtual_machine": {"id": 501},
                    "proxmox_vm_id": 101,
                    "proxmox_endpoint_raw_id": 11,
                    "proxmox_vm_type": "qemu",
                    "proxmox_cluster_name": "lab",
                },
                {
                    "virtual_machine": {"id": 999},
                    "proxmox_vm_id": "broken",
                },
            )
        )

    async def _fetch(_session, node, **_kwargs):
        return [
            {
                "upid": f"UPID:{node}:101",
                "id": "101",
                "type": "qmstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
        ]

    async def _bulk(*_args, **_kwargs):
        return SimpleNamespace(created=1, updated=0, unchanged=0, failed=0, records=[])

    monkeypatch.setattr(task_history_service, "custom_fields_enabled", lambda: False)
    monkeypatch.setattr(task_history_service, "_list_all_vms_with_proxmox_id", _list_vms)
    monkeypatch.setattr(task_history_service, "load_vm_sync_state_identities", _scan)
    monkeypatch.setattr(task_history_service, "get_node_tasks", _fetch)
    monkeypatch.setattr(task_history_service, "dump_models", lambda items: items)
    monkeypatch.setattr(task_history_service, "rest_bulk_reconcile_async", _bulk)

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(),
            pxs=[SimpleNamespace(db_endpoint_id=11)],
            cluster_status=[SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")])],
            netbox_vm_ids=[501],
        )
    )

    assert result == {"count": 1, "created": 1, "skipped": 0}


def test_all_task_history_stops_when_node_ignores_pagination_offset(monkeypatch):
    """A repeated full page must terminate instead of looping forever."""

    px = SimpleNamespace(db_endpoint_id=11)
    cluster = SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")])
    fetch_offsets: list[int] = []
    bulk_payloads: list[dict[str, object]] = []
    phase_summaries: list[dict[str, object]] = []

    class _Bridge:
        async def emit_discovery(self, **_kwargs):
            return None

        async def emit_phase_summary(self, **kwargs):
            phase_summaries.append(kwargs)

    vms = [
        {
            "id": 501,
            "cluster": {"name": "lab"},
            "custom_fields": {
                "proxmox_endpoint_id": 11,
                "proxmox_vm_id": 101,
                "proxmox_vm_type": "qemu",
            },
        },
        {
            "id": 502,
            "cluster": {"name": "lab"},
            "custom_fields": {
                "proxmox_endpoint_id": 11,
                "proxmox_vm_id": 102,
                "proxmox_vm_type": "qemu",
            },
        },
    ]

    async def _fake_list_vms(*_args, **_kwargs):
        return vms

    async def _fake_get_node_tasks(_session, node, **kwargs):
        fetch_offsets.append(kwargs["start"])
        return [
            {
                "upid": f"UPID:{node}:101",
                "node": node,
                "id": "101",
                "type": "qmstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            },
            {
                "upid": f"UPID:{node}:102",
                "node": node,
                "id": "102",
                "type": "qmstart",
                "user": "root@pam",
                "starttime": 1710000001,
                "endtime": 1710000301,
                "status": "OK",
            },
        ]

    async def _fake_bulk(_nb, _path, **kwargs):
        bulk_payloads.extend(kwargs["payloads"])
        return SimpleNamespace(created=2, updated=0, unchanged=0, failed=0, records=[])

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history._TASK_ARCHIVE_PAGE_SIZE", 2, raising=False
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history._list_all_vms_with_proxmox_id",
        _fake_list_vms,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_tasks", _fake_get_node_tasks
    )
    monkeypatch.setattr("proxbox_api.services.sync.task_history.dump_models", lambda items: items)
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.rest_bulk_reconcile_async", _fake_bulk
    )

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(),
            pxs=[px],
            cluster_status=[cluster],
            websocket=_Bridge(),
        )
    )

    assert result["created"] == 2
    assert result["degraded"] is True
    assert result["errors"] == 1
    assert fetch_offsets == [0, 2]
    assert len(bulk_payloads) == 2
    assert phase_summaries[-1]["failed"] == 1
    assert "degraded coverage" in str(phase_summaries[-1]["message"])


def test_all_task_history_reports_no_new_upid_page_as_degraded(monkeypatch):
    """A reordered duplicate page is no progress even when its page signature changes."""

    px = SimpleNamespace(db_endpoint_id=11)
    cluster = SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")])
    offsets: list[int] = []
    phase_summaries: list[dict[str, object]] = []
    vms = [
        {
            "id": 500 + vmid,
            "cluster": {"name": "lab"},
            "custom_fields": {
                "proxmox_endpoint_id": 11,
                "proxmox_vm_id": vmid,
                "proxmox_vm_type": "qemu",
            },
        }
        for vmid in (101, 102)
    ]

    class _Bridge:
        async def emit_discovery(self, **_kwargs):
            return None

        async def emit_phase_summary(self, **kwargs):
            phase_summaries.append(kwargs)

    def _task(vmid: int) -> dict[str, object]:
        return {
            "upid": f"UPID:pve-a:{vmid}",
            "node": "pve-a",
            "id": str(vmid),
            "type": "qmstart",
            "user": "root@pam",
            "starttime": 1710000000,
            "endtime": 1710000300,
            "status": "OK",
        }

    async def _fake_get_node_tasks(*_args, **kwargs):
        offsets.append(kwargs["start"])
        page = [_task(101), _task(102)]
        return page if kwargs["start"] == 0 else list(reversed(page))

    async def _fake_bulk(_nb, _path, **kwargs):
        return SimpleNamespace(
            created=len(kwargs["payloads"]),
            updated=0,
            unchanged=0,
            failed=0,
            records=[],
        )

    monkeypatch.setattr(task_history_service, "_TASK_ARCHIVE_PAGE_SIZE", 2)
    monkeypatch.setattr(
        task_history_service,
        "_list_all_vms_with_proxmox_id",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=vms),
    )
    monkeypatch.setattr(task_history_service, "get_node_tasks", _fake_get_node_tasks)
    monkeypatch.setattr(task_history_service, "dump_models", lambda items: items)
    monkeypatch.setattr(task_history_service, "rest_bulk_reconcile_async", _fake_bulk)

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(),
            pxs=[px],
            cluster_status=[cluster],
            websocket=_Bridge(),
        )
    )

    assert offsets == [0, 2]
    assert result["created"] == 2
    assert result["degraded"] is True
    assert result["errors"] == 1
    assert phase_summaries[-1]["failed"] == 1


def test_all_task_history_reads_more_than_one_archive_page_without_losing_new_rows(
    monkeypatch,
):
    px = SimpleNamespace(db_endpoint_id=11)
    cluster = SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")])
    vms = [
        {
            "id": 1000 + vmid,
            "cluster": {"name": "lab"},
            "custom_fields": {
                "proxmox_endpoint_id": 11,
                "proxmox_vm_id": vmid,
                "proxmox_vm_type": "qemu",
            },
        }
        for vmid in range(1, 502)
    ]
    offsets: list[int] = []
    payload_count = 0

    async def _fake_list_vms(*_args, **_kwargs):
        return vms

    async def _fake_get_node_tasks(_session, node, **kwargs):
        offsets.append(kwargs["start"])
        start = kwargs["start"]
        vmids = range(1, 501) if start == 0 else range(501, 502)
        return [
            {
                "upid": f"UPID:{node}:{vmid}",
                "node": node,
                "id": str(vmid),
                "type": "qmstart",
                "user": "root@pam",
                "starttime": 1710000000 + vmid,
                "endtime": 1710000300 + vmid,
                "status": "OK",
            }
            for vmid in vmids
        ]

    async def _fake_bulk(_nb, _path, **kwargs):
        nonlocal payload_count
        payload_count = len(kwargs["payloads"])
        return SimpleNamespace(
            created=payload_count,
            updated=0,
            unchanged=0,
            failed=0,
            records=[],
        )

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history._list_all_vms_with_proxmox_id",
        _fake_list_vms,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_tasks", _fake_get_node_tasks
    )
    monkeypatch.setattr("proxbox_api.services.sync.task_history.dump_models", lambda items: items)
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.rest_bulk_reconcile_async", _fake_bulk
    )

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(),
            pxs=[px],
            cluster_status=[cluster],
        )
    )

    assert offsets == [0, 500]
    assert payload_count == 501
    assert result["created"] == 501


def test_all_task_history_isolates_partial_node_failure_dedupes_and_bounds_concurrency(
    monkeypatch,
):
    px = SimpleNamespace(db_endpoint_id=11)
    cluster = SimpleNamespace(
        name="lab",
        node_list=[
            SimpleNamespace(name="partial"),
            SimpleNamespace(name="healthy"),
            SimpleNamespace(name="migrated"),
            SimpleNamespace(name="idle"),
        ],
    )
    vms = [
        {
            "id": 500 + vmid,
            "cluster": {"name": "lab"},
            "custom_fields": {
                "proxmox_endpoint_id": 11,
                "proxmox_vm_id": vmid,
                "proxmox_vm_type": "qemu",
            },
        }
        for vmid in range(101, 105)
    ]
    active = 0
    max_active = 0
    payloads: list[dict[str, object]] = []
    phase_summaries: list[dict[str, object]] = []

    class _Bridge:
        async def emit_discovery(self, **_kwargs):
            return None

        async def emit_phase_summary(self, **kwargs):
            phase_summaries.append(kwargs)

    def _task(node: str, vmid: int, upid: str | None = None) -> dict[str, object]:
        return {
            "upid": upid or f"UPID:{node}:{vmid}",
            "node": node,
            "id": str(vmid),
            "type": "qmstart",
            "user": "root@pam",
            "starttime": 1710000000 + vmid,
            "endtime": 1710000300 + vmid,
            "status": "OK",
        }

    async def _fake_list_vms(*_args, **_kwargs):
        return vms

    async def _fake_get_node_tasks(_session, node, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        if node == "partial":
            if kwargs["start"] == 0:
                return [_task(node, 101), _task(node, 102)]
            raise TimeoutError("later archive page timed out")
        if node == "healthy":
            return [_task(node, 103)]
        if node == "migrated":
            if kwargs["start"] > 0:
                return []
            return [
                _task(node, 101, "UPID:partial:101"),
                _task(node, 104),
            ]
        return []

    async def _fake_bulk(_nb, _path, **kwargs):
        payloads.extend(kwargs["payloads"])
        return SimpleNamespace(created=len(payloads), updated=0, unchanged=0, failed=0, records=[])

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history._TASK_ARCHIVE_PAGE_SIZE", 2, raising=False
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history._list_all_vms_with_proxmox_id",
        _fake_list_vms,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_tasks", _fake_get_node_tasks
    )
    monkeypatch.setattr("proxbox_api.services.sync.task_history.dump_models", lambda items: items)
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.rest_bulk_reconcile_async", _fake_bulk
    )

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(),
            pxs=[px],
            cluster_status=[cluster],
            fetch_max_concurrency=2,
            websocket=_Bridge(),
        )
    )

    assert max_active == 2
    assert result["created"] == 4
    assert result["degraded"] is True
    assert result["errors"] == 1
    assert {payload["virtual_machine"] for payload in payloads} == {601, 602, 603, 604}
    assert len({payload["upid"] for payload in payloads}) == 4
    assert phase_summaries[-1]["failed"] == 1
    assert "degraded coverage" in str(phase_summaries[-1]["message"])


def test_all_task_history_skips_ambiguous_legacy_cluster_vmid(monkeypatch):
    px = SimpleNamespace(db_endpoint_id=None)
    cluster = SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")])
    vms = [
        {
            "id": 501,
            "cluster": {"name": "lab"},
            "custom_fields": {"proxmox_vm_id": 101, "proxmox_vm_type": "qemu"},
        },
        {
            "id": 502,
            "cluster": {"name": "lab"},
            "custom_fields": {"proxmox_vm_id": 101, "proxmox_vm_type": "qemu"},
        },
    ]

    async def _fake_list_vms(*_args, **_kwargs):
        return vms

    async def _fake_get_node_tasks(_session, node, **_kwargs):
        return [
            {
                "upid": f"UPID:{node}:101",
                "node": node,
                "id": "101",
                "type": "qmstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
        ]

    async def _unexpected_bulk(*_args, **_kwargs):
        raise AssertionError("ambiguous task must not be written")

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history._list_all_vms_with_proxmox_id",
        _fake_list_vms,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_tasks", _fake_get_node_tasks
    )
    monkeypatch.setattr("proxbox_api.services.sync.task_history.dump_models", lambda items: items)
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.rest_bulk_reconcile_async",
        _unexpected_bulk,
    )

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(), pxs=[px], cluster_status=[cluster]
        )
    )

    assert result["created"] == 0
    assert result["skipped"] > 0
    assert result["degraded"] is True
    assert result["errors"] == 1


def test_all_task_history_marks_exact_vm_ownership_collision_degraded(monkeypatch):
    px = SimpleNamespace(db_endpoint_id=11)
    cluster = SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")])
    vms = [
        {
            "id": netbox_id,
            "cluster": {"name": "lab"},
            "custom_fields": {
                "proxmox_endpoint_id": 11,
                "proxmox_vm_id": 101,
                "proxmox_vm_type": "qemu",
            },
        }
        for netbox_id in (501, 502)
    ]

    async def _list_vms(*_args, **_kwargs):
        return vms

    async def _fetch(_session, node, **_kwargs):
        return [
            {
                "upid": f"UPID:{node}:101",
                "id": "101",
                "type": "qmstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
        ]

    async def _unexpected_bulk(*_args, **_kwargs):
        raise AssertionError("ambiguous task must not be written")

    monkeypatch.setattr(task_history_service, "_list_all_vms_with_proxmox_id", _list_vms)
    monkeypatch.setattr(task_history_service, "get_node_tasks", _fetch)
    monkeypatch.setattr(task_history_service, "dump_models", lambda items: items)
    monkeypatch.setattr(task_history_service, "rest_bulk_reconcile_async", _unexpected_bulk)

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(),
            pxs=[px],
            cluster_status=[cluster],
        )
    )

    assert result["created"] == 0
    assert result["skipped"] == 1
    assert result["degraded"] is True
    assert result["errors"] == 1


def test_all_task_history_marks_sidecar_and_endpointless_legacy_collision_degraded(
    monkeypatch,
):
    vms = [
        {"id": 501, "name": "sidecar-owner", "cluster": {"name": "stale"}},
        {
            "id": 502,
            "name": "legacy-claimant",
            "cluster": {"name": "lab"},
            "custom_fields": {
                "proxmox_vm_id": 101,
                "proxmox_vm_type": "qemu",
            },
        },
    ]

    async def _list_vms(*_args, **_kwargs):
        return vms

    async def _scan(_nb):
        return VMSyncStateIdentityScan(
            rows=(
                {
                    "id": 1,
                    "virtual_machine": {"id": 501},
                    "proxmox_vm_id": 101,
                    "proxmox_endpoint_raw_id": 11,
                    "proxmox_vm_type": "qemu",
                    "proxmox_cluster_name": "lab",
                },
            )
        )

    async def _fetch(_session, node, **_kwargs):
        return [
            {
                "upid": f"UPID:{node}:101",
                "id": "101",
                "type": "qmstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
        ]

    bulk_calls = 0

    async def _unexpected_bulk(*_args, **_kwargs):
        nonlocal bulk_calls
        bulk_calls += 1
        raise AssertionError("mixed identity collision must not be reconciled")

    monkeypatch.setattr(task_history_service, "_list_all_vms_with_proxmox_id", _list_vms)
    monkeypatch.setattr(task_history_service, "load_vm_sync_state_identities", _scan)
    monkeypatch.setattr(task_history_service, "get_node_tasks", _fetch)
    monkeypatch.setattr(task_history_service, "dump_models", lambda items: items)
    monkeypatch.setattr(task_history_service, "rest_bulk_reconcile_async", _unexpected_bulk)

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(),
            pxs=[SimpleNamespace(db_endpoint_id=11)],
            cluster_status=[SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")])],
        )
    )

    assert result == {
        "count": 2,
        "created": 0,
        "skipped": 1,
        "degraded": True,
        "errors": 1,
    }
    assert bulk_calls == 0


def test_all_task_history_unrelated_archive_vmid_is_only_skipped(monkeypatch):
    vm = {
        "id": 501,
        "cluster": {"name": "lab"},
        "custom_fields": {
            "proxmox_endpoint_id": 11,
            "proxmox_vm_id": 101,
            "proxmox_vm_type": "qemu",
        },
    }

    async def _list_vms(*_args, **_kwargs):
        return [vm]

    async def _fetch(_session, node, **_kwargs):
        return [
            {
                "upid": f"UPID:{node}:{vmid}",
                "id": str(vmid),
                "type": "qmstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
            for vmid in (101, 999)
        ]

    async def _bulk(_nb, _path, **kwargs):
        assert len(kwargs["payloads"]) == 1
        return SimpleNamespace(created=1, updated=0, unchanged=0, failed=0, records=[])

    monkeypatch.setattr(task_history_service, "_list_all_vms_with_proxmox_id", _list_vms)
    monkeypatch.setattr(task_history_service, "get_node_tasks", _fetch)
    monkeypatch.setattr(task_history_service, "dump_models", lambda items: items)
    monkeypatch.setattr(task_history_service, "rest_bulk_reconcile_async", _bulk)

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(),
            pxs=[SimpleNamespace(db_endpoint_id=11)],
            cluster_status=[SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")])],
        )
    )

    assert result == {"count": 1, "created": 1, "skipped": 1}


def test_all_task_history_fails_closed_for_legacy_vm_across_duplicate_cluster_names(
    monkeypatch,
):
    px_a = SimpleNamespace(db_endpoint_id=11)
    px_b = SimpleNamespace(db_endpoint_id=22)
    clusters = [
        SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")]),
        SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-b")]),
    ]
    legacy_vm = {
        "id": 501,
        "cluster": {"name": "lab"},
        "custom_fields": {"proxmox_vm_id": 101, "proxmox_vm_type": "qemu"},
    }

    async def _fake_list_vms(*_args, **_kwargs):
        return [legacy_vm]

    async def _fake_get_node_tasks(_session, node, **_kwargs):
        return [
            {
                "upid": f"UPID:{node}:101",
                "node": node,
                "id": "101",
                "type": "qmstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
        ]

    async def _unexpected_bulk(*_args, **_kwargs):
        raise AssertionError("legacy identity must fail closed across duplicate estates")

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history._list_all_vms_with_proxmox_id",
        _fake_list_vms,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_tasks", _fake_get_node_tasks
    )
    monkeypatch.setattr("proxbox_api.services.sync.task_history.dump_models", lambda items: items)
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.rest_bulk_reconcile_async",
        _unexpected_bulk,
    )

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(), pxs=[px_a, px_b], cluster_status=clusters
        )
    )

    assert result["created"] == 0
    assert result["skipped"] == 2
    assert result["degraded"] is True
    assert result["errors"] == 2


def test_all_task_history_never_legacy_maps_a_known_mismatched_endpoint(monkeypatch):
    source = SimpleNamespace(db_endpoint_id=11)
    cluster = SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")])
    vm_on_other_endpoint = {
        "id": 501,
        "cluster": {"name": "lab"},
        "custom_fields": {
            "proxmox_endpoint_id": 22,
            "proxmox_vm_id": 101,
            "proxmox_vm_type": "qemu",
        },
    }

    async def _fake_list_vms(*_args, **_kwargs):
        return [vm_on_other_endpoint]

    async def _unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("a selected VM must not fetch a mismatched endpoint")

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history._list_all_vms_with_proxmox_id",
        _fake_list_vms,
    )
    monkeypatch.setattr("proxbox_api.services.sync.task_history.get_node_tasks", _unexpected_fetch)

    with pytest.raises(ProxboxException, match="no selected Proxmox nodes"):
        asyncio.run(
            sync_all_virtual_machine_task_histories(
                netbox_session=object(), pxs=[source], cluster_status=[cluster]
            )
        )


def test_all_task_history_reports_partially_missing_target_scope_as_degraded(monkeypatch):
    vms = [
        {
            "id": 501,
            "cluster": {"name": "lab-a"},
            "custom_fields": {
                "proxmox_endpoint_id": 11,
                "proxmox_vm_id": 101,
                "proxmox_vm_type": "qemu",
            },
        },
        {
            "id": 502,
            "cluster": {"name": "lab-b"},
            "custom_fields": {
                "proxmox_endpoint_id": 22,
                "proxmox_vm_id": 102,
                "proxmox_vm_type": "qemu",
            },
        },
    ]

    async def _list_vms(*_args, **_kwargs):
        return vms

    async def _fetch(_session, node, **_kwargs):
        return [
            {
                "upid": f"UPID:{node}:101",
                "id": "101",
                "type": "qmstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
        ]

    async def _bulk(*_args, **_kwargs):
        return SimpleNamespace(created=1, updated=0, unchanged=0, failed=0, records=[])

    monkeypatch.setattr(task_history_service, "_list_all_vms_with_proxmox_id", _list_vms)
    monkeypatch.setattr(task_history_service, "get_node_tasks", _fetch)
    monkeypatch.setattr(task_history_service, "dump_models", lambda items: items)
    monkeypatch.setattr(task_history_service, "rest_bulk_reconcile_async", _bulk)

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(),
            pxs=[SimpleNamespace(db_endpoint_id=11)],
            cluster_status=[
                SimpleNamespace(name="lab-a", node_list=[SimpleNamespace(name="pve-a")])
            ],
        )
    )

    assert result["count"] == 2
    assert result["created"] == 1
    assert result["degraded"] is True
    assert result["errors"] == 1


def test_all_task_history_empty_node_coverage_for_every_scope_is_fatal(monkeypatch):
    vm = {
        "id": 501,
        "cluster": {"name": "lab"},
        "custom_fields": {
            "proxmox_endpoint_id": 11,
            "proxmox_vm_id": 101,
            "proxmox_vm_type": "qemu",
        },
    }

    async def _list_vms(*_args, **_kwargs):
        return [vm]

    monkeypatch.setattr(task_history_service, "_list_all_vms_with_proxmox_id", _list_vms)

    with pytest.raises(ProxboxException, match="no selected Proxmox nodes") as exc_info:
        asyncio.run(
            sync_all_virtual_machine_task_histories(
                netbox_session=object(),
                pxs=[SimpleNamespace(db_endpoint_id=11)],
                cluster_status=[SimpleNamespace(name="lab", node_list=[])],
            )
        )

    assert exc_info.value.http_status_code == 502
    assert "endpoint=11/cluster=lab" in str(exc_info.value.detail)


def test_all_task_history_all_node_failure_is_fatal(monkeypatch):
    px = SimpleNamespace(db_endpoint_id=11)
    cluster = SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")])
    vm = {
        "id": 501,
        "cluster": {"name": "lab"},
        "custom_fields": {
            "proxmox_endpoint_id": 11,
            "proxmox_vm_id": 101,
            "proxmox_vm_type": "qemu",
        },
    }

    async def _fake_list_vms(*_args, **_kwargs):
        return [vm]

    async def _failing_fetch(*_args, **_kwargs):
        raise TimeoutError("node unavailable")

    async def _unexpected_bulk(*_args, **_kwargs):
        raise AssertionError("no archive rows means no reconcile")

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history._list_all_vms_with_proxmox_id",
        _fake_list_vms,
    )
    monkeypatch.setattr("proxbox_api.services.sync.task_history.get_node_tasks", _failing_fetch)
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.rest_bulk_reconcile_async",
        _unexpected_bulk,
    )

    with pytest.raises(ProxboxException, match="failed for every selected node"):
        asyncio.run(
            sync_all_virtual_machine_task_histories(
                netbox_session=object(), pxs=[px], cluster_status=[cluster]
            )
        )


def test_all_task_history_vm_list_failure_is_fatal(monkeypatch):
    async def _failing_list(*_args, **_kwargs):
        raise RuntimeError("NetBox unavailable")

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history._list_all_vms_with_proxmox_id",
        _failing_list,
    )

    with pytest.raises(ProxboxException, match="Unable to list VMs"):
        asyncio.run(
            sync_all_virtual_machine_task_histories(
                netbox_session=object(), pxs=[], cluster_status=[]
            )
        )


def test_all_task_history_global_bulk_reconcile_failure_is_fatal(monkeypatch):
    vm = {
        "id": 501,
        "cluster": {"name": "lab"},
        "custom_fields": {
            "proxmox_endpoint_id": 11,
            "proxmox_vm_id": 101,
            "proxmox_vm_type": "qemu",
        },
    }

    async def _fake_list(*_args, **_kwargs):
        return [vm]

    async def _fake_tasks(_session, node, **_kwargs):
        return [
            {
                "upid": f"UPID:{node}:101",
                "node": node,
                "id": "101",
                "type": "qmstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
        ]

    async def _failing_bulk(*_args, **_kwargs):
        raise RuntimeError("NetBox bulk unavailable")

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history._list_all_vms_with_proxmox_id",
        _fake_list,
    )
    monkeypatch.setattr("proxbox_api.services.sync.task_history.get_node_tasks", _fake_tasks)
    monkeypatch.setattr("proxbox_api.services.sync.task_history.dump_models", lambda items: items)
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.rest_bulk_reconcile_async",
        _failing_bulk,
    )

    with pytest.raises(ProxboxException, match="Bulk task-history reconciliation failed"):
        asyncio.run(
            sync_all_virtual_machine_task_histories(
                netbox_session=object(),
                pxs=[SimpleNamespace(db_endpoint_id=11)],
                cluster_status=[
                    SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")])
                ],
            )
        )


def test_all_task_history_preserves_cancellation_without_reconciling(monkeypatch):
    px = SimpleNamespace(db_endpoint_id=11)
    cluster = SimpleNamespace(name="lab", node_list=[SimpleNamespace(name="pve-a")])
    vm = {
        "id": 501,
        "cluster": {"name": "lab"},
        "custom_fields": {
            "proxmox_endpoint_id": 11,
            "proxmox_vm_id": 101,
            "proxmox_vm_type": "qemu",
        },
    }

    async def _fake_list_vms(*_args, **_kwargs):
        return [vm]

    async def _cancelled_fetch(*_args, **_kwargs):
        raise asyncio.CancelledError

    async def _unexpected_bulk(*_args, **_kwargs):
        raise AssertionError("cancelled collection must not reconcile")

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history._list_all_vms_with_proxmox_id",
        _fake_list_vms,
    )
    monkeypatch.setattr("proxbox_api.services.sync.task_history.get_node_tasks", _cancelled_fetch)
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.rest_bulk_reconcile_async",
        _unexpected_bulk,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            sync_all_virtual_machine_task_histories(
                netbox_session=object(), pxs=[px], cluster_status=[cluster]
            )
        )


def test_all_task_history_selected_scope_fetches_only_selected_endpoint(monkeypatch):
    px_a = SimpleNamespace(db_endpoint_id=11)
    px_b = SimpleNamespace(db_endpoint_id=22)
    clusters = [
        SimpleNamespace(name="lab-a", node_list=[SimpleNamespace(name="pve-a")]),
        SimpleNamespace(name="lab-b", node_list=[SimpleNamespace(name="pve-b")]),
    ]
    selected_vm = {
        "id": 502,
        "cluster": {"name": "lab-b"},
        "custom_fields": {
            "proxmox_endpoint_id": 22,
            "proxmox_vm_id": 102,
            "proxmox_vm_type": "lxc",
        },
    }
    fetched_endpoints: list[int] = []
    bulk_queries: list[dict[str, object] | None] = []

    async def _fake_list_vms(_nb, batch_size=500, *, netbox_vm_ids=None):
        assert netbox_vm_ids == [502]
        return [selected_vm]

    async def _fake_get_node_tasks(session, node, **_kwargs):
        fetched_endpoints.append(session.db_endpoint_id)
        return [
            {
                "upid": f"UPID:{node}:102",
                "node": node,
                "id": "102",
                "type": "vzstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
        ]

    async def _fake_bulk(_nb, _path, **kwargs):
        bulk_queries.append(kwargs["base_query"])
        return SimpleNamespace(created=1, updated=0, unchanged=0, failed=0, records=[])

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history._list_all_vms_with_proxmox_id",
        _fake_list_vms,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_tasks", _fake_get_node_tasks
    )
    monkeypatch.setattr("proxbox_api.services.sync.task_history.dump_models", lambda items: items)
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.rest_bulk_reconcile_async", _fake_bulk
    )

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(),
            pxs=[px_a, px_b],
            cluster_status=clusters,
            netbox_vm_ids=[502],
        )
    )

    assert result["created"] == 1
    assert fetched_endpoints == [22]
    assert bulk_queries == [None]


@pytest.mark.parametrize(
    ("requested_ids", "returned_vms", "expected_detail"),
    [
        ([501], [], "missing id(s): [501]"),
        ([501, 502], [{"id": 501}], "missing id(s): [502]"),
        ([], [], "selection was empty"),
    ],
)
def test_all_task_history_explicit_lookup_requires_complete_coverage(
    monkeypatch,
    requested_ids,
    returned_vms,
    expected_detail,
):
    async def _selected_list(_nb, batch_size=500, *, netbox_vm_ids=None):
        assert batch_size == 500
        assert netbox_vm_ids == requested_ids
        return returned_vms

    monkeypatch.setattr(task_history_service, "_list_all_vms_with_proxmox_id", _selected_list)

    with pytest.raises(ProxboxException, match="explicitly selected NetBox VMs") as exc_info:
        asyncio.run(
            sync_all_virtual_machine_task_histories(
                netbox_session=object(),
                pxs=[],
                cluster_status=[],
                netbox_vm_ids=requested_ids,
            )
        )

    assert exc_info.value.http_status_code == 502
    assert expected_detail in str(exc_info.value.detail)


def test_all_task_history_skips_same_upid_mapped_to_different_endpoint_owners(monkeypatch):
    px_a = SimpleNamespace(db_endpoint_id=11)
    px_b = SimpleNamespace(db_endpoint_id=22)
    clusters = [
        SimpleNamespace(name="lab-a", node_list=[SimpleNamespace(name="pve-a")]),
        SimpleNamespace(name="lab-b", node_list=[SimpleNamespace(name="pve-b")]),
    ]
    vms = [
        {
            "id": 501,
            "cluster": {"name": "lab-a"},
            "custom_fields": {
                "proxmox_endpoint_id": 11,
                "proxmox_vm_id": 101,
                "proxmox_vm_type": "qemu",
            },
        },
        {
            "id": 502,
            "cluster": {"name": "lab-b"},
            "custom_fields": {
                "proxmox_endpoint_id": 22,
                "proxmox_vm_id": 101,
                "proxmox_vm_type": "lxc",
            },
        },
    ]

    async def _fake_list_vms(*_args, **_kwargs):
        return vms

    async def _fake_get_node_tasks(_session, node, **_kwargs):
        return [
            {
                "upid": "UPID:collision:101",
                "node": node,
                "id": "101",
                "type": "qmstart",
                "user": "root@pam",
                "starttime": 1710000000,
                "endtime": 1710000300,
                "status": "OK",
            }
        ]

    async def _unexpected_bulk(*_args, **_kwargs):
        raise AssertionError("conflicting UPID ownership must not be reconciled")

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history._list_all_vms_with_proxmox_id",
        _fake_list_vms,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.get_node_tasks", _fake_get_node_tasks
    )
    monkeypatch.setattr("proxbox_api.services.sync.task_history.dump_models", lambda items: items)
    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history.rest_bulk_reconcile_async",
        _unexpected_bulk,
    )

    result = asyncio.run(
        sync_all_virtual_machine_task_histories(
            netbox_session=object(),
            pxs=[px_a, px_b],
            cluster_status=clusters,
        )
    )

    assert result["created"] == 0
    assert result["skipped"] > 0
    assert result["degraded"] is True
    assert result["errors"] == 1


def test_individual_task_filter_uses_archive_id_when_vmid_field_is_absent(monkeypatch):
    """PVE archive rows identify the guest in ``id`` rather than ``vmid``."""

    async def _fake_node_tasks(*_args, **_kwargs):
        return [
            SimpleNamespace(
                model_dump=lambda **_dump_kwargs: {
                    "upid": "UPID:pve-a:101",
                    "id": "101",
                }
            ),
            SimpleNamespace(
                model_dump=lambda **_dump_kwargs: {
                    "upid": "UPID:pve-a:other",
                    "id": "999",
                }
            ),
            SimpleNamespace(
                model_dump=lambda **_dump_kwargs: {
                    "upid": "UPID:pve-a:non-vm",
                    "id": "network-reload",
                }
            ),
        ]

    monkeypatch.setattr("proxbox_api.services.proxmox_helpers.get_node_tasks", _fake_node_tasks)

    result = asyncio.run(get_vm_tasks_individual(object(), "pve-a", vmid=101))

    assert result == [{"upid": "UPID:pve-a:101", "id": "101"}]


def test_get_node_tasks_forwards_archive_pagination_filters():
    captured: list[dict[str, object]] = []

    class _Tasks:
        def get(self, **kwargs):
            captured.append(kwargs)
            return []

    class _Node:
        tasks = _Tasks()

    class _Nodes:
        def __call__(self, _node):
            return _Node()

    session = SimpleNamespace(session=SimpleNamespace(nodes=_Nodes()))

    result = get_node_tasks(
        session,
        "pve-a",
        source="archive",
        start=500,
        limit=500,
        since=1700000000,
        until=1700001000,
        errors=True,
    )

    assert result == []
    assert captured == [
        {
            "source": "archive",
            "start": 500,
            "limit": 500,
            "since": 1700000000,
            "until": 1700001000,
            "errors": True,
        }
    ]


def test_netbox_task_history_sync_state_accepts_long_exitstatus():
    """Regression test for issue #330: exitstatus/status longer than 255 chars must not raise."""
    from proxbox_api.proxmox_to_netbox.models import NetBoxTaskHistorySyncState

    long_error = "ERROR: " + ("x" * 500)
    model = NetBoxTaskHistorySyncState.model_validate(
        {
            "virtual_machine": 1,
            "upid": "UPID:pve01:00001CB4:0001DA12:69AA4164:qmcreate:101:root@pam:",
            "node": "pve01",
            "task_type": "qmcreate",
            "username": "root@pam",
            "start_time": "2024-03-10T00:00:00",
            "exitstatus": long_error,
            "status": long_error,
        }
    )
    # Both fields must be accepted without validation error
    assert model.exitstatus is not None
    assert model.status is not None
    # Values longer than 255 chars are accepted; truncation kicks in at 2048
    assert len(model.exitstatus) > 255
    assert len(model.status) > 255


def test_netbox_task_history_sync_state_truncates_at_2048():
    """Values exceeding 2048 chars are truncated before reaching NetBox."""
    from proxbox_api.proxmox_to_netbox.models import NetBoxTaskHistorySyncState

    huge_value = "E" * 3000
    model = NetBoxTaskHistorySyncState.model_validate(
        {
            "virtual_machine": 1,
            "upid": "UPID:pve01:00001CB4:0001DA12:69AA4164:qmcreate:101:root@pam:",
            "node": "pve01",
            "task_type": "qmcreate",
            "username": "root@pam",
            "start_time": "2024-03-10T00:00:00",
            "exitstatus": huge_value,
            "status": huge_value,
        }
    )
    assert len(model.exitstatus) == 2048
    assert len(model.status) == 2048
