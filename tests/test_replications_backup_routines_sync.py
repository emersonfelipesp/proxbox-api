"""Regression tests for replication and backup-routine bulk sync services."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from proxbox_api.services.sync.backup_routines import (
    _mark_stale_routines,
    sync_all_backup_routines,
)
from proxbox_api.services.sync.replications import (
    _mark_stale_replications,
    sync_all_replications,
)


def test_mark_stale_replications_marks_only_missing_endpoint_records(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_list_paginated(_nb, _path, query=None, **_kwargs):
        captured["query"] = query
        return [
            {"id": 1, "replication_id": "rep-1"},
            {"id": 2, "replication_id": "rep-2"},
        ]

    async def _fake_bulk_patch(_nb, _path, updates=None, **_kwargs):
        captured["updates"] = updates

    monkeypatch.setattr(
        "proxbox_api.services.sync.replications.rest_list_paginated_async",
        _fake_list_paginated,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.replications.rest_bulk_patch_async",
        _fake_bulk_patch,
    )

    count = asyncio.run(
        _mark_stale_replications(
            object(),
            synced_replication_ids={"rep-1"},
            endpoint_id=9,
        )
    )

    assert count == 1
    assert captured["query"] == {"status": "active", "endpoint": 9}
    assert captured["updates"] == [{"id": 2, "status": "stale"}]


def test_sync_all_replications_reports_reconcile_and_stale_counts(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_get_endpoint_id(_nb, _px):
        return 9

    async def _fake_list_async(_nb, _path, **_kwargs):
        if _path == "/api/virtualization/virtual-machines/":
            return [{"id": 55, "custom_fields": {"proxmox_vm_id": "101"}}]
        if _path == "/api/plugins/proxbox/nodes/":
            return [{"id": 77, "name": "pve02"}]
        raise AssertionError(f"Unexpected rest_list_async path: {_path}")

    async def _fake_bulk_reconcile(_nb, _path, payloads, **kwargs):
        captured["path"] = _path
        captured["lookup_fields"] = kwargs.get("lookup_fields")
        captured["payloads"] = list(payloads)
        return SimpleNamespace(created=2, updated=3, unchanged=0, failed=1, records=[])

    async def _fake_mark_stale(_nb, synced_replication_ids, endpoint_id):
        captured["stale_synced_ids"] = synced_replication_ids
        captured["stale_endpoint_id"] = endpoint_id
        return 4

    monkeypatch.setattr(
        "proxbox_api.services.sync.replications._get_netbox_endpoint_id",
        _fake_get_endpoint_id,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.replications.rest_list_async",
        _fake_list_async,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.replications.rest_bulk_reconcile_async",
        _fake_bulk_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.replications._mark_stale_replications",
        _fake_mark_stale,
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
                                "type": "local",
                            }
                        ]
                    )
                )
            ),
        )
    ]

    result = asyncio.run(sync_all_replications(netbox_session=object(), pxs=pxs))

    assert result == {"created": 2, "updated": 3, "stale": 4, "errors": 1}
    assert captured["path"] == "/api/plugins/proxbox/replications/"
    assert captured["lookup_fields"] == ["replication_id", "endpoint"]
    assert captured["payloads"] == [
        {
            "replication_id": "rep-1",
            "endpoint": 9,
            "guest": 101,
            "target": "pve02",
            "job_type": "local",
            "schedule": "*/15",
            "rate": None,
            "comment": None,
            "disable": None,
            "source": None,
            "jobnum": None,
            "remove_job": None,
            "virtual_machine": 55,
            "proxmox_node": 77,
            "raw_config": {
                "id": "rep-1",
                "guest": 101,
                "target": "pve02",
                "schedule": "*/15",
                "type": "local",
            },
            "status": "active",
            "tags": [],
        }
    ]
    assert captured["stale_synced_ids"] == {"rep-1"}
    assert captured["stale_endpoint_id"] == 9


def test_mark_stale_backup_routines_marks_only_non_synced_active_records(monkeypatch):
    captured: dict[str, object] = {}

    class _Record:
        def __init__(self, payload):
            self._payload = payload

        def serialize(self):
            return self._payload

    async def _fake_list_paginated(_nb, _path, **_kwargs):
        return [
            _Record(
                {
                    "id": 1,
                    "endpoint": {"id": 9},
                    "job_id": "job-1",
                    "status": {"value": "active"},
                }
            ),
            _Record(
                {
                    "id": 2,
                    "endpoint": {"id": 9},
                    "job_id": "job-2",
                    "status": {"value": "active"},
                }
            ),
            _Record(
                {
                    "id": 3,
                    "endpoint": {"id": 9},
                    "job_id": "job-3",
                    "status": {"value": "stale"},
                }
            ),
        ]

    async def _fake_bulk_patch(_nb, _path, updates, **_kwargs):
        captured["updates"] = updates

    monkeypatch.setattr(
        "proxbox_api.services.sync.backup_routines.rest_list_paginated_async",
        _fake_list_paginated,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.backup_routines.rest_bulk_patch_async",
        _fake_bulk_patch,
    )

    stale = asyncio.run(
        _mark_stale_routines(
            object(),
            synced_payloads=[{"endpoint": 9, "job_id": "job-1"}],
        )
    )

    assert stale == 1
    assert captured["updates"] == [{"id": 2, "status": "stale"}]


def test_sync_all_backup_routines_reports_reconcile_and_stale_counts(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_get_endpoint_id(_nb, _px):
        return 9

    async def _fake_bulk_reconcile(_nb, _path, payloads, **kwargs):
        captured["path"] = _path
        captured["lookup_fields"] = kwargs.get("lookup_fields")
        captured["payloads"] = list(payloads)
        return SimpleNamespace(created=1, updated=2, unchanged=0, failed=0, records=[])

    async def _fake_mark_stale(_nb, synced_payloads):
        captured["stale_payloads"] = synced_payloads
        return 5

    monkeypatch.setattr(
        "proxbox_api.services.sync.backup_routines._get_netbox_endpoint_id",
        _fake_get_endpoint_id,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.backup_routines.rest_bulk_reconcile_async",
        _fake_bulk_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.backup_routines._mark_stale_routines",
        _fake_mark_stale,
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
                                "vmid": "101,102",
                            }
                        ]
                    )
                )
            ),
        )
    ]

    result = asyncio.run(sync_all_backup_routines(netbox_session=object(), pxs=pxs))

    assert result == {"created": 1, "updated": 2, "stale": 5, "errors": 0}
    assert captured["path"] == "/api/plugins/proxbox/backup-routines/"
    assert captured["lookup_fields"] == ["job_id", "endpoint"]
    assert captured["payloads"] == [
        {
            "job_id": "job-1",
            "endpoint": 9,
            "enabled": True,
            "schedule": "daily",
            "node": None,
            "storage": None,
            "fleecing_storage": None,
            "selection": [101, 102],
            "keep_last": None,
            "keep_daily": None,
            "keep_weekly": None,
            "keep_monthly": None,
            "keep_yearly": None,
            "keep_all": None,
            "bwlimit": None,
            "zstd": None,
            "io_workers": None,
            "fleecing": None,
            "repeat_missed": None,
            "pbs_change_detection_mode": None,
            "raw_config": {
                "id": "job-1",
                "enabled": True,
                "schedule": "daily",
                "vmid": "101,102",
            },
            "status": "active",
        }
    ]
    assert captured["stale_payloads"] == captured["payloads"]

