"""Tests for storage sync orchestration and payload mapping."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from proxbox_api.proxmox_to_netbox.models import NetBoxStorageSyncState
from proxbox_api.routes.virtualization.virtual_machines.storages_vm import (
    create_storages_stream,
)
from proxbox_api.services.sync.storages import create_storages
from proxbox_api.utils.streaming import WebSocketSSEBridge


def test_create_storages_reconciles_and_updates_enabled_flag(monkeypatch):
    class _Record:
        def __init__(self, payload):
            self.payload = payload

        def serialize(self):
            return {"id": 1, **self.payload}

    storage_payloads = [
        [
            {
                "storage": "local",
                "type": "dir",
                "content": "images,rootdir,backup",
                "path": "/var/lib/vz",
                "nodes": "all",
                "shared": 0,
                "disable": 0,
            }
        ],
        [
            {
                "storage": "local",
                "type": "dir",
                "content": "images,rootdir,backup",
                "path": "/var/lib/vz",
                "nodes": "all",
                "shared": 0,
                "disable": 1,
            }
        ],
    ]
    reconciled: list[tuple[dict, dict]] = []

    def _fake_get_storage_list(_px):
        return storage_payloads.pop(0)

    async def _fake_reconcile(_nb, _path, lookup, payload, **kwargs):
        reconciled.append((lookup, payload))
        return _Record(payload)

    async def _fake_list_clusters(*args, **kwargs):
        return [{"id": 42, "name": "cluster-a"}]

    monkeypatch.setattr(
        "proxbox_api.services.sync.storages.get_storage_list",
        _fake_get_storage_list,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.storages.dump_models",
        lambda items: items,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.storages.rest_reconcile_async",
        _fake_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.storages.rest_list_async",
        _fake_list_clusters,
    )

    tag = SimpleNamespace(id=1, name="Proxbox", slug="proxbox", color="ff5722")
    pxs = [SimpleNamespace(name="cluster-a")]

    asyncio.run(create_storages(netbox_session=object(), pxs=pxs, tag=tag))
    asyncio.run(create_storages(netbox_session=object(), pxs=pxs, tag=tag))

    assert reconciled[0][0] == {"cluster": 42, "name": "local"}
    assert reconciled[0][1]["enabled"] is True
    assert reconciled[1][0] == {"cluster": 42, "name": "local"}
    assert reconciled[1][1]["enabled"] is False


def test_create_storages_stream_emits_complete_event(monkeypatch):
    class _StreamingResponseStub:
        def __init__(self, content, media_type=None, headers=None):
            self.content = content
            self.media_type = media_type
            self.headers = headers or {}

    async def _fake_sync_storages(**kwargs):
        return [{"id": 1, "name": "local"}, {"id": 2, "name": "nfs01"}]

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.storages_vm.StreamingResponse",
        _StreamingResponseStub,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.storages_vm.sync_storages",
        _fake_sync_storages,
    )

    response = asyncio.run(
        create_storages_stream(
            netbox_session=object(),
            pxs=[],
            tag=SimpleNamespace(id=1),
        )
    )
    payload = "".join(asyncio.run(_collect_async_frames(response.content)))
    assert "event: complete" in payload
    assert "Storage sync completed." in payload
    assert '"count": 2' in payload


def test_create_storages_deduplicates_cluster_storage_pairs(monkeypatch):
    class _Record:
        def __init__(self, payload):
            self.payload = payload

        def serialize(self):
            return {"id": 1, **self.payload}

    storages = [
        {"storage": "local-zfs", "type": "zfspool", "shared": 0, "disable": 0},
        {"storage": "local-zfs", "type": "zfspool", "shared": 0, "disable": 0},
    ]
    calls: list[tuple[dict, dict]] = []

    def _fake_get_storage_list(_px):
        return storages

    async def _fake_reconcile(_nb, _path, lookup, payload, **kwargs):
        calls.append((lookup, payload))
        return _Record(payload)

    async def _fake_list_clusters(*args, **kwargs):
        return [{"id": 99, "name": "TEST-CLUSTER"}]

    monkeypatch.setattr(
        "proxbox_api.services.sync.storages.get_storage_list", _fake_get_storage_list
    )
    monkeypatch.setattr("proxbox_api.services.sync.storages.dump_models", lambda items: items)
    monkeypatch.setattr("proxbox_api.services.sync.storages.rest_reconcile_async", _fake_reconcile)
    monkeypatch.setattr(
        "proxbox_api.services.sync.storages.rest_list_async",
        _fake_list_clusters,
    )

    tag = SimpleNamespace(id=1, name="Proxbox", slug="proxbox", color="ff5722")
    pxs = [SimpleNamespace(name="TEST-CLUSTER"), SimpleNamespace(name="TEST-CLUSTER")]

    asyncio.run(create_storages(netbox_session=object(), pxs=pxs, tag=tag))

    assert len(calls) == 1
    assert calls[0][0] == {"cluster": 99, "name": "local-zfs"}


def test_storage_state_normalizes_backups_relation():
    payload = {
        "cluster": 42,
        "name": "local-zfs",
        "backups": [{"id": 31}, {"id": "32"}, 31],
    }

    state = NetBoxStorageSyncState.model_validate(payload)

    assert state.cluster == 42
    assert state.backups == [31, 32]


def test_create_storages_bridge_emits_detailed_events(monkeypatch):
    class _Record:
        def __init__(self, payload):
            self.payload = payload

        def serialize(self):
            return {"id": 11, "url": "/api/plugins/proxbox/storage/11/", **self.payload}

    storages = [
        {
            "storage": "local-zfs",
            "type": "zfspool",
            "content": "images,rootdir",
            "path": "/tank",
            "nodes": "pve01",
            "shared": 0,
            "disable": 0,
        }
    ]

    def _fake_get_storage_list(_px):
        return storages

    async def _fake_reconcile(_nb, _path, lookup, payload, **kwargs):
        return _Record(payload)

    async def _fake_list_clusters(*args, **kwargs):
        return [{"id": 99, "name": "TEST-CLUSTER"}]

    monkeypatch.setattr(
        "proxbox_api.services.sync.storages.get_storage_list", _fake_get_storage_list
    )
    monkeypatch.setattr("proxbox_api.services.sync.storages.dump_models", lambda items: items)
    monkeypatch.setattr("proxbox_api.services.sync.storages.rest_reconcile_async", _fake_reconcile)
    monkeypatch.setattr("proxbox_api.services.sync.storages.rest_list_async", _fake_list_clusters)

    tag = SimpleNamespace(id=1, name="Proxbox", slug="proxbox", color="ff5722")
    pxs = [SimpleNamespace(name="TEST-CLUSTER")]
    bridge = WebSocketSSEBridge()

    async def _run_and_collect():
        sync_task = asyncio.create_task(
            create_storages(
                netbox_session=object(),
                pxs=pxs,
                tag=tag,
                websocket=bridge,
                use_websocket=True,
            )
        )
        frames: list[str] = []
        while not sync_task.done():
            try:
                item = await asyncio.wait_for(bridge._queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            if item is None:
                break
            event, data = item
            frames.append(f"{event}:{data.get('event', '')}:{data.get('message', '')}")
        while not bridge._queue.empty():
            item = await bridge._queue.get()
            if item is None:
                break
            event, data = item
            frames.append(f"{event}:{data.get('event', '')}:{data.get('message', '')}")
        await sync_task
        return frames

    frames = asyncio.run(_run_and_collect())

    assert any(frame.startswith("discovery:discovery:") for frame in frames)
    assert any(frame.startswith("item_progress:item_progress:Synced storage") for frame in frames)
    assert any(frame.startswith("phase_summary:phase_summary:") for frame in frames)


async def _collect_async_frames(stream) -> list[str]:
    output: list[str] = []
    async for frame in stream:
        output.append(frame)
    return output
