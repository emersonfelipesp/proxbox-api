from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from proxbox_api.netbox_rest import RestRecord
from proxbox_api.routes.virtualization.virtual_machines import backups_vm
from proxbox_api.routes.virtualization.virtual_machines.backups_vm import (
    _create_all_virtual_machine_backups,
)

from .test_backups_vm_sync import (
    _allow_dict_proxmox_rows,  # noqa: F401
    _cluster,
    _netbox_vm,
)


def _px(storages):
    return SimpleNamespace(
        db_endpoint_id=1,
        session=SimpleNamespace(storage=SimpleNamespace(get=lambda: storages)),
    )


def _row(path, **fields):
    return RestRecord(SimpleNamespace(), path, fields)


async def _run(monkeypatch, storages, *, nodes=("pve1",), content_fn=None, existing=None):
    deleted: list[int] = []

    async def _vm_list(_nb, path, *, query=None):
        return [_netbox_vm(7, endpoint_id=1, cluster_name="cluster-a")]

    async def _storage_index(_nb):
        return {}

    async def _empty(*_a, **_k):
        return []

    async def _existing(_nb, path, **_k):
        rows = existing
        if rows is None:
            rows = [
                {"id": 70, "virtual_machine": {"id": 7}, "volume_id": "pbs:backup/vm/101/stale"}
            ]
        return [_row(path, **r) for r in rows]

    async def _delete(_nb, _path, ids):
        deleted.extend(ids)
        return len(ids)

    monkeypatch.setattr(backups_vm, "rest_list_async", _vm_list)
    monkeypatch.setattr(backups_vm, "_load_storage_index", _storage_index)
    monkeypatch.setattr(backups_vm, "get_node_storage_content", content_fn or _empty)
    monkeypatch.setattr(backups_vm, "rest_list_paginated_async", _existing)
    monkeypatch.setattr(backups_vm, "rest_bulk_delete_async", _delete)
    monkeypatch.setattr(backups_vm, "_resolve_bulk_batch_delay_ms", lambda: 0)
    await _create_all_virtual_machine_backups(
        netbox_session=object(),
        pxs=[_px(storages)],
        cluster_status=[_cluster("cluster-a", *nodes)],
        tag=object(),
        delete_nonexistent_backup=True,
    )
    return deleted


GOOD = {"storage": "pbs", "nodes": "all", "content": "backup,iso"}


@pytest.mark.asyncio
async def test_base_complete_empty_discovery_still_deletes(monkeypatch):
    assert await _run(monkeypatch, [GOOD]) == [70]


@pytest.mark.asyncio
async def test_exact_node_list_match_deletes(monkeypatch):
    storages = [{**GOOD, "nodes": "pve2, pve1"}]
    assert await _run(monkeypatch, storages) == [70]


@pytest.mark.asyncio
async def test_node_substring_does_not_match(monkeypatch):
    storages = [{**GOOD, "nodes": "pve10,pve2"}]
    assert await _run(monkeypatch, storages) == []


def test_storage_serves_node_helpers():
    assert backups_vm._storage_serves_node("all", "pve1")
    assert backups_vm._storage_serves_node(["pve1"], "pve1")
    assert not backups_vm._storage_serves_node("pve10,pve2", "pve1")
    assert backups_vm._split_csv(None) == []


@pytest.mark.asyncio
async def test_storage_with_missing_content_suppresses_owner_wide_cleanup(monkeypatch):
    """An omitted storage might hold backups, so even a good storage must not authorize deletes."""
    odd = {"storage": "odd", "nodes": "all"}
    assert await _run(monkeypatch, [odd, GOOD]) == []
    assert await _run(monkeypatch, [GOOD, odd]) == []
    assert await _run(monkeypatch, [odd]) == []


@pytest.mark.asyncio
async def test_storage_with_malformed_content_suppresses_cleanup(monkeypatch):
    assert await _run(monkeypatch, [{"storage": "odd", "nodes": "all", "content": 5}, GOOD]) == []


@pytest.mark.asyncio
async def test_unread_node_restricted_storage_suppresses_owner_wide_cleanup(monkeypatch):
    """A backup storage restricted to nodes we never read must block cleanup for the owner."""
    restricted = {"storage": "far", "nodes": "pve10,pve2", "content": "backup"}
    assert await _run(monkeypatch, [GOOD, restricted]) == []
    assert await _run(monkeypatch, [restricted, GOOD]) == []


@pytest.mark.asyncio
async def test_node_restricted_storage_that_is_read_still_allows_cleanup(monkeypatch):
    local = {"storage": "near", "nodes": "pve1", "content": "backup"}
    assert await _run(monkeypatch, [GOOD, local]) == [70]


