"""Unit tests for VM backup discovery helpers (Proxmox volid → stale-delete set)."""

from __future__ import annotations

import asyncio

from proxbox_api.routes.virtualization.virtual_machines import (
    _volids_from_proxmox_storage_backup_items,
)


def test_volids_from_proxmox_storage_backup_items_collects_backup_content_only():
    items = [
        {"content": "backup", "volid": "pbs:vm/100/2024-01-01T00:00:00Z"},
        {"content": "iso", "volid": "local:iso/x.iso"},
        {"content": "backup", "volid": "local:backup/vzdump-qemu-101-2024_01_01-00_00_00.vma.zst"},
        {"content": "backup", "volid": ""},
        {"content": "backup"},
    ]
    volids = _volids_from_proxmox_storage_backup_items(items)
    assert volids == {
        "pbs:vm/100/2024-01-01T00:00:00Z",
        "local:backup/vzdump-qemu-101-2024_01_01-00_00_00.vma.zst",
    }


def test_process_backups_batch_counts_failures():
    from proxbox_api.routes.virtualization import virtual_machines as vm_mod

    async def ok():
        return {"id": 1}

    async def boom():
        raise RuntimeError("netbox down")

    results, failures = asyncio.run(
        vm_mod.process_backups_batch([ok(), ok(), boom(), ok()], batch_size=10)
    )
    assert len(results) == 3
    assert failures == 1
