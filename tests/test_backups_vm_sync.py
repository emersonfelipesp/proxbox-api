"""Regression tests for VM backup synchronization."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import RestRecord
from proxbox_api.routes.virtualization.virtual_machines import backups_vm
from proxbox_api.routes.virtualization.virtual_machines.backups_vm import (
    _BackupVMCache,
    _bulk_reconcile_backups,
    _create_all_virtual_machine_backups,
    _normalize_backup_format,
    _normalize_backup_subtype,
    create_netbox_backups,
    get_node_backups,
)


def _netbox_vm(
    netbox_id: int,
    *,
    endpoint_id: int,
    cluster_name: str,
    vmid: int = 101,
) -> dict[str, object]:
    return {
        "id": netbox_id,
        "name": f"vm-{netbox_id}",
        "cluster": {"name": cluster_name},
        "custom_fields": {
            "proxmox_endpoint_id": endpoint_id,
            "proxmox_vm_id": vmid,
        },
    }


@pytest.fixture(autouse=True)
def _allow_dict_proxmox_rows(monkeypatch):
    """Production receives Pydantic SDK rows; focused tests use equivalent dicts."""

    monkeypatch.setattr(backups_vm, "dump_models", lambda items: items)


def test_normalize_backup_subtype_aliases_and_volume_fallbacks():
    assert _normalize_backup_subtype("ct", "pbs:backup/ct/100/2026-01-01T00:00:00Z") == "lxc"
    assert _normalize_backup_subtype("vm", "pbs:backup/vm/101/2026-01-01T00:00:00Z") == "qemu"
    assert _normalize_backup_subtype(None, "pbs:backup/ct/100/2026-01-01T00:00:00Z") == "lxc"
    assert _normalize_backup_subtype("", "pbs:backup/vm/101/2026-01-01T00:00:00Z") == "qemu"
    assert _normalize_backup_subtype("unknown", "local:backup/other") == "undefined"


def test_normalize_backup_format_aliases_and_volume_fallbacks():
    assert _normalize_backup_format("zst", "pbs:backup/vm/101/2026-01-01T00:00:00Z") == "tzst"
    assert _normalize_backup_format("vma.zst", "pbs:backup/vm/101/2026-01-01T00:00:00Z") == "tzst"
    assert _normalize_backup_format(None, "pbs:backup/ct/100/2026-01-01T00:00:00Z") == "pbs-ct"
    assert _normalize_backup_format("", "pbs:backup/vm/101/2026-01-01T00:00:00Z") == "pbs-vm"
    assert _normalize_backup_format("unexpected", "local:backup/foo") == "undefined"


def test_create_netbox_backups_links_storage_by_volume_prefix(monkeypatch):
    reconciled: list[tuple[dict, dict]] = []
    journal_entries: list[dict] = []

    async def _fake_rest_list_async(_nb, _path, *, query=None):
        assert _path == "/api/virtualization/virtual-machines/"
        assert query == {"cf_proxmox_vm_id": 101}
        return [_netbox_vm(7, endpoint_id=1, cluster_name="cluster-a")]

    async def _fake_reconcile_async(_nb, _path, lookup, payload, **kwargs):
        reconciled.append((lookup, payload))

        class _Record:
            id = 55

        return _Record()

    async def _fake_rest_create_async(_nb, _path, payload):
        journal_entries.append(payload)
        return payload

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.backups_vm.rest_list_async",
        _fake_rest_list_async,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.backups_vm.rest_reconcile_async",
        _fake_reconcile_async,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.backups_vm.rest_create_async",
        _fake_rest_create_async,
    )

    backup = {
        "vmid": 101,
        "volid": "local-zfs:vm-101-disk-0",
        "ctime": 1700000000,
        "size": 1024,
        "subtype": "qemu",
        "format": "qcow2",
        "content": "backup",
    }
    storage_index = {
        ("cluster-a", "local-zfs"): {"id": 99, "cluster": "cluster-a", "name": "local-zfs"}
    }

    result = asyncio.run(
        create_netbox_backups(
            backup,
            netbox_session=object(),
            cluster_name="cluster-a",
            storage_index=storage_index,
        )
    )

    assert result is not None
    assert reconciled[0][1]["storage"] == "local-zfs"
    assert journal_entries[0]["assigned_object_type"] == "netbox_proxbox.vmbackup"
    assert journal_entries[0]["assigned_object_id"] == 55


def test_create_netbox_backups_reuses_cached_vm_lookup(monkeypatch):
    queries: list[dict] = []

    async def _fake_rest_list_async(_nb, _path, *, query=None):
        queries.append(query or {})
        return [_netbox_vm(7, endpoint_id=1, cluster_name="cluster-a")]

    async def _fake_reconcile_async(_nb, _path, lookup, payload, **kwargs):
        return SimpleNamespace(id=55)

    async def _fake_rest_create_async(_nb, _path, payload):
        return payload

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.backups_vm.rest_list_async",
        _fake_rest_list_async,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.backups_vm.rest_reconcile_async",
        _fake_reconcile_async,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.backups_vm.rest_create_async",
        _fake_rest_create_async,
    )

    backup = {
        "vmid": 101,
        "volid": "local-zfs:vm-101-disk-0",
        "ctime": 1700000000,
        "size": 1024,
        "subtype": "qemu",
        "format": "qcow2",
        "content": "backup",
    }
    vm_cache: dict[int, dict | None] = {}

    asyncio.run(
        create_netbox_backups(
            backup,
            netbox_session=object(),
            cluster_name="cluster-a",
            storage_index={},
            vm_cache=vm_cache,
        )
    )
    asyncio.run(
        create_netbox_backups(
            backup,
            netbox_session=object(),
            cluster_name="cluster-a",
            storage_index={},
            vm_cache=vm_cache,
        )
    )

    assert queries == [{"cf_proxmox_vm_id": 101}]


def test_get_node_backups_enforces_vmid_filter_locally(monkeypatch):
    seen_backups: list[dict] = []

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.backups_vm.dump_models",
        lambda items: items,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.backups_vm.get_node_storage_content",
        lambda *args, **kwargs: [
            {"content": "backup", "vmid": 101, "volid": "local:vm-101-a"},
            {"content": "backup", "vmid": 202, "volid": "local:vm-202-a"},
        ],
    )

    async def _fake_create_netbox_backups(backup, *_args, **_kwargs):
        seen_backups.append(backup)
        return backup

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.backups_vm.create_netbox_backups",
        _fake_create_netbox_backups,
    )

    async def _run():
        tasks, volids = await get_node_backups(
            [object()],
            [SimpleNamespace(name="cluster-a", node_list=[SimpleNamespace(name="pve01")])],
            node="pve01",
            storage="local",
            netbox_session=object(),
            storage_index={},
            vmid="101",
        )
        results = await asyncio.gather(*tasks)
        return results, volids

    results, volids = asyncio.run(_run())

    assert [backup["vmid"] for backup in seen_backups] == [101]
    assert [result["vmid"] for result in results] == [101]
    assert volids == {"local:vm-101-a"}


def test_create_netbox_backups_resolves_duplicate_vmid_by_endpoint_and_cluster(monkeypatch):
    reconciled: list[tuple[dict, dict]] = []

    async def _fake_rest_list_async(_nb, _path, *, query=None):
        assert query == {"cf_proxmox_vm_id": 101}
        return [
            _netbox_vm(7, endpoint_id=1, cluster_name="cluster-a"),
            _netbox_vm(8, endpoint_id=2, cluster_name="cluster-b"),
        ]

    async def _fake_reconcile(_nb, _path, *, lookup, payload, **_kwargs):
        reconciled.append((lookup, payload))
        return SimpleNamespace(id=55)

    async def _fake_create(_nb, _path, payload):
        return payload

    monkeypatch.setattr(backups_vm, "rest_list_async", _fake_rest_list_async)
    monkeypatch.setattr(backups_vm, "rest_reconcile_async", _fake_reconcile)
    monkeypatch.setattr(backups_vm, "rest_create_async", _fake_create)

    result = asyncio.run(
        create_netbox_backups(
            {
                "content": "backup",
                "vmid": 101,
                "volid": "pbs:backup/vm/101/shared",
            },
            netbox_session=object(),
            endpoint_id=2,
            cluster_name="cluster-b",
        )
    )

    assert result is not None
    assert reconciled[0][0] == {
        "virtual_machine": 8,
        "volume_id": "pbs:backup/vm/101/shared",
    }
    assert reconciled[0][1]["virtual_machine"] == 8


def test_backup_vm_cache_never_uses_ambiguous_global_vmid():
    cache = _BackupVMCache()
    cache.add(_netbox_vm(7, endpoint_id=1, cluster_name="cluster-a"))
    cache.add(_netbox_vm(8, endpoint_id=2, cluster_name="cluster-b"))

    assert cache.resolve(endpoint_id=1, cluster_name="cluster-a", proxmox_vmid=101)["id"] == 7
    assert cache.resolve(endpoint_id=2, cluster_name="cluster-b", proxmox_vmid=101)["id"] == 8
    assert cache.resolve(endpoint_id=None, cluster_name=None, proxmox_vmid=101) is None

    single_owner_cache = _BackupVMCache()
    single_owner_cache.add(_netbox_vm(7, endpoint_id=1, cluster_name="cluster-a"))
    assert (
        single_owner_cache.resolve(
            endpoint_id=2,
            cluster_name="cluster-a",
            proxmox_vmid=101,
        )
        is None
    )


@pytest.mark.asyncio
async def test_bulk_backup_reconcile_keeps_same_volume_for_distinct_vm_owners(monkeypatch):
    submitted: list[dict] = []

    monkeypatch.setattr(backups_vm, "clear_rest_get_cache_for_path", lambda *_args: None)

    async def _empty_existing(*_args, **_kwargs):
        return []

    async def _bulk_create(_nb, path, payloads):
        if path == "/api/extras/journal-entries/":
            return []
        submitted.extend(payloads)
        return [
            RestRecord(
                SimpleNamespace(),
                path,
                {"id": index, **payload},
            )
            for index, payload in enumerate(payloads, start=1)
        ]

    monkeypatch.setattr(backups_vm, "rest_list_paginated_async", _empty_existing)
    monkeypatch.setattr(backups_vm, "rest_bulk_create_async", _bulk_create)

    common = {
        "storage": "pbs",
        "subtype": "qemu",
        "volume_id": "pbs:backup/vm/101/shared",
        "vmid": 101,
        "format": "pbs-vm",
    }
    results, created, patched = await _bulk_reconcile_backups(
        object(),
        [
            {**common, "virtual_machine": 7},
            {**common, "virtual_machine": 8},
        ],
        bulk_batch_size=50,
        bulk_batch_delay_ms=0,
    )

    assert [payload["virtual_machine"] for payload in submitted] == [7, 8]
    assert [payload["virtual_machine"] for payload in results] == [7, 8]
    assert (created, patched) == (2, 0)


@pytest.mark.asyncio
async def test_bulk_backup_reconcile_rejects_duplicate_existing_owner_keys(monkeypatch):
    monkeypatch.setattr(backups_vm, "clear_rest_get_cache_for_path", lambda *_args: None)

    async def _duplicate_existing(_nb, path, **_kwargs):
        payload = {
            "virtual_machine": {"id": 7},
            "volume_id": "pbs:backup/vm/101/shared",
        }
        return [
            RestRecord(SimpleNamespace(), path, {"id": 1, **payload}),
            RestRecord(SimpleNamespace(), path, {"id": 2, **payload}),
        ]

    monkeypatch.setattr(backups_vm, "rest_list_paginated_async", _duplicate_existing)

    with pytest.raises(ProxboxException, match="Duplicate NetBox backups") as exc_info:
        await _bulk_reconcile_backups(
            object(),
            [
                {
                    "virtual_machine": 7,
                    "volume_id": "pbs:backup/vm/101/shared",
                }
            ],
        )
    assert exc_info.value.http_status_code == 502


def _px(endpoint_id: int):
    return SimpleNamespace(
        db_endpoint_id=endpoint_id,
        session=SimpleNamespace(
            storage=SimpleNamespace(
                get=lambda: [{"storage": "pbs", "nodes": "all", "content": "backup"}]
            )
        ),
    )


def _cluster(name: str, *nodes: str):
    return SimpleNamespace(
        name=name,
        node_list=[SimpleNamespace(name=node) for node in nodes],
    )


def _vm_sidecar_scan(netbox_id: int, *, endpoint_id: int, cluster_name: str, vmid: int = 101):
    async def _scan(_nb):
        return SimpleNamespace(
            rows=(
                {
                    "virtual_machine": {"id": netbox_id},
                    "proxmox_cluster_name": cluster_name,
                    "proxmox_endpoint_raw_id": endpoint_id,
                    "proxmox_vm_id": vmid,
                    "proxmox_vm_type": "qemu",
                },
            ),
            sidecar_unavailable=False,
            sidecar_read_failed=False,
        )

    return _scan


@pytest.mark.asyncio
async def test_selected_backup_scope_never_queries_same_vmid_on_another_endpoint(monkeypatch):
    queries: list[dict] = []
    fetched_endpoints: list[int] = []
    reconciled_payloads: list[dict] = []

    async def _selected_vms(_nb, path, *, query=None):
        assert path == "/api/virtualization/virtual-machines/"
        queries.append(dict(query or {}))
        return [_netbox_vm(7, endpoint_id=1, cluster_name="cluster-a")]

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_filter.load_vm_sync_state_identities",
        _vm_sidecar_scan(7, endpoint_id=1, cluster_name="cluster-a"),
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_filter.custom_fields_enabled",
        lambda: False,
    )

    async def _empty_storage_index(_nb):
        return {}

    async def _get_backups(proxmox, **_kwargs):
        fetched_endpoints.append(proxmox.db_endpoint_id)
        return [
            {
                "content": "backup",
                "vmid": 101,
                "volid": "pbs:backup/vm/101/shared",
                "format": "pbs-vm",
                "subtype": "qemu",
            }
        ]

    async def _bulk(_nb, payloads, **_kwargs):
        reconciled_payloads.extend(payloads)
        return payloads, len(payloads), 0

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _selected_vms)
    monkeypatch.setattr(backups_vm, "_load_storage_index", _empty_storage_index)
    monkeypatch.setattr(backups_vm, "get_node_storage_content", _get_backups)
    monkeypatch.setattr(backups_vm, "_bulk_reconcile_backups", _bulk)

    await _create_all_virtual_machine_backups(
        netbox_session=object(),
        pxs=[_px(1), _px(2)],
        cluster_status=[_cluster("cluster-a", "pve-a"), _cluster("cluster-b", "pve-b")],
        tag=object(),
        netbox_vm_ids=[7],
    )

    assert queries == [{"id": ["7"]}]
    assert fetched_endpoints == [1]
    assert [payload["virtual_machine"] for payload in reconciled_payloads] == [7]


@pytest.mark.asyncio
async def test_selected_backup_scope_resolves_sidecar_only_identity_by_default(monkeypatch):
    fetched_endpoints: list[int] = []
    reconciled_payloads: list[dict] = []

    async def _selected_vms(_nb, path, *, query=None):
        assert path == "/api/virtualization/virtual-machines/"
        return [
            {
                "id": 7,
                "name": "vm-7",
                "cluster": None,
                "custom_fields": {},
            }
        ]

    async def _empty_storage_index(_nb):
        return {}

    async def _get_backups(proxmox, **_kwargs):
        fetched_endpoints.append(proxmox.db_endpoint_id)
        return [
            {
                "content": "backup",
                "vmid": 101,
                "volid": "pbs:backup/vm/101/shared",
                "format": "pbs-vm",
                "subtype": "qemu",
            }
        ]

    async def _bulk(_nb, payloads, **_kwargs):
        reconciled_payloads.extend(payloads)
        return payloads, len(payloads), 0

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_filter.load_vm_sync_state_identities",
        _vm_sidecar_scan(7, endpoint_id=1, cluster_name="cluster-a"),
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_filter.custom_fields_enabled",
        lambda: False,
    )
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _selected_vms)
    monkeypatch.setattr(backups_vm, "_load_storage_index", _empty_storage_index)
    monkeypatch.setattr(backups_vm, "get_node_storage_content", _get_backups)
    monkeypatch.setattr(backups_vm, "_bulk_reconcile_backups", _bulk)

    await _create_all_virtual_machine_backups(
        netbox_session=object(),
        pxs=[_px(1), _px(2)],
        cluster_status=[_cluster("cluster-a", "pve-a"), _cluster("cluster-b", "pve-b")],
        tag=object(),
        netbox_vm_ids=[7],
    )

    assert fetched_endpoints == [1]
    assert [payload["virtual_machine"] for payload in reconciled_payloads] == [7]


@pytest.mark.asyncio
async def test_partial_backup_discovery_never_runs_stale_deletion(monkeypatch):
    cleanup_calls = 0

    async def _vm_list(_nb, path, *, query=None):
        assert path == "/api/virtualization/virtual-machines/"
        return [_netbox_vm(7, endpoint_id=1, cluster_name="cluster-a")]

    async def _empty_storage_index(_nb):
        return {}

    async def _get_backups(_proxmox, *, node, **_kwargs):
        if node == "pve-bad":
            raise RuntimeError("storage unavailable")
        return [
            {
                "content": "backup",
                "vmid": 101,
                "volid": "pbs:backup/vm/101/present",
                "format": "pbs-vm",
                "subtype": "qemu",
            }
        ]

    async def _bulk(_nb, payloads, **_kwargs):
        return payloads, len(payloads), 0

    async def _unexpected_cleanup(*_args, **_kwargs):
        nonlocal cleanup_calls
        cleanup_calls += 1
        return []

    monkeypatch.setattr(backups_vm, "rest_list_async", _vm_list)
    monkeypatch.setattr(backups_vm, "_load_storage_index", _empty_storage_index)
    monkeypatch.setattr(backups_vm, "get_node_storage_content", _get_backups)
    monkeypatch.setattr(backups_vm, "_bulk_reconcile_backups", _bulk)
    monkeypatch.setattr(backups_vm, "rest_list_paginated_async", _unexpected_cleanup)

    await _create_all_virtual_machine_backups(
        netbox_session=object(),
        pxs=[_px(1)],
        cluster_status=[_cluster("cluster-a", "pve-good", "pve-bad")],
        tag=object(),
        delete_nonexistent_backup=True,
    )

    assert cleanup_calls == 0


@pytest.mark.asyncio
async def test_backup_stale_presence_is_scoped_by_virtual_machine_owner(monkeypatch):
    deleted_ids: list[int] = []
    vm_records = [
        _netbox_vm(7, endpoint_id=1, cluster_name="cluster-a"),
        _netbox_vm(8, endpoint_id=2, cluster_name="cluster-b"),
    ]

    async def _vm_list(_nb, path, *, query=None):
        assert path == "/api/virtualization/virtual-machines/"
        return vm_records

    async def _empty_storage_index(_nb):
        return {}

    async def _get_backups(proxmox, **_kwargs):
        if proxmox.db_endpoint_id == 2:
            return []
        return [
            {
                "content": "backup",
                "vmid": 101,
                "volid": "pbs:backup/vm/101/shared",
                "format": "pbs-vm",
                "subtype": "qemu",
            }
        ]

    async def _bulk(_nb, payloads, **_kwargs):
        return payloads, len(payloads), 0

    async def _existing(_nb, path, **_kwargs):
        return [
            RestRecord(
                SimpleNamespace(),
                path,
                {
                    "id": 70,
                    "virtual_machine": {"id": 7},
                    "volume_id": "pbs:backup/vm/101/shared",
                },
            ),
            RestRecord(
                SimpleNamespace(),
                path,
                {
                    "id": 80,
                    "virtual_machine": {"id": 8},
                    "volume_id": "pbs:backup/vm/101/shared",
                },
            ),
        ]

    async def _delete(_nb, _path, ids):
        deleted_ids.extend(ids)
        return len(ids)

    monkeypatch.setattr(backups_vm, "rest_list_async", _vm_list)
    monkeypatch.setattr(backups_vm, "_load_storage_index", _empty_storage_index)
    monkeypatch.setattr(backups_vm, "get_node_storage_content", _get_backups)
    monkeypatch.setattr(backups_vm, "_bulk_reconcile_backups", _bulk)
    monkeypatch.setattr(backups_vm, "rest_list_paginated_async", _existing)
    monkeypatch.setattr(backups_vm, "rest_bulk_delete_async", _delete)
    monkeypatch.setattr(backups_vm, "_resolve_bulk_batch_delay_ms", lambda: 0)

    await _create_all_virtual_machine_backups(
        netbox_session=object(),
        pxs=[_px(1), _px(2)],
        cluster_status=[_cluster("cluster-a", "pve-a"), _cluster("cluster-b", "pve-b")],
        tag=object(),
        delete_nonexistent_backup=True,
    )

    assert deleted_ids == [80]


@pytest.mark.asyncio
async def test_estate_backup_cleanup_deletes_after_complete_empty_discovery(monkeypatch):
    deleted_ids: list[int] = []

    async def _vm_list(_nb, path, *, query=None):
        assert path == "/api/virtualization/virtual-machines/"
        return [_netbox_vm(7, endpoint_id=1, cluster_name="cluster-a")]

    async def _empty_storage_index(_nb):
        return {}

    async def _empty_backups(*_args, **_kwargs):
        return []

    async def _existing(_nb, path, **_kwargs):
        return [
            RestRecord(
                SimpleNamespace(),
                path,
                {
                    "id": 70,
                    "virtual_machine": {"id": 7},
                    "volume_id": "pbs:backup/vm/101/stale",
                },
            )
        ]

    async def _delete(_nb, _path, ids):
        deleted_ids.extend(ids)
        return len(ids)

    async def _unexpected_bulk(*_args, **_kwargs):
        raise AssertionError("empty discovery must skip reconciliation")

    monkeypatch.setattr(backups_vm, "rest_list_async", _vm_list)
    monkeypatch.setattr(backups_vm, "_load_storage_index", _empty_storage_index)
    monkeypatch.setattr(backups_vm, "get_node_storage_content", _empty_backups)
    monkeypatch.setattr(backups_vm, "_bulk_reconcile_backups", _unexpected_bulk)
    monkeypatch.setattr(backups_vm, "rest_list_paginated_async", _existing)
    monkeypatch.setattr(backups_vm, "rest_bulk_delete_async", _delete)
    monkeypatch.setattr(backups_vm, "_resolve_bulk_batch_delay_ms", lambda: 0)

    result = await _create_all_virtual_machine_backups(
        netbox_session=object(),
        pxs=[_px(1)],
        cluster_status=[_cluster("cluster-a", "pve-a")],
        tag=object(),
        delete_nonexistent_backup=True,
    )

    assert result == []
    assert deleted_ids == [70]


@pytest.mark.asyncio
async def test_estate_backup_cleanup_suppresses_ambiguous_exact_vm_owners(monkeypatch):
    cleanup_lists = 0
    deleted_ids: list[int] = []
    messages: list[dict[str, object]] = []
    duplicate_owners = [
        _netbox_vm(7, endpoint_id=1, cluster_name="cluster-a", vmid=101),
        _netbox_vm(8, endpoint_id=1, cluster_name="cluster-a", vmid=101),
    ]

    async def _vm_list(_nb, path, *, query=None):
        assert path == "/api/virtualization/virtual-machines/"
        return duplicate_owners

    async def _empty_storage_index(_nb):
        return {}

    async def _discovered_backup(*_args, **_kwargs):
        return [
            {
                "content": "backup",
                "vmid": 101,
                "volid": "pbs:backup/vm/101/present",
                "format": "pbs-vm",
                "subtype": "qemu",
            }
        ]

    async def _cleanup(_nb, _path, **_kwargs):
        nonlocal cleanup_lists
        cleanup_lists += 1
        return []

    async def _delete(_nb, _path, ids):
        deleted_ids.extend(ids)
        return len(ids)

    class _Bridge:
        async def send_json(self, payload):
            messages.append(payload)

    monkeypatch.setattr(backups_vm, "rest_list_async", _vm_list)
    monkeypatch.setattr(backups_vm, "_load_storage_index", _empty_storage_index)
    monkeypatch.setattr(backups_vm, "get_node_storage_content", _discovered_backup)
    monkeypatch.setattr(backups_vm, "rest_list_paginated_async", _cleanup)
    monkeypatch.setattr(backups_vm, "rest_bulk_delete_async", _delete)

    result = await _create_all_virtual_machine_backups(
        netbox_session=object(),
        pxs=[_px(1)],
        cluster_status=[_cluster("cluster-a", "pve-a")],
        tag=object(),
        delete_nonexistent_backup=True,
        websocket=_Bridge(),
        use_websocket=True,
    )

    assert result == []
    assert cleanup_lists == 0
    assert deleted_ids == []
    completed = [message for message in messages if message.get("status") == "completed"]
    assert completed[-1]["result"]["failed_tasks"] == 1