STALE_VOLID = "pbs:backup/vm/101/stale"


async def _content(_proxmox, **_kwargs):
    return _content.rows


@pytest.mark.asyncio
async def test_unclassifiable_rows_still_protect_their_records(monkeypatch):
    """A listed volume with no vmid, or no content field, must never be deleted from NetBox."""
    for rows in (
        [{"volid": STALE_VOLID, "content": "backup"}],  # no vmid: dropped by classification
        [{"volid": STALE_VOLID, "vmid": 101}],  # no content field: dropped by the content filter
    ):
        _content.rows = rows
        assert await _run(monkeypatch, [GOOD], content_fn=_content) == []


@pytest.mark.asyncio
async def test_unrelated_listed_volume_does_not_protect_a_stale_record(monkeypatch):
    _content.rows = [{"volid": "pbs:backup/vm/999/other", "content": "backup"}]
    assert await _run(monkeypatch, [GOOD], content_fn=_content) == [70]


def test_all_volids_includes_rows_without_content_or_vmid():
    rows = [{"volid": "a"}, {"volid": "b", "content": "backup"}, {"vmid": 1}, {"volid": ""}]
    assert backups_vm._all_volids(rows) == {"a", "b"}


@pytest.mark.asyncio
async def test_known_non_backup_storage_does_not_block_cleanup(monkeypatch):
    """Valid content that simply lacks 'backup' is fully known, so cleanup may proceed."""
    images = {"storage": "local-lvm", "nodes": "all", "content": "images,rootdir"}
    assert await _run(monkeypatch, [images, GOOD]) == [70]


@pytest.mark.asyncio
async def test_content_type_match_is_exact(monkeypatch):
    assert await _run(monkeypatch, [{**GOOD, "content": "iso,backups"}]) == []


@pytest.mark.asyncio
async def test_owner_with_zero_backup_storages_deletes_nothing(monkeypatch):
    assert await _run(monkeypatch, []) == []
    assert await _run(monkeypatch, [{"storage": "local", "nodes": "all", "content": "iso"}]) == []


@pytest.mark.asyncio
async def test_failed_storage_read_deletes_nothing(monkeypatch):
    async def _boom(*_a, **_k):
        raise RuntimeError("storage unreachable")

    assert await _run(monkeypatch, [GOOD], content_fn=_boom) == []


@pytest.mark.asyncio
async def test_one_failed_read_blocks_deletion_even_if_another_succeeds(monkeypatch):
    async def _flaky(proxmox, *, node, storage, **_k):
        if storage == "bad":
            raise RuntimeError("boom")
        return []

    storages = [GOOD, {"storage": "bad", "nodes": "all", "content": "backup"}]
    assert await _run(monkeypatch, storages, content_fn=_flaky) == []


@pytest.mark.asyncio
async def test_existing_backup_rows_without_ownership_are_skipped(monkeypatch):
    existing = [
        {"id": 71, "virtual_machine": {"id": 7}, "volume_id": ""},
        {"id": 72, "virtual_machine": None, "volume_id": "pbs:backup/vm/101/x"},
    ]

    async def _list(_nb, path, **_k):
        return [_row(path, **r) for r in existing]

    created: list[dict] = []

    async def _create(_nb, _path, batch):
        created.extend(batch)
        return []

    monkeypatch.setattr(backups_vm, "rest_list_paginated_async", _list)
    monkeypatch.setattr(backups_vm, "rest_bulk_create_async", _create)
    monkeypatch.setattr(backups_vm, "clear_rest_get_cache_for_path", lambda *_a, **_k: None)
    monkeypatch.setattr(backups_vm, "_resolve_bulk_batch_delay_ms", lambda: 0)
    proxbox_logger = logging.getLogger("proxbox")
    handler_log = []
    handler = logging.Handler()
    handler.emit = handler_log.append  # type: ignore[method-assign]
    proxbox_logger.addHandler(handler)
    try:
        payload = {"virtual_machine": 7, "volume_id": "pbs:backup/vm/101/new", "vmid": 101}
        # An unowned existing row must not abort reconcile; a valid payload is still processed.
        monkeypatch.setattr(
            backups_vm.NetBoxBackupSyncState,
            "model_validate",
            classmethod(lambda cls, p: SimpleNamespace(model_dump=lambda **_k: dict(p))),
        )
        results, created_count, _patched = await backups_vm._bulk_reconcile_backups(
            object(), [payload]
        )
    finally:
        proxbox_logger.removeHandler(handler)
    assert created_count == 0 or created_count == len(created)
    assert created == [payload]
    messages = [r.getMessage() for r in handler_log if "without stable ownership" in r.getMessage()]
    assert any("id=71" in m for m in messages)
    assert any("id=72" in m for m in messages)
