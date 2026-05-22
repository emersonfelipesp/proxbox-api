"""Regression tests for virtual disk synchronization."""

import asyncio
from types import SimpleNamespace

from proxbox_api.services.sync.virtual_disks import create_virtual_disks


def test_create_virtual_disks_uses_custom_fields_proxmox_vm_id(monkeypatch):
    calls = {"resolve_vm_config": []}
    reconciled_payloads: list[dict] = []

    async def _fake_rest_list(_nb, _path, query=None):
        if _path == "/api/virtualization/virtual-machines/":
            return [
                {
                    "id": 7,
                    "name": "vm-101",
                    "cluster": {"name": "cluster-a"},
                    "custom_fields": {"proxmox_vm_id": 101},
                }
            ]
        if _path == "/api/plugins/proxbox/storage/":
            return [
                {
                    "id": 42,
                    "cluster": {"name": "cluster-a"},
                    "name": "local-lvm",
                    "backups": [],
                }
            ]
        return []

    async def _fake_resolve_vm_config(**kwargs):
        calls["resolve_vm_config"].append(kwargs)
        return {"scsi0": "local-lvm:vm-101-disk-0,size=20G"}

    async def _fake_bulk_reconcile(_nb, _path, *, payloads, **kwargs):
        reconciled_payloads.extend(payloads)
        return SimpleNamespace(records=[], created=len(payloads), updated=0, unchanged=0, failed=0)

    monkeypatch.setattr(
        "proxbox_api.services.sync.virtual_disks.rest_list_async",
        _fake_rest_list,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.virtual_disks.resolve_vm_config",
        _fake_resolve_vm_config,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.virtual_disks.rest_bulk_reconcile_async",
        _fake_bulk_reconcile,
    )

    result = asyncio.run(
        create_virtual_disks(
            netbox_session=object(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[
                {"cluster-a": [{"type": "qemu", "name": "vm-101", "vmid": "101", "node": "pve01"}]}
            ],
            tag=None,
            use_websocket=False,
            use_css=False,
        )
    )

    assert result == {"count": 1, "created": 1, "updated": 0, "skipped": 0}
    assert calls["resolve_vm_config"] == [
        {
            "pxs": [],
            "node": "pve01",
            "vm_type": "qemu",
            "vmid": "101",
        }
    ]
    assert len(reconciled_payloads) == 1
    assert reconciled_payloads[0]["virtual_machine"] == 7
    assert reconciled_payloads[0]["name"] == "scsi0"
    assert reconciled_payloads[0].get("custom_fields", {}).get("proxbox_storage_id") == 42


def test_cdrom_disk_is_included_with_size_zero(monkeypatch):
    """CD-ROM drives (size=None) must appear in the reconcile payloads with size=0.

    Regression test for GH#157 / GH#145: ide0 with media=cdrom has no size
    field.  Previously the entry was skipped or sent with size=None, causing
    NetBox to reject with 'size: This field is required.'  The fix uses
    ProxmoxDiskEntry.size_mb which returns 0 for null-size entries, so CD-ROM
    drives are created in NetBox with size=0 (valid for PositiveIntegerField).
    """
    reconciled_payloads: list[dict] = []
    bulk_reconcile_kwargs: list[dict] = []

    async def _fake_rest_list(_nb, _path, query=None):
        if _path == "/api/virtualization/virtual-machines/":
            return [
                {
                    "id": 38,
                    "name": "vm-cdrom",
                    "cluster": {"name": "cluster-a"},
                    "custom_fields": {"proxmox_vm_id": 124},
                }
            ]
        if _path == "/api/plugins/proxbox/storage/":
            return []
        return []

    async def _fake_resolve_vm_config(**kwargs):
        # VM config has a regular disk (scsi0) and a CD-ROM drive (ide0).
        return {
            "scsi0": "local-lvm:vm-124-disk-0,size=32G",
            "ide0": "none,media=cdrom",
        }

    async def _fake_bulk_reconcile(_nb, _path, *, payloads, **kwargs):
        reconciled_payloads.extend(payloads)
        bulk_reconcile_kwargs.append(kwargs)
        return SimpleNamespace(records=[], created=len(payloads), updated=0, unchanged=0, failed=0)

    monkeypatch.setattr(
        "proxbox_api.services.sync.virtual_disks.rest_list_async",
        _fake_rest_list,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.virtual_disks.resolve_vm_config",
        _fake_resolve_vm_config,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.virtual_disks.rest_bulk_reconcile_async",
        _fake_bulk_reconcile,
    )

    result = asyncio.run(
        create_virtual_disks(
            netbox_session=object(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[
                {
                    "cluster-a": [
                        {"type": "qemu", "name": "vm-cdrom", "vmid": "124", "node": "pve01"}
                    ]
                }
            ],
            tag=None,
            use_websocket=False,
            use_css=False,
        )
    )

    # Both disks must be in the payloads: scsi0 with real size, ide0 (CD-ROM) with size=0.
    assert len(reconciled_payloads) == 2
    names = {p["name"]: p["size"] for p in reconciled_payloads}
    assert names["scsi0"] == 32 * 1024  # 32 GiB in MiB
    assert names["ide0"] == 0  # CD-ROM → size_mb returns 0
    assert result["count"] == 1
    assert result["created"] == 1

    # lookup_query_field_map must be forwarded so the fallback GET query uses
    # virtual_machine_id instead of virtual_machine (GH#157 bug 2).
    assert bulk_reconcile_kwargs[0].get("lookup_query_field_map") == {
        "virtual_machine": "virtual_machine_id"
    }


def test_all_cdrom_vm_synced_as_zero_size(monkeypatch):
    """A VM with only CD-ROM drives still creates disk entries in NetBox (size=0)."""
    reconciled_payloads: list[dict] = []

    async def _fake_rest_list(_nb, _path, query=None):
        if _path == "/api/virtualization/virtual-machines/":
            return [
                {
                    "id": 55,
                    "name": "vm-nodata",
                    "cluster": {"name": "cluster-b"},
                    "custom_fields": {"proxmox_vm_id": 55},
                }
            ]
        if _path == "/api/plugins/proxbox/storage/":
            return []
        return []

    async def _fake_resolve_vm_config(**kwargs):
        return {"ide0": "none,media=cdrom", "ide2": "local:iso/ubuntu.iso,media=cdrom"}

    async def _fake_bulk_reconcile(_nb, _path, *, payloads, **kwargs):
        reconciled_payloads.extend(payloads)
        return SimpleNamespace(records=[], created=len(payloads), updated=0, unchanged=0, failed=0)

    monkeypatch.setattr(
        "proxbox_api.services.sync.virtual_disks.rest_list_async",
        _fake_rest_list,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.virtual_disks.resolve_vm_config",
        _fake_resolve_vm_config,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.virtual_disks.rest_bulk_reconcile_async",
        _fake_bulk_reconcile,
    )

    result = asyncio.run(
        create_virtual_disks(
            netbox_session=object(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[
                {
                    "cluster-b": [
                        {"type": "qemu", "name": "vm-nodata", "vmid": "55", "node": "pve01"}
                    ]
                }
            ],
            tag=None,
            use_websocket=False,
            use_css=False,
        )
    )

    # Both CD-ROM drives must be synced to NetBox with size=0.
    assert len(reconciled_payloads) == 2
    assert all(p["size"] == 0 for p in reconciled_payloads)
    assert result["count"] == 1
    assert result["created"] == 1


def test_cdrom_no_patch_storm_when_existing_has_null_size(monkeypatch):
    """Re-syncing a CD-ROM disk must not generate a spurious PATCH when the
    existing NetBox record has size=NULL.

    Without the normalizer fix, comparing desired size=0 against current size=None
    triggers a PATCH on every sync run. The normalizer must return 0 for None
    so the comparison sees no diff.
    """
    from unittest.mock import MagicMock

    from proxbox_api.netbox_rest import rest_bulk_reconcile_async
    from proxbox_api.proxmox_to_netbox.models import NetBoxVirtualDiskSyncState

    existing_record = MagicMock()
    existing_record.id = 10
    existing_record.serialize.return_value = {
        "virtual_machine": {"id": 7},
        "name": "ide0",
        "size": None,
        "storage": None,
        "description": "",
        "tags": [],
        "custom_fields": {},
    }

    patched: list = []

    async def _fake_list_paginated(_nb, _path, *, base_query=None, **kwargs):
        return [existing_record]

    async def _fake_bulk_create(_nb, _path, entries):
        return []

    async def _fake_bulk_patch(_nb, _path, entries):
        patched.extend(entries)
        return []

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_paginated_async", _fake_list_paginated)
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_bulk_create_async", _fake_bulk_create)
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_bulk_patch_async", _fake_bulk_patch)

    import asyncio

    result = asyncio.run(
        rest_bulk_reconcile_async(
            object(),
            "/api/virtualization/virtual-disks/",
            payloads=[
                {
                    "virtual_machine": 7,
                    "name": "ide0",
                    "size": 0,
                    "storage": None,
                    "description": "",
                    "tags": [],
                    "custom_fields": {},
                }
            ],
            lookup_fields=["virtual_machine", "name"],
            schema=NetBoxVirtualDiskSyncState,
            current_normalizer=lambda record: {
                "virtual_machine": record.get("virtual_machine"),
                "name": record.get("name"),
                "size": record.get("size") if record.get("size") is not None else 0,
                "storage": record.get("storage"),
                "description": record.get("description"),
                "tags": record.get("tags"),
                "custom_fields": record.get("custom_fields"),
            },
            base_query={"virtual_machine_id": 7},
            lookup_query_field_map={"virtual_machine": "virtual_machine_id"},
            strict_lookup=True,
        )
    )

    assert patched == [], "no PATCH should be issued when existing size=NULL matches desired size=0"
    assert result.unchanged == 1
    assert result.created == 0
    assert result.updated == 0
