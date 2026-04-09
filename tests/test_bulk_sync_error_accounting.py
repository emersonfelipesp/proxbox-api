"""Regression tests for bulk sync error accounting."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from proxbox_api.services.sync.backup_routines import sync_all_backup_routines
from proxbox_api.services.sync.replications import sync_all_replications


def test_backup_routines_includes_bulk_failed_count_in_errors(monkeypatch):
    async def _fake_list_async(_nb, _path, **_kwargs):
        return [{"id": 1, "name": "lab"}]

    async def _fake_bulk_reconcile(*_args, **_kwargs):
        return SimpleNamespace(created=3, updated=4, unchanged=0, failed=2, records=[])

    monkeypatch.setattr(
        "proxbox_api.services.sync.backup_routines.rest_list_async",
        _fake_list_async,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.backup_routines.rest_bulk_reconcile_async",
        _fake_bulk_reconcile,
    )

    pxs = [
        SimpleNamespace(
            name="lab",
            session=SimpleNamespace(
                cluster=SimpleNamespace(
                    backup=SimpleNamespace(
                        get=lambda: [
                            {
                                "id": "job-1",
                                "enabled": True,
                                "schedule": "daily",
                            }
                        ]
                    )
                )
            ),
        )
    ]

    result = asyncio.run(sync_all_backup_routines(netbox_session=object(), pxs=pxs))

    assert result == {"created": 3, "updated": 4, "stale": 0, "errors": 2}


def test_replications_includes_bulk_failed_count_in_errors(monkeypatch):
    async def _fake_list_async(_nb, _path, **_kwargs):
        return [{"id": 55, "custom_fields": {"proxmox_vm_id": "101"}}]

    async def _fake_bulk_reconcile(*_args, **_kwargs):
        return SimpleNamespace(created=1, updated=0, unchanged=0, failed=1, records=[])

    monkeypatch.setattr(
        "proxbox_api.services.sync.replications.rest_list_async",
        _fake_list_async,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.replications.rest_bulk_reconcile_async",
        _fake_bulk_reconcile,
    )

    pxs = [
        SimpleNamespace(
            name="lab",
            session=SimpleNamespace(
                cluster=SimpleNamespace(
                    replication=SimpleNamespace(
                        get=lambda: [
                            {
                                "id": "rep-1",
                                "guest": 101,
                                "target": "pve02",
                                "schedule": "*/15",
                            }
                        ]
                    )
                )
            ),
        )
    ]

    result = asyncio.run(sync_all_replications(netbox_session=object(), pxs=pxs))

    assert result == {"created": 1, "updated": 0, "stale": 0, "errors": 1}
