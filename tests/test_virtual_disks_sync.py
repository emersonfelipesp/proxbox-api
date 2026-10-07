"""Regression tests for virtual disk synchronization."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import RestRecord
from proxbox_api.routes.virtualization.virtual_machines.disks_vm import (
    create_virtual_disks as create_virtual_disks_route,
)
from proxbox_api.services.sync import virtual_disks as virtual_disks_module
from proxbox_api.services.sync.virtual_disks import create_virtual_disks


@pytest.fixture(autouse=True)
def bridge_virtual_disk_pagination(monkeypatch: pytest.MonkeyPatch) -> None:

    async def _inline_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)

    async def _typed_identity_bridge(_nb, vms, *, require_all, **_kwargs):
        del require_all
        return vms

    monkeypatch.setattr(
        virtual_disks_module,
        "hydrate_vm_identities_from_sidecars",
        _typed_identity_bridge,
    )

    async def _legacy_list_bridge(
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
        return await virtual_disks_module.rest_list_async(
            netbox_session,
            path,
            query=query,
        )

    # Existing workflow tests provide one path-aware REST list fake. Bridge the
    # migrated exhaustive VM snapshot call to that fake; pagination behavior is
    # covered independently by the netbox_rest contract tests.
    monkeypatch.setattr(
        virtual_disks_module,
        "rest_list_paginated_async",
        _legacy_list_bridge,
    )


def test_selected_virtual_disk_vm_lookup_uses_repeated_ids_and_rest_records(monkeypatch):
    queries: list[dict[str, object]] = []

    async def _selected_list(_nb, path, *, query=None):
        queries.append(dict(query or {}))
        return [RestRecord(SimpleNamespace(), path, {"id": 7})]

    async def _other_lists(*_args, **_kwargs):
        return []

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _selected_list)
    monkeypatch.setattr(
        "proxbox_api.services.sync.virtual_disks.rest_list_async",
        _other_lists,
    )

    result = asyncio.run(
        create_virtual_disks(
            netbox_session=object(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            tag=None,
            netbox_vm_ids=[7],
        )
    )

    assert queries == [{"id": ["7"]}]
    assert result == {"count": 0, "created": 0, "updated": 0, "skipped": 0}


def test_explicit_empty_virtual_disk_scope_fails_without_listing_all_vms(monkeypatch):
    selected_queries: list[dict[str, object]] = []

    async def _unexpected_selected_list(*_args, **_kwargs):
        selected_queries.append(dict(_kwargs.get("query") or {}))
        return []

    async def _other_lists(*_args, **_kwargs):
        return []

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _unexpected_selected_list)
    monkeypatch.setattr(
        "proxbox_api.services.sync.virtual_disks.rest_list_async",
        _other_lists,
    )

    with pytest.raises(ProxboxException, match="explicitly selected NetBox VMs") as exc_info:
        asyncio.run(
            create_virtual_disks(
                netbox_session=object(),
                pxs=[],
                cluster_status=[],
                cluster_resources=[],
                tag=None,
                netbox_vm_ids=[],
            )
        )

    assert selected_queries == []
    assert exc_info.value.http_status_code == 502
    assert "selection was empty" in str(exc_info.value.detail)


def test_partial_selected_virtual_disk_lookup_is_typed_failure(monkeypatch):
    async def _partial_selected_list(_nb, path, *, query=None):
        assert path == "/api/virtualization/virtual-machines/"
        assert query == {"id": ["7", "8"]}
        return [{"id": 7}]

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _partial_selected_list)

    with pytest.raises(ProxboxException, match="explicitly selected NetBox VMs") as exc_info:
        asyncio.run(
            create_virtual_disks(
                netbox_session=object(),
                pxs=[],
                cluster_status=[],
                cluster_resources=[],
                tag=None,
                netbox_vm_ids=[7, 8],
            )
        )

    assert exc_info.value.http_status_code == 502
    assert "missing id(s): [8]" in str(exc_info.value.detail)


@pytest.mark.parametrize("selector", ["", "bad", "1,bad"])
def test_virtual_disk_route_rejects_present_invalid_scope(selector):
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            create_virtual_disks_route(
                netbox_session=object(),
                pxs=[],
                cluster_status=[],
                cluster_resources=[],
                tag=None,
                netbox_vm_ids=selector,
            )
        )

    assert exc_info.value.status_code == 422


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
                    "proxmox_vm_id": 101,
                },
                {
                    "id": 8,
                    "name": "vm-102",
                    "cluster": {"name": "cluster-a"},
                    "proxmox_vm_id": 102,
                },
                {
                    "id": 9,
                    "name": "vm-103",
                    "cluster": {"name": "cluster-a"},
                    "proxmox_vm_id": 103,
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


def test_create_virtual_disks_uses_typed_sidecar_proxmox_vm_id(monkeypatch):
    calls = {"resolve_vm_config": []}
    reconciled_payloads: list[dict] = []

    async def _fake_rest_list(_nb, _path, query=None):
        if _path == "/api/virtualization/virtual-machines/":
            return [
                {
                    "id": 7,
                    "name": "vm-101",
                    "cluster": {"name": "cluster-a"},
                    "proxmox_vm_id": 101,
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
    assert reconciled_payloads[0]["storage"] == 42


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
                    "proxmox_endpoint_id": 1,
                    "proxmox_vm_id": 105,
                    "proxmox_vm_type": "qemu",
                    "proxmox_node": "pve",
                },
                {
                    "id": 8,
                    "name": "vm-105-b",
                    "cluster": {"name": "astro"},
                    "proxmox_endpoint_id": 2,
                    "proxmox_vm_id": 105,
                    "proxmox_vm_type": "qemu",
                    "proxmox_node": "astro",
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
            "proxmox_vm_id": 101,
            "proxmox_vm_type": "qemu",
            "proxmox_node": "stale-node",
        },
        cluster_resources=[
            {"cluster-a": [{"type": "qemu", "name": "vm-101", "vmid": 101, "node": "pve02"}]}
        ],
    )

    assert result == {"count": 1, "created": 1, "updated": 0, "skipped": 0}
    assert calls[0]["node"] == "pve02"
    assert calls[0]["vm_type"] == "qemu"


def test_create_virtual_disks_uses_sidecar_node_when_resource_missing(monkeypatch):
    result, calls = _run_virtual_disk_sync_for_vm(
        monkeypatch,
        vm={
            "id": 7,
            "name": "vm-101",
            "cluster": {"name": "cluster-a"},
            "proxmox_vm_id": 101,
            "proxmox_vm_type": "qemu",
            "proxmox_node": "pve03",
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
            "proxmox_vm_id": 101,
            "proxmox_vm_type": "qemu",
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
                    "proxmox_vm_id": 101,
                }
            ]
        if _path == "/api/plugins/proxbox/storage/":
            return []
        if _path == "/api/virtualization/virtual-disks/":
            assert query == {"virtual_machine_id": 7, "limit": 500}
            return [
                {
                    "id": 10,
                    "virtual_machine": {"id": 7},
                    "name": "scsi0",
                    "size": 2252,
                    "tags": [{"name": "Proxbox", "slug": "proxbox"}],
                },
                {
                    "id": 12,
                    "virtual_machine": {"id": 7},
                    "name": "scsi0",
                    "size": 2252,
                    "tags": [{"name": "Proxbox", "slug": "proxbox"}],
                },
                {
                    "id": 11,
                    "virtual_machine": {"id": 7},
                    "name": "efidisk0",
                    "size": 4,
                    "tags": [{"name": "Proxbox", "slug": "proxbox"}],
                },
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
                    "proxmox_vm_id": 124,
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
                    "proxmox_vm_id": 55,
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


def _run_disk_sync_with_vms(monkeypatch, *, vms, resolver):
    """Run the disk stage over ``vms`` with ``resolver`` standing in for the Proxmox fetch."""

    async def _fake_rest_list(_nb, path, query=None):
        return vms if path == "/api/virtualization/virtual-machines/" else []

    async def _fake_bulk_reconcile(_nb, _path, *, payloads, **kwargs):
        return SimpleNamespace(records=[], created=len(payloads), updated=0, unchanged=0, failed=0)

    monkeypatch.setattr(virtual_disks_module, "rest_list_async", _fake_rest_list)
    monkeypatch.setattr(virtual_disks_module, "resolve_vm_config", resolver)
    monkeypatch.setattr(virtual_disks_module, "rest_bulk_reconcile_async", _fake_bulk_reconcile)
    return asyncio.run(
        create_virtual_disks(
            netbox_session=object(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[
                {
                    "cluster-a": [
                        {"type": "qemu", "name": "vm-101", "vmid": "101", "node": "pve01"},
                        {"type": "qemu", "name": "vm-102", "vmid": "102", "node": "pve01"},
                    ]
                }
            ],
            tag=None,
            use_websocket=False,
            use_css=False,
        )
    )


def _disk_vm(record_id, vmid, **extra):
    return {
        "id": record_id,
        "name": f"vm-{vmid}",
        "cluster": {"name": "cluster-a"},
        "proxmox_vm_id": vmid,
        **extra,
    }


@pytest.mark.parametrize(
    "marker",
    [
        {"status": {"value": "decommissioning", "label": "Decommissioning"}},
        {"status": "decommissioning"},
        {"status": {"value": "active"}, "tags": [{"slug": "proxbox-soft-deleted"}]},
        {"tags": ["proxbox-soft-deleted"]},
    ],
)
def test_create_virtual_disks_skips_decommissioned_and_soft_deleted_vms(
    monkeypatch, proxbox_log_capture, marker
):
    resolved_vmids: list[object] = []

    async def _resolver(**kwargs):
        resolved_vmids.append(kwargs["vmid"])
        return {"scsi0": "local-lvm:vm-102-disk-0,size=1G"}

    result = _run_disk_sync_with_vms(
        monkeypatch,
        vms=[_disk_vm(7, 101, **marker), _disk_vm(8, 102, status={"value": "active"})],
        resolver=_resolver,
    )

    assert resolved_vmids == ["102"]
    assert result["count"] == 1
    assert (
        proxbox_log_capture.messages(logging.INFO).count(
            "Skipping 1 decommissioned or soft-deleted VM(s) during virtual disk sync"
        )
        == 1
    )


def test_create_virtual_disks_with_only_decommissioned_vms_never_fetches_configs(monkeypatch):
    async def _resolver(**_kwargs):
        raise AssertionError("decommissioned VMs must not reach Proxmox")

    result = _run_disk_sync_with_vms(
        monkeypatch,
        vms=[_disk_vm(7, 101, status={"value": "decommissioning"})],
        resolver=_resolver,
    )

    assert result == {"count": 0, "created": 0, "updated": 0, "skipped": 0}


def _missing_guest_error():
    return ProxboxException(
        message="VM Config not found.",
        detail=(
            "VM Config not found. Check if the 'node', 'type', and 'vmid' are correct. "
            "Session errors: pve: Configuration file 'nodes/pve01/qemu-server/101.conf' "
            "does not exist"
        ),
    )


def _config_failure_records(capture):
    return [r for r in capture.records if r.getMessage().startswith("Error getting VM config")]


def test_create_virtual_disks_logs_a_missing_guest_below_error_and_counts_it_skipped(
    monkeypatch, proxbox_log_capture
):
    async def _resolver(**_kwargs):
        raise _missing_guest_error()

    result = _run_disk_sync_with_vms(monkeypatch, vms=[_disk_vm(7, 101)], resolver=_resolver)

    records = _config_failure_records(proxbox_log_capture)
    assert [r.levelno for r in records] == [logging.WARNING]
    assert result["skipped"] == 1
    assert result["created"] == 0


def test_create_virtual_disks_keeps_error_level_for_other_config_failures(
    monkeypatch, proxbox_log_capture
):
    async def _resolver(**_kwargs):
        raise ProxboxException(
            message="VM Config not found.",
            detail="Session errors: pve: ClientConnectorError: connection refused",
        )

    result = _run_disk_sync_with_vms(monkeypatch, vms=[_disk_vm(7, 101)], resolver=_resolver)

    records = _config_failure_records(proxbox_log_capture)
    assert [r.levelno for r in records] == [logging.ERROR]
    assert result["skipped"] == 1


def test_missing_guest_failure_message_is_reported_to_the_stage(monkeypatch):
    async def _resolver(**_kwargs):
        raise _missing_guest_error()

    monkeypatch.setattr(virtual_disks_module, "resolve_vm_config", _resolver)

    fetched = asyncio.run(
        virtual_disks_module._fetch_virtual_disk_vm_config(
            vm={"id": 7, "name": "vm-101", "cluster": {"name": "cluster-a"}, "proxmox_vm_id": 101},
            pxs=[],
            cluster_status=[],
            cluster_resources=[
                {"cluster-a": [{"type": "qemu", "name": "vm-101", "vmid": "101", "node": "pve01"}]}
            ],
        )
    )

    assert fetched.failure_message == "VM not found in Proxmox"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_missing_guest_error(), True),
        (
            ProxboxException(
                message="VM Config not found.",
                detail="VM Config not found. Check if the 'node', 'type', and 'vmid' are correct.",
            ),
            True,
        ),
        (
            ProxboxException(
                message="Cannot reach node", detail="Configuration FILE DOES NOT EXIST"
            ),
            True,
        ),
        (RuntimeError("Configuration file 'x.conf' does not exist"), True),
        (
            ProxboxException(
                message="VM Config not found.",
                detail="Session errors: pve: TimeoutError: request timed out",
            ),
            False,
        ),
        (ProxboxException(message="Invalid VM Type. Use 'qemu' or 'lxc'."), False),
        (RuntimeError("connection reset by peer"), False),
        (ProxboxException(message="", detail=None), False),
    ],
)
def test_is_guest_not_found_error_classification(error, expected):
    from proxbox_api.services.proxmox.config import is_guest_not_found_error

    assert is_guest_not_found_error(error) is expected


# --- strict vs lenient selection of VMs with unusable ownership -----------------


def _selection_vm(netbox_id: int, vmid: int) -> dict[str, object]:
    return {"id": netbox_id, "name": f"vm-{netbox_id}", "cluster": {"name": "cluster-a"}}


def _disk_sidecar(netbox_id: int, vmid: int, *, endpoint_id: int | None = 1):
    row: dict[str, object] = {
        "virtual_machine": {"id": netbox_id},
        "proxmox_cluster_name": "cluster-a",
        "proxmox_vm_id": vmid,
        "proxmox_vm_type": "qemu",
    }
    if endpoint_id is not None:
        row["proxmox_endpoint_raw_id"] = endpoint_id
    return row


@pytest.fixture
def mixed_disk_selection(monkeypatch):
    """VM 7 is usable, VM 8 has no endpoint id, VM 9 has two sidecars."""

    from proxbox_api.services.sync import vm_filter

    monkeypatch.setattr(
        virtual_disks_module,
        "hydrate_vm_identities_from_sidecars",
        vm_filter.hydrate_vm_identities_from_sidecars,
    )
    vms = [_selection_vm(7, 101), _selection_vm(8, 102), _selection_vm(9, 103)]
    sidecars = [
        _disk_sidecar(7, 101),
        _disk_sidecar(8, 102, endpoint_id=None),
        _disk_sidecar(9, 103),
        _disk_sidecar(9, 103, endpoint_id=2),
    ]

    async def _lookup(_nb, path, *, query=None):
        requested = {int(vm_id) for vm_id in (query or {}).get("id", [])}
        return [vm for vm in vms if vm["id"] in requested]

    async def _scan(_nb):
        return SimpleNamespace(
            rows=tuple(sidecars), sidecar_unavailable=False, sidecar_read_failed=False
        )

    async def _no_records(*_args, **_kwargs):
        return []

    fetched: list[int] = []

    async def _fetch(*, vm, **_kwargs):
        fetched.append(vm["id"])
        return virtual_disks_module.VmDiskFetchResult(
            vm=vm,
            vmid=str(vm["proxmox_vm_id"]),
            vm_name=str(vm["name"]),
            cluster_name="cluster-a",
            target=None,
            vm_config={},
        )

    async def _sync(*, fetched_vm, **_kwargs):
        return virtual_disks_module.VmDiskSyncOutcome(state="created")

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _lookup)
    monkeypatch.setattr(vm_filter, "load_vm_sync_state_identities", _scan)
    monkeypatch.setattr(virtual_disks_module, "rest_list_async", _no_records)
    monkeypatch.setattr(virtual_disks_module, "_fetch_virtual_disk_vm_config", _fetch)
    monkeypatch.setattr(virtual_disks_module, "_sync_virtual_disks_for_vm", _sync)
    return fetched


def _run_disks(**kwargs):
    return asyncio.run(
        create_virtual_disks(
            netbox_session=object(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            tag=None,
            **kwargs,
        )
    )


def test_selected_virtual_disks_lenient_syncs_good_vm_and_reports_degraded(mixed_disk_selection):
    result = _run_disks(netbox_vm_ids=[7, 8, 9])

    assert mixed_disk_selection == [7]
    assert result["count"] == 1
    assert result["created"] == 1
    assert result["degraded"] is True
    assert sorted(w["netbox_vm_id"] for w in result["warnings"]) == [8, 9]


def test_selected_virtual_disks_lenient_with_every_vm_bad_is_degraded_not_error(
    mixed_disk_selection,
):
    result = _run_disks(netbox_vm_ids=[8, 9])

    assert mixed_disk_selection == []
    assert result["count"] == 0
    assert result["degraded"] is True
    assert sorted(w["netbox_vm_id"] for w in result["warnings"]) == [8, 9]


def _install_shared_disk_owner(monkeypatch, claimants):
    from proxbox_api.services.sync import vm_filter

    vms = [_selection_vm(7, 101)] + [_selection_vm(c, 102) for c in claimants]
    sidecars = [_disk_sidecar(7, 101)] + [_disk_sidecar(c, 102) for c in claimants]

    async def _lookup(_nb, path, *, query=None):
        requested = {int(vm_id) for vm_id in (query or {}).get("id", [])}
        return [vm for vm in vms if vm["id"] in requested]

    async def _scan(_nb):
        return SimpleNamespace(
            rows=tuple(sidecars), sidecar_unavailable=False, sidecar_read_failed=False
        )

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _lookup)
    monkeypatch.setattr(vm_filter, "load_vm_sync_state_identities", _scan)
    return [7, *claimants]


@pytest.mark.parametrize("claimants", [[8, 9], [8, 9, 10]])
def test_selected_virtual_disks_lenient_drops_every_shared_owner_claimant(
    monkeypatch, mixed_disk_selection, claimants
):
    ids = _install_shared_disk_owner(monkeypatch, claimants)

    result = _run_disks(netbox_vm_ids=ids)

    assert mixed_disk_selection == [7]
    assert result["count"] == 1
    assert result["degraded"] is True
    assert sorted(w["netbox_vm_id"] for w in result["warnings"]) == claimants


def test_selected_virtual_disks_strict_fails_before_any_write_on_a_shared_owner(
    monkeypatch, mixed_disk_selection
):
    from proxbox_api.services.sync.vm_filter import SelectionMode

    ids = _install_shared_disk_owner(monkeypatch, [8, 9])

    with pytest.raises(ProxboxException, match="claim the same Proxmox"):
        _run_disks(netbox_vm_ids=ids, selection_mode=SelectionMode.STRICT)

    assert mixed_disk_selection == []


def test_single_vm_virtual_disks_stays_strict_when_the_route_says_so(mixed_disk_selection):
    from proxbox_api.services.sync.vm_filter import SelectionMode

    with pytest.raises(ProxboxException, match="selected VM ownership"):
        _run_disks(netbox_vm_id=8, selection_mode=SelectionMode.STRICT)

    assert mixed_disk_selection == []


def test_virtual_disk_routes_choose_selection_mode_by_addressing(monkeypatch):
    from proxbox_api.routes.virtualization.virtual_machines import disks_vm
    from proxbox_api.services.sync.vm_filter import SelectionMode

    captured: list[dict[str, object]] = []

    async def _fake_sync(**kwargs):
        captured.append(kwargs)
        return {"count": 0, "created": 0, "updated": 0, "skipped": 0}

    async def _get(id):
        return {"id": id}

    monkeypatch.setattr(disks_vm, "sync_virtual_disks", _fake_sync)
    common = {
        "pxs": [],
        "cluster_status": [],
        "cluster_resources": [],
        "tag": SimpleNamespace(id=1),
        "fetch_max_concurrency": None,
    }

    async def _drain(response):
        async for _chunk in response.body_iterator:
            pass

    async def _drive():
        session = SimpleNamespace(
            virtualization=SimpleNamespace(virtual_machines=SimpleNamespace(get=_get))
        )
        await disks_vm.create_virtual_disks(
            netbox_session=session, netbox_vm_ids="5,6", websocket=None, **common
        )
        await _drain(
            await disks_vm.create_virtual_disks_stream(
                netbox_session=session, netbox_vm_ids="5,6", **common
            )
        )
        await _drain(
            await disks_vm.create_virtual_disks_for_vm_stream(
                netbox_vm_id=5, netbox_session=session, **common
            )
        )

    asyncio.run(_drive())

    assert [call["selection_mode"] for call in captured] == [
        SelectionMode.LENIENT,
        SelectionMode.LENIENT,
        SelectionMode.STRICT,
    ]
