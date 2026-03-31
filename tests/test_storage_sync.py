from __future__ import annotations

import asyncio
from types import SimpleNamespace

from proxbox_api.routes.virtualization.virtual_machines.storages_vm import (
    create_storages_stream,
)
from proxbox_api.services.sync.storages import create_storages


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

    tag = SimpleNamespace(id=1, name="Proxbox", slug="proxbox", color="ff5722")
    pxs = [SimpleNamespace(name="cluster-a")]

    asyncio.run(create_storages(netbox_session=object(), pxs=pxs, tag=tag))
    asyncio.run(create_storages(netbox_session=object(), pxs=pxs, tag=tag))

    assert reconciled[0][0] == {"cluster": "cluster-a", "name": "local"}
    assert reconciled[0][1]["enabled"] is True
    assert reconciled[1][0] == {"cluster": "cluster-a", "name": "local"}
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


async def _collect_async_frames(stream) -> list[str]:
    output: list[str] = []
    async for frame in stream:
        output.append(frame)
    return output
