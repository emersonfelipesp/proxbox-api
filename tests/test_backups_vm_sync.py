"""Regression tests for VM backup synchronization."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from proxbox_api.routes.virtualization.virtual_machines.backups_vm import (
    _normalize_backup_format,
    _normalize_backup_subtype,
    create_netbox_backups,
    get_node_backups,
)


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
        return [{"id": 7, "name": "vm-101"}]

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
    assert reconciled[0][1]["proxmox_storage"] == 99
    assert reconciled[0][1]["storage"] == "local-zfs"
    assert journal_entries[0]["assigned_object_type"] == "netbox_proxbox.vmbackup"
    assert journal_entries[0]["assigned_object_id"] == 55


def test_create_netbox_backups_reuses_cached_vm_lookup(monkeypatch):
    queries: list[dict] = []

    async def _fake_rest_list_async(_nb, _path, *, query=None):
        queries.append(query or {})
        return [{"id": 7, "name": "vm-101"}]

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
