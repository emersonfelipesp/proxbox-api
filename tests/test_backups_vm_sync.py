"""Regression tests for VM backup synchronization."""

from __future__ import annotations

import asyncio

from proxbox_api.routes.virtualization.virtual_machines.backups_vm import create_netbox_backups


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

    monkeypatch.setattr("proxbox_api.routes.virtualization.virtual_machines.backups_vm.rest_list_async", _fake_rest_list_async)
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
    assert reconciled[0][1]["storage"] == 99
    assert journal_entries[0]["assigned_object_type"] == "netbox_proxbox.vmbackup"
    assert journal_entries[0]["assigned_object_id"] == 55
