"""Regression tests for virtual disk synchronization."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from proxbox_api.services.sync.virtual_disks import create_virtual_disks


@pytest.fixture(autouse=True)
def enable_legacy_custom_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "proxbox_api.services.custom_fields.get_plugin_bool",
        lambda *, settings_key, default=False: (
            True if settings_key == "custom_fields_enabled" else default
        ),
    )


def _run_virtual_disk_sync_for_vm(monkeypatch, *, vm, cluster_resources):
    calls = {"resolve_vm_config": []}

    async def _fake_rest_list(_nb, _path, query=None):
        if _path == "/api/virtualization/virtual-machines/":
            return [vm]
        if _path == "/api/plugins/proxbox/storage/":
            return []
        if _path == "/api/virtualization/virtual-disks/":
            return []
        return []

    async def _fake_resolve_vm_config(**kwargs):
        calls["resolve_vm_config"].append(kwargs)
        return {"scsi0": "local-lvm:vm-101-disk-0,size=1G"}

    async def _fake_bulk_reconcile(_nb, _path, *, payloads, **kwargs):
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
            cluster_resources=cluster_resources,
            tag=None,
            use_websocket=False,
            use_css=False,
        )
    )
    return result, calls["resolve_vm_config"]


def _virtual_disk_normalizer(record):
    return {
        "virtual_machine": record.get("virtual_machine"),
        "name": record.get("name"),
        "size": record.get("size") if record.get("size") is not None else 0,
        "storage": record.get("storage"),
        "description": record.get("description"),
        "tags": record.get("tags"),
        "custom_fields": record.get("custom_fields"),
    }


def _make_virtual_disk_record(
    *,
    record_id=10,
    vm_id=7,
    name="scsi0",
    size=1024,
    storage_id=None,
    with_save=False,
):
    record = MagicMock()
    record.id = record_id
    if with_save:
        record.save = AsyncMock()
    record.serialize.return_value = {
        "virtual_machine": {"id": vm_id},
        "name": name,
        "size": size,
        "storage": {"id": storage_id} if storage_id is not None else None,
        "description": "",
        "tags": [],
        "custom_fields": {},
    }
    return record


def test_create_virtual_disks_fetches_vm_configs_with_bounded_concurrency(monkeypatch):
    active_fetches = 0
    max_active_fetches = 0
    two_fetches_started = asyncio.Event()
    release_fetches = asyncio.Event()

    async def _fake_rest_list(_nb, _path, query=None):
        if _path == "/api/virtualization/virtual-machines/":
            return [
                {
                    "id": 7,
                    "name": "vm-101",
                    "cluster": {"name": "cluster-a"},
                    "custom_fields": {"proxmox_vm_id": 101},
                },
                {
                    "id": 8,
                    "name": "vm-102",
                    "cluster": {"name": "cluster-a"},
                    "custom_fields": {"proxmox_vm_id": 102},
                },
                {
                    "id": 9,
                    "name": "vm-103",
                    "cluster": {"name": "cluster-a"},
                    "custom_fields": {"proxmox_vm_id": 103},
                },
            ]
        if _path == "/api/plugins/proxbox/storage/":
            return []
        if _path == "/api/virtualization/virtual-disks/":
            return []
        return []

    async def _fake_resolve_vm_config(**kwargs):
        nonlocal active_fetches, max_active_fetches
        active_fetches += 1
        max_active_fetches = max(max_active_fetches, active_fetches)
        if active_fetches >= 2:
            two_fetches_started.set()
        await release_fetches.wait()
        active_fetches -= 1
        return {"scsi0": f"local-lvm:vm-{kwargs['vmid']}-disk-0,size=1G"}

    async def _fake_bulk_reconcile(_nb, _path, *, payloads, **kwargs):
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

    async def _run():
        sync_task = asyncio.create_task(
            create_virtual_disks(
                netbox_session=object(),
                pxs=[],
                cluster_status=[],
                cluster_resources=[
                    {
                        "cluster-a": [
                            {"type": "qemu", "name": "vm-101", "vmid": "101", "node": "pve01"},
                            {"type": "qemu", "name": "vm-102", "vmid": "102", "node": "pve01"},
                            {"type": "qemu", "name": "vm-103", "vmid": "103", "node": "pve01"},
                        ]
                    }
                ],
                tag=None,
                use_websocket=False,
                use_css=False,
                fetch_max_concurrency=2,
            )
        )
        await asyncio.wait_for(two_fetches_started.wait(), timeout=1)
        release_fetches.set()
        result = await sync_task
        return result

    result = asyncio.run(_run())

    assert max_active_fetches == 2
    assert result == {"count": 3, "created": 3, "updated": 0, "skipped": 0}


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


def test_create_virtual_disks_scopes_config_fetch_by_endpoint(monkeypatch):
    calls = {"resolve_vm_config": []}
    endpoint_a = SimpleNamespace(db_endpoint_id=1, name="pve")
    endpoint_b = SimpleNamespace(db_endpoint_id=2, name="astro")

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

    async def _fake_resolve_vm_config(**kwargs):
        calls["resolve_vm_config"].append(kwargs)
        return {"scsi0": f"local-lvm:vm-{kwargs['vmid']}-disk-0,size=1G"}

    async def _fake_bulk_reconcile(_nb, _path, *, payloads, **kwargs):
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
            pxs=[endpoint_a, endpoint_b],
            cluster_status=[],
            cluster_resources=[
                {"pve": [{"type": "qemu", "name": "vm-105-a", "vmid": "105", "node": "pve"}]},
                {"astro": [{"type": "qemu", "name": "vm-105-b", "vmid": "105", "node": "astro"}]},
            ],
            tag=None,
            use_websocket=False,
            use_css=False,
        )
    )

    assert result == {"count": 2, "created": 2, "updated": 0, "skipped": 0}
    assert [call["pxs"] for call in calls["resolve_vm_config"]] == [[endpoint_a], [endpoint_b]]
    assert [call["node"] for call in calls["resolve_vm_config"]] == ["pve", "astro"]


def test_create_virtual_disks_prefers_cluster_resource_node_over_vm_device(monkeypatch):
    result, calls = _run_virtual_disk_sync_for_vm(
        monkeypatch,
        vm={
            "id": 7,
            "name": "vm-101",
            "cluster": {"name": "cluster-a"},
            "device": {"name": "pve01.example.com"},
            "custom_fields": {
                "proxmox_vm_id": 101,
                "proxmox_vm_type": "qemu",
                "proxmox_node": "stale-node",
            },
        },
        cluster_resources=[
            {"cluster-a": [{"type": "qemu", "name": "vm-101", "vmid": 101, "node": "pve02"}]}
        ],
    )

    assert result == {"count": 1, "created": 1, "updated": 0, "skipped": 0}
    assert calls[0]["node"] == "pve02"
    assert calls[0]["vm_type"] == "qemu"


def test_create_virtual_disks_uses_proxmox_node_custom_field_when_resource_missing(monkeypatch):
    result, calls = _run_virtual_disk_sync_for_vm(
        monkeypatch,
        vm={
            "id": 7,
            "name": "vm-101",
            "cluster": {"name": "cluster-a"},
            "custom_fields": {
                "proxmox_vm_id": 101,
                "proxmox_vm_type": "qemu",
                "proxmox_node": "pve03",
            },
        },
        cluster_resources=[],
    )

    assert result == {"count": 1, "created": 1, "updated": 0, "skipped": 0}
    assert calls[0]["node"] == "pve03"


def test_create_virtual_disks_uses_device_name_as_last_resort(monkeypatch):
    result, calls = _run_virtual_disk_sync_for_vm(
        monkeypatch,
        vm={
            "id": 7,
            "name": "vm-101",
            "cluster": {"name": "cluster-a"},
            "device": {"name": "pve04"},
            "custom_fields": {"proxmox_vm_id": 101, "proxmox_vm_type": "qemu"},
        },
        cluster_resources=[],
    )

    assert result == {"count": 1, "created": 1, "updated": 0, "skipped": 0}
    assert calls[0]["node"] == "pve04"


def test_create_virtual_disks_deletes_stale_disks_and_updates_vm_total(monkeypatch):
    deleted_ids: list[int] = []
    parent_vm_patches: list[dict[str, object]] = []

    async def _fake_rest_list(_nb, _path, query=None):
        if _path == "/api/virtualization/virtual-machines/":
            return [
                {
                    "id": 7,
                    "name": "vm-101",
                    "disk": 2256,
                    "cluster": {"name": "cluster-a"},
                    "custom_fields": {"proxmox_vm_id": 101},
                }
            ]
        if _path == "/api/plugins/proxbox/storage/":
            return []
        if _path == "/api/virtualization/virtual-disks/":
            assert query == {"virtual_machine_id": 7, "limit": 500}
            return [
                {"id": 10, "virtual_machine": {"id": 7}, "name": "scsi0", "size": 2252},
                {"id": 12, "virtual_machine": {"id": 7}, "name": "scsi0", "size": 2252},
                {"id": 11, "virtual_machine": {"id": 7}, "name": "efidisk0", "size": 4},
            ]
        return []

    async def _fake_resolve_vm_config(**kwargs):
        return {"scsi0": "local-lvm:vm-101-disk-0,size=2252M"}

    async def _fake_bulk_reconcile(_nb, _path, *, payloads, **kwargs):
        assert payloads == [
            {
                "virtual_machine": 7,
                "name": "scsi0",
                "size": 2252,
                "storage": None,
                "description": "Storage: local-lvm",
                "tags": [],
            }
        ]
        return SimpleNamespace(records=[], created=0, updated=0, unchanged=1, failed=0)

    async def _fake_bulk_delete(_nb, _path, ids):
        deleted_ids.extend(ids)
        return len(ids)

    async def _fake_patch(_nb, _path, record_id, payload):
        parent_vm_patches.append({"record_id": record_id, **payload})
        return {"id": record_id, **payload}

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
    monkeypatch.setattr(
        "proxbox_api.services.sync.virtual_disks.rest_bulk_delete_async",
        _fake_bulk_delete,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.virtual_disks.rest_patch_async",
        _fake_patch,
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

    assert deleted_ids == [11, 12]
    assert parent_vm_patches == [{"record_id": 7, "disk": 2252}]
    assert result == {"count": 1, "created": 0, "updated": 1, "skipped": 0}


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
    from proxbox_api.netbox_rest import rest_bulk_reconcile_async
    from proxbox_api.proxmox_to_netbox.models import NetBoxVirtualDiskSyncState

    existing_record = _make_virtual_disk_record(name="ide0", size=None)

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
            current_normalizer=_virtual_disk_normalizer,
            base_query={"virtual_machine_id": 7},
            lookup_query_field_map={"virtual_machine": "virtual_machine_id"},
            strict_lookup=True,
        )
    )

    assert patched == [], "no PATCH should be issued when existing size=NULL matches desired size=0"
    assert result.unchanged == 1
    assert result.created == 0
    assert result.updated == 0


def test_single_reconcile_nullable_field_keeps_matching_storage(monkeypatch):
    """A nullable FK must not be cleared when desired and current values match."""
    from proxbox_api.netbox_rest import rest_reconcile_async_with_status
    from proxbox_api.proxmox_to_netbox.models import NetBoxVirtualDiskSyncState

    existing_record = _make_virtual_disk_record(storage_id=11, with_save=True)

    async def _fake_first(_nb, _path, *, query):
        return existing_record

    async def _fake_create(*_args, **_kwargs):
        raise AssertionError("create should not be called for an existing disk")

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_first_async", _fake_first)
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_create_async", _fake_create)

    result = asyncio.run(
        rest_reconcile_async_with_status(
            object(),
            "/api/virtualization/virtual-disks/",
            lookup={"virtual_machine": 7, "name": "scsi0"},
            payload={
                "virtual_machine": 7,
                "name": "scsi0",
                "size": 1024,
                "storage": 11,
                "description": "",
                "tags": [],
                "custom_fields": {},
            },
            schema=NetBoxVirtualDiskSyncState,
            current_normalizer=_virtual_disk_normalizer,
            strict_lookup=True,
            nullable_fields={"storage"},
        )
    )

    assert result.status == "unchanged"
    existing_record.save.assert_not_awaited()


def test_bulk_reconcile_nullable_field_keeps_matching_storage(monkeypatch):
    from proxbox_api.netbox_rest import rest_bulk_reconcile_async
    from proxbox_api.proxmox_to_netbox.models import NetBoxVirtualDiskSyncState

    existing_record = _make_virtual_disk_record(storage_id=11)
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

    result = asyncio.run(
        rest_bulk_reconcile_async(
            object(),
            "/api/virtualization/virtual-disks/",
            payloads=[
                {
                    "virtual_machine": 7,
                    "name": "scsi0",
                    "size": 1024,
                    "storage": 11,
                    "description": "",
                    "tags": [],
                    "custom_fields": {},
                }
            ],
            lookup_fields=["virtual_machine", "name"],
            schema=NetBoxVirtualDiskSyncState,
            current_normalizer=_virtual_disk_normalizer,
            base_query={"virtual_machine_id": 7},
            lookup_query_field_map={"virtual_machine": "virtual_machine_id"},
            strict_lookup=True,
            nullable_fields={"storage"},
        )
    )

    assert patched == []
    assert result.unchanged == 1
    assert result.created == 0
    assert result.updated == 0


def test_bulk_create_fallback_forwards_nullable_fields(monkeypatch):
    from proxbox_api.netbox_rest import rest_bulk_reconcile_async
    from proxbox_api.proxmox_to_netbox.models import NetBoxVirtualDiskSyncState

    captured_kwargs: list[dict] = []

    async def _fake_list_paginated(_nb, _path, *, base_query=None, **kwargs):
        return []

    async def _fake_bulk_create(_nb, _path, entries):
        raise RuntimeError("duplicate")

    async def _fake_reconcile_with_status(*_args, **kwargs):
        captured_kwargs.append(kwargs)
        return SimpleNamespace(record=SimpleNamespace(id=10), status="unchanged")

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_paginated_async", _fake_list_paginated)
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_bulk_create_async", _fake_bulk_create)
    monkeypatch.setattr(
        "proxbox_api.netbox_rest.rest_reconcile_async_with_status",
        _fake_reconcile_with_status,
    )

    result = asyncio.run(
        rest_bulk_reconcile_async(
            object(),
            "/api/virtualization/virtual-disks/",
            payloads=[
                {
                    "virtual_machine": 7,
                    "name": "scsi0",
                    "size": 1024,
                    "storage": None,
                    "description": "",
                    "tags": [],
                    "custom_fields": {},
                }
            ],
            lookup_fields=["virtual_machine", "name"],
            schema=NetBoxVirtualDiskSyncState,
            current_normalizer=_virtual_disk_normalizer,
            base_query={"virtual_machine_id": 7},
            lookup_query_field_map={"virtual_machine": "virtual_machine_id"},
            strict_lookup=True,
            nullable_fields={"storage"},
        )
    )

    assert captured_kwargs[0]["nullable_fields"] == {"storage"}
    assert result.unchanged == 1
    assert result.created == 0
    assert result.updated == 0
