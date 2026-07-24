"""Regression tests for VM snapshot synchronization."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from proxbox_api.exception import ProxboxException
from proxbox_api.services.sync import snapshots as snapshots_module
from proxbox_api.services.sync.snapshots import create_virtual_machine_snapshots


@pytest.fixture(autouse=True)
def _bridge_snapshot_vm_pagination_to_legacy_test_fakes(monkeypatch):
    """Keep path-aware test fakes while production uses the shared paginator."""

    async def _legacy_bridge(
        nb,
        path,
        *,
        page_size=500,
        base_query=None,
        **_kwargs,
    ):
        query = dict(base_query or {})
        query.setdefault("limit", page_size)
        query.setdefault("offset", 0)
        return await snapshots_module.rest_list_async(nb, path, query=query)

    monkeypatch.setattr(snapshots_module, "rest_list_paginated_async", _legacy_bridge)


def test_create_virtual_machine_snapshots_uses_nested_custom_fields_proxmox_vm_id(
    monkeypatch,
):
    session = object()
    proxmox_session = object()
    calls = {"get_vm_snapshots": [], "rest_list_async": []}
    reconciled = []

    async def _fake_rest_list(_nb, _path, query=None):
        calls["rest_list_async"].append({"path": _path, "query": query})
        if _path == "/api/plugins/proxbox/storage/":
            return [
                {"id": 41, "cluster": "cluster-a", "name": "local-zfs"},
            ]
        if _path == "/api/virtualization/virtual-disks/":
            return [
                {
                    "id": 301,
                    "virtual_machine": 7,
                    "name": "local-zfs:vm-101-disk-0",
                }
            ]
        return [
            {
                "id": 7,
                "name": "vm-101",
                "custom_fields": {"proxmox_vm_id": 101},
            }
        ]

    def _fake_get_vm_snapshots(*, session, node, vm_type, vmid, raise_on_error=False):
        assert raise_on_error is True
        calls["get_vm_snapshots"].append(
            {"session": session, "node": node, "vm_type": vm_type, "vmid": vmid}
        )
        return [
            {
                "name": "pre-upgrade",
                "description": "Before upgrade",
                "snaptime": 1712345678,
                "type": "qemu",
            }
        ]

    async def _fake_bulk_reconcile(_nb, _path, *, payloads, **kwargs):
        # Capture lookup info from the payloads for test verification
        if _path == "/api/plugins/proxbox/snapshots/":
            for payload in payloads:
                reconciled.append(
                    (
                        {
                            "vmid": payload.get("vmid"),
                            "name": payload.get("name"),
                            "node": payload.get("node"),
                        },
                        payload,
                    )
                )
        # Return bulk reconcile result with created snapshots
        from proxbox_api.netbox_rest import BulkReconcileResult

        return BulkReconcileResult(
            records=[{"id": 99, **payload} for payload in payloads],
            created=len(payloads),
            updated=0,
            unchanged=0,
            failed=0,
        )

    monkeypatch.setattr(
        "proxbox_api.services.sync.snapshots.rest_list_async",
        _fake_rest_list,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.snapshots.get_vm_snapshots",
        _fake_get_vm_snapshots,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.snapshots.rest_bulk_reconcile_async",
        _fake_bulk_reconcile,
    )

    px = type("P", (), {"session": proxmox_session, "name": "lab"})()
    result = asyncio.run(
        create_virtual_machine_snapshots(
            netbox_session=session,
            pxs=[px],
            cluster_status=[],
            cluster_resources=[
                {"cluster-a": [{"type": "qemu", "name": "vm-101", "vmid": "101", "node": "pve01"}]}
            ],
            tag=None,
            use_websocket=False,
        )
    )

    assert result == {"count": 1, "created": 1, "updated": 0, "skipped": 0, "deleted": 0}
    assert calls["get_vm_snapshots"] == [
        {"session": px, "node": "pve01", "vm_type": "qemu", "vmid": 101}
    ]
    assert calls["rest_list_async"][0] == {
        "path": "/api/virtualization/virtual-machines/",
        "query": {"limit": 500, "offset": 0},
    }
    assert calls["rest_list_async"][1] == {
        "path": "/api/plugins/proxbox/storage/",
        "query": None,
    }
    assert calls["rest_list_async"][2] == {
        "path": "/api/virtualization/virtual-disks/",
        "query": {"virtual_machine_id": 7, "ordering": "name"},
    }
    assert len(reconciled) == 1
    assert reconciled[0][0] == {"vmid": 101, "name": "pre-upgrade", "node": "pve01"}
    assert reconciled[0][1]["virtual_machine"] == 7
    assert reconciled[0][1]["proxmox_storage"] == 41


def test_create_virtual_machine_snapshots_scopes_fetch_by_endpoint(monkeypatch):
    endpoint_a = SimpleNamespace(db_endpoint_id=1, name="pve")
    endpoint_b = SimpleNamespace(db_endpoint_id=2, name="astro")
    calls = {"get_vm_snapshots": []}

    async def _fake_rest_list(_nb, _path, query=None):
        if _path == "/api/virtualization/virtual-machines/":
            return [
                {
                    "id": 7,
                    "name": "vm-105-a",
                    "cluster": {"name": "pve"},
                    "custom_fields": {
                        "proxmox_endpoint_id": 1,
                        "proxmox_vm_id": 105,
                        "proxmox_vm_type": "qemu",
                        "proxmox_node": "pve",
                    },
                },
                {
                    "id": 8,
                    "name": "vm-105-b",
                    "cluster": {"name": "astro"},
                    "custom_fields": {
                        "proxmox_endpoint_id": 2,
                        "proxmox_vm_id": 105,
                        "proxmox_vm_type": "qemu",
                        "proxmox_node": "astro",
                    },
                },
            ]
        if _path in {"/api/plugins/proxbox/storage/", "/api/virtualization/virtual-disks/"}:
            return []
        return []

    def _fake_get_vm_snapshots(*, session, node, vm_type, vmid, raise_on_error=False):
        assert raise_on_error is True
        calls["get_vm_snapshots"].append(
            {"session": session, "node": node, "vm_type": vm_type, "vmid": vmid}
        )
        return [{"name": f"snap-{node}", "type": "qemu"}]

    async def _fake_bulk_reconcile(_nb, _path, *, payloads, **kwargs):
        from proxbox_api.netbox_rest import BulkReconcileResult

        return BulkReconcileResult(
            records=[{"id": index, **payload} for index, payload in enumerate(payloads, start=1)],
            created=len(payloads),
            updated=0,
            unchanged=0,
            failed=0,
        )

    monkeypatch.setattr("proxbox_api.services.sync.snapshots.rest_list_async", _fake_rest_list)
    monkeypatch.setattr(
        "proxbox_api.services.sync.snapshots.get_vm_snapshots",
        _fake_get_vm_snapshots,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.snapshots.rest_bulk_reconcile_async",
        _fake_bulk_reconcile,
    )

    result = asyncio.run(
        create_virtual_machine_snapshots(
            netbox_session=object(),
            pxs=[endpoint_a, endpoint_b],
            cluster_status=[],
            cluster_resources=[
                {"pve": [{"type": "qemu", "name": "vm-105-a", "vmid": "105", "node": "pve"}]},
                {"astro": [{"type": "qemu", "name": "vm-105-b", "vmid": "105", "node": "astro"}]},
            ],
            tag=None,
            use_websocket=False,
        )
    )

    assert result == {"count": 2, "created": 2, "updated": 0, "skipped": 0, "deleted": 0}
    assert calls["get_vm_snapshots"] == [
        {"session": endpoint_a, "node": "pve", "vm_type": "qemu", "vmid": 105},
        {"session": endpoint_b, "node": "astro", "vm_type": "qemu", "vmid": 105},
    ]


def _snapshot_vm(
    *,
    netbox_id: int = 7,
    vmid: int = 105,
    endpoint_id: int | None = 1,
    cluster_name: str = "cluster-a",
) -> dict[str, object]:
    custom_fields: dict[str, object] = {
        "proxmox_vm_id": vmid,
        "proxmox_vm_type": "qemu",
    }
    if endpoint_id is not None:
        custom_fields["proxmox_endpoint_id"] = endpoint_id
    return {
        "id": netbox_id,
        "name": f"vm-{netbox_id}",
        "cluster": {"name": cluster_name},
        "custom_fields": custom_fields,
    }


def test_selected_snapshot_scope_uses_exact_netbox_owner_in_lookup(monkeypatch):
    selected_queries: list[dict] = []
    fetched_endpoints: list[int] = []
    reconciled: dict[str, object] = {}
    selected_vm = _snapshot_vm(netbox_id=8, endpoint_id=2, cluster_name="cluster-b")

    async def _selected_list(_nb, path, *, query=None):
        selected_queries.append(dict(query or {}))
        return [selected_vm]

    async def _empty_storage(_nb):
        return {}

    async def _no_disk(*_args, **_kwargs):
        return None

    def _snapshots(*, session, **_kwargs):
        fetched_endpoints.append(session.db_endpoint_id)
        return [{"name": "same-name", "type": "qemu"}]

    async def _bulk(_nb, _path, *, payloads, lookup_fields, **_kwargs):
        from proxbox_api.netbox_rest import BulkReconcileResult

        reconciled["payloads"] = payloads
        reconciled["lookup_fields"] = lookup_fields
        return BulkReconcileResult(
            records=[],
            created=len(payloads),
            updated=0,
            unchanged=0,
            failed=0,
        )

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _selected_list)
    monkeypatch.setattr(snapshots_module, "_load_storage_index", _empty_storage)
    monkeypatch.setattr(snapshots_module, "_resolve_snapshot_storage_record", _no_disk)
    monkeypatch.setattr(snapshots_module, "get_vm_snapshots", _snapshots)
    monkeypatch.setattr(snapshots_module, "rest_bulk_reconcile_async", _bulk)

    result = asyncio.run(
        create_virtual_machine_snapshots(
            netbox_session=object(),
            pxs=[
                SimpleNamespace(db_endpoint_id=1, name="cluster-a"),
                SimpleNamespace(db_endpoint_id=2, name="cluster-b"),
            ],
            cluster_status=[],
            cluster_resources=[
                {"cluster-a": [{"vmid": 105, "node": "pve01"}]},
                {"cluster-b": [{"vmid": 105, "node": "pve01"}]},
            ],
            netbox_vm_ids=[8],
        )
    )

    assert result["count"] == 1
    assert selected_queries == [{"id": ["8"]}]
    assert fetched_endpoints == [2]
    assert reconciled["lookup_fields"] == ["virtual_machine", "vmid", "name", "node"]
    assert reconciled["payloads"][0]["virtual_machine"] == 8


def test_selected_snapshot_lookup_failure_is_fatal(monkeypatch):
    async def _failed_lookup(*_args, **_kwargs):
        raise RuntimeError("NetBox selected lookup timed out")

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _failed_lookup)

    with pytest.raises(ProxboxException, match="explicitly selected NetBox VMs") as exc_info:
        asyncio.run(
            create_virtual_machine_snapshots(
                netbox_session=object(),
                pxs=[],
                cluster_status=[],
                cluster_resources=[],
                netbox_vm_ids=[7],
            )
        )

    assert exc_info.value.http_status_code == 502


def test_selected_snapshot_lookup_normalizes_upstream_proxbox_failure_to_502(monkeypatch):
    async def _failed_lookup(*_args, **_kwargs):
        raise ProxboxException(
            message="NetBox selected lookup unavailable",
            http_status_code=503,
        )

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _failed_lookup)

    with pytest.raises(ProxboxException, match="explicitly selected NetBox VMs") as exc_info:
        asyncio.run(
            create_virtual_machine_snapshots(
                netbox_session=object(),
                pxs=[],
                cluster_status=[],
                cluster_resources=[],
                netbox_vm_ids=[7],
            )
        )

    assert exc_info.value.http_status_code == 502


def test_selected_snapshot_lookup_requires_every_requested_vm(monkeypatch):
    async def _empty_lookup(*_args, **_kwargs):
        return []

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _empty_lookup)

    with pytest.raises(ProxboxException, match="did not return selected VM") as exc_info:
        asyncio.run(
            create_virtual_machine_snapshots(
                netbox_session=object(),
                pxs=[],
                cluster_status=[],
                cluster_resources=[],
                netbox_vm_ids=[7],
            )
        )

    assert exc_info.value.http_status_code == 502


def test_selected_snapshot_never_falls_back_to_another_cluster_node(monkeypatch):
    fetched = 0
    cleanup_lists = 0
    selected_vm = _snapshot_vm(netbox_id=8, endpoint_id=2, cluster_name="cluster-b")

    async def _selected_lookup(*_args, **_kwargs):
        return [selected_vm]

    async def _empty_storage(_nb):
        return {}

    def _unexpected_fetch(**_kwargs):
        nonlocal fetched
        fetched += 1
        return []

    async def _unexpected_cleanup(*_args, **_kwargs):
        nonlocal cleanup_lists
        cleanup_lists += 1
        return []

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _selected_lookup)
    monkeypatch.setattr(snapshots_module, "_load_storage_index", _empty_storage)
    monkeypatch.setattr(snapshots_module, "get_vm_snapshots", _unexpected_fetch)
    monkeypatch.setattr(snapshots_module, "rest_list_async", _unexpected_cleanup)

    result = asyncio.run(
        create_virtual_machine_snapshots(
            netbox_session=object(),
            pxs=[SimpleNamespace(db_endpoint_id=2, name="cluster-b")],
            cluster_status=[
                SimpleNamespace(
                    name="cluster-a",
                    node_list=[SimpleNamespace(name="pve-a")],
                )
            ],
            cluster_resources=[{"cluster-a": [{"vmid": 105, "node": "pve-a"}]}],
            netbox_vm_ids=[8],
            delete_nonexistent_snapshot=True,
        )
    )

    assert result["skipped"] == 1
    assert result["deleted"] == 0
    assert fetched == 0
    assert cleanup_lists == 0


def test_selected_snapshot_falls_back_only_within_known_cluster():
    node_name, cluster_name = snapshots_module._resolve_snapshot_node_context(
        vmid=105,
        node=None,
        cluster_name="cluster-b",
        cluster_status=[
            SimpleNamespace(name="cluster-a", node_list=[SimpleNamespace(name="pve-a")]),
            SimpleNamespace(name="cluster-b", node_list=[SimpleNamespace(name="pve-b")]),
        ],
        cluster_resources=[{"cluster-a": [{"vmid": 999, "node": "pve-a"}]}],
        require_unique_match=True,
    )

    assert (node_name, cluster_name) == ("pve-b", "cluster-b")


def test_production_snapshot_helper_failure_is_not_authoritative_empty_discovery(monkeypatch):
    cleanup_lists = 0
    selected_vm = _snapshot_vm()

    async def _selected_lookup(*_args, **_kwargs):
        return [selected_vm]

    async def _empty_storage(_nb):
        return {}

    async def _no_disk(*_args, **_kwargs):
        return None

    async def _unexpected_cleanup(*_args, **_kwargs):
        nonlocal cleanup_lists
        cleanup_lists += 1
        return []

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _selected_lookup)
    monkeypatch.setattr(snapshots_module, "_load_storage_index", _empty_storage)
    monkeypatch.setattr(snapshots_module, "_resolve_snapshot_storage_record", _no_disk)
    monkeypatch.setattr(snapshots_module, "rest_list_async", _unexpected_cleanup)

    # Keep the real get_vm_snapshots helper. This session has no SDK transport,
    # so the helper's historical catch-and-return-empty path is exercised.
    result = asyncio.run(
        create_virtual_machine_snapshots(
            netbox_session=object(),
            pxs=[SimpleNamespace(db_endpoint_id=1, name="cluster-a")],
            cluster_status=[],
            cluster_resources=[{"cluster-a": [{"vmid": 105, "node": "pve-a"}]}],
            netbox_vm_ids=[7],
            delete_nonexistent_snapshot=True,
        )
    )

    assert result["skipped"] == 1
    assert result["deleted"] == 0
    assert cleanup_lists == 0


@pytest.mark.parametrize(
    ("explicitly_selected", "failure_mode"),
    [
        (True, "endpoint_unavailable"),
        (False, "node_unavailable"),
        (False, "fetch_exception"),
    ],
)
def test_incomplete_snapshot_discovery_never_deletes(
    monkeypatch,
    explicitly_selected,
    failure_mode,
):
    cleanup_lists = 0
    deleted_ids: list[int] = []
    vm = _snapshot_vm()

    async def _estate_vms(*_args, **_kwargs):
        return [vm]

    async def _selected_vms(_nb, _path, *, query=None):
        assert query == {"id": ["7"]}
        return [vm]

    async def _empty_storage(_nb):
        return {}

    async def _no_disk(*_args, **_kwargs):
        return None

    async def _cleanup_list(_nb, path, **_kwargs):
        nonlocal cleanup_lists
        if path == "/api/plugins/proxbox/snapshots/":
            cleanup_lists += 1
        return []

    async def _delete(_nb, _path, ids):
        deleted_ids.extend(ids)
        return len(ids)

    def _fetch(**_kwargs):
        if failure_mode == "fetch_exception":
            raise RuntimeError("snapshot endpoint unavailable")
        return []

    monkeypatch.setattr(snapshots_module, "_list_all_vms_with_proxmox_id", _estate_vms)
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _selected_vms)
    monkeypatch.setattr(snapshots_module, "_load_storage_index", _empty_storage)
    monkeypatch.setattr(snapshots_module, "_resolve_snapshot_storage_record", _no_disk)
    monkeypatch.setattr(snapshots_module, "rest_list_async", _cleanup_list)
    monkeypatch.setattr(snapshots_module, "rest_bulk_delete_async", _delete)
    monkeypatch.setattr(snapshots_module, "get_vm_snapshots", _fetch)

    cluster_resources = (
        []
        if failure_mode == "node_unavailable"
        else [{"cluster-a": [{"vmid": 105, "node": "pve01"}]}]
    )
    endpoint = 2 if failure_mode == "endpoint_unavailable" else 1
    result = asyncio.run(
        create_virtual_machine_snapshots(
            netbox_session=object(),
            pxs=[SimpleNamespace(db_endpoint_id=endpoint, name="cluster-a")],
            cluster_status=[],
            cluster_resources=cluster_resources,
            netbox_vm_ids=[7] if explicitly_selected else None,
            delete_nonexistent_snapshot=True,
        )
    )

    assert result["deleted"] == 0
    assert result["skipped"] == 1
    assert cleanup_lists == 0
    assert deleted_ids == []


def test_selected_endpointless_snapshot_scope_rejects_ambiguous_sessions(monkeypatch):
    fetched = 0
    cleanup_lists = 0
    vm = _snapshot_vm(endpoint_id=None)

    async def _selected_vms(_nb, _path, *, query=None):
        assert query == {"id": ["7"]}
        return [vm]

    async def _empty_storage(_nb):
        return {}

    async def _no_disk(*_args, **_kwargs):
        return None

    async def _cleanup_list(*_args, **_kwargs):
        nonlocal cleanup_lists
        cleanup_lists += 1
        return []

    def _unexpected_fetch(**_kwargs):
        nonlocal fetched
        fetched += 1
        return []

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _selected_vms)
    monkeypatch.setattr(snapshots_module, "_load_storage_index", _empty_storage)
    monkeypatch.setattr(snapshots_module, "_resolve_snapshot_storage_record", _no_disk)
    monkeypatch.setattr(snapshots_module, "rest_list_async", _cleanup_list)
    monkeypatch.setattr(snapshots_module, "get_vm_snapshots", _unexpected_fetch)

    result = asyncio.run(
        create_virtual_machine_snapshots(
            netbox_session=object(),
            pxs=[
                SimpleNamespace(name="cluster-a"),
                SimpleNamespace(name="cluster-a"),
            ],
            cluster_status=[],
            cluster_resources=[{"cluster-a": [{"vmid": 105, "node": "pve01"}]}],
            netbox_vm_ids=[7],
            delete_nonexistent_snapshot=True,
        )
    )

    assert result["skipped"] == 1
    assert fetched == 0
    assert cleanup_lists == 0


def test_complete_empty_snapshot_discovery_allows_owner_scoped_cleanup(monkeypatch):
    deleted_ids: list[int] = []
    vm = _snapshot_vm()

    async def _estate_vms(*_args, **_kwargs):
        return [vm]

    async def _empty_storage(_nb):
        return {}

    async def _no_disk(*_args, **_kwargs):
        return None

    async def _existing(_nb, path, **_kwargs):
        assert path == "/api/plugins/proxbox/snapshots/"
        return [
            {
                "id": 90,
                "virtual_machine": {"id": 7},
                "name": "stale",
            }
        ]

    async def _delete(_nb, _path, ids):
        deleted_ids.extend(ids)
        return len(ids)

    monkeypatch.setattr(snapshots_module, "_list_all_vms_with_proxmox_id", _estate_vms)
    monkeypatch.setattr(snapshots_module, "_load_storage_index", _empty_storage)
    monkeypatch.setattr(snapshots_module, "_resolve_snapshot_storage_record", _no_disk)
    monkeypatch.setattr(snapshots_module, "get_vm_snapshots", lambda **_kwargs: [])
    monkeypatch.setattr(snapshots_module, "rest_list_async", _existing)
    monkeypatch.setattr(snapshots_module, "rest_bulk_delete_async", _delete)

    result = asyncio.run(
        create_virtual_machine_snapshots(
            netbox_session=object(),
            pxs=[SimpleNamespace(db_endpoint_id=1, name="cluster-a")],
            cluster_status=[],
            cluster_resources=[{"cluster-a": [{"vmid": 105, "node": "pve01"}]}],
            delete_nonexistent_snapshot=True,
        )
    )

    assert result["deleted"] == 1
    assert deleted_ids == [90]
