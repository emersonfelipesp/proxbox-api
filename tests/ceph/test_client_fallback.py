"""Tests for proxbox-api's internal Ceph SDK fallback facade."""

from __future__ import annotations

from proxbox_api.ceph.client import CephClient


class _FakeResource:
    def __init__(self, path: str, payloads: dict[str, object], calls: list[str]) -> None:
        self.path = path
        self._payloads = payloads
        self._calls = calls

    async def get(self, **_params: object) -> object:
        self._calls.append(self.path)
        return self._payloads[self.path]


class _FakeSDK:
    def __init__(self, payloads: dict[str, object]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def __call__(self, path: str | list[str | int]) -> _FakeResource:
        normalized = path if isinstance(path, str) else "/".join(str(part) for part in path)
        return _FakeResource(normalized, self.payloads, self.calls)


async def test_internal_ceph_client_reads_cluster_status_with_attribute_access():
    sdk = _FakeSDK(
        {
            "cluster/ceph/status": {
                "data": {
                    "health": {"status": "HEALTH_OK"},
                    "fsid": "fsid-1",
                }
            }
        }
    )

    status = await CephClient.from_sdk(sdk).status()

    assert status.health.status == "HEALTH_OK"
    assert status.fsid == "fsid-1"
    assert sdk.calls == ["cluster/ceph/status"]


async def test_internal_ceph_client_maps_sync_resources_to_pve_paths():
    sdk = _FakeSDK(
        {
            "cluster/ceph/metadata": {"data": {"mon": {"a": {}}}},
            "cluster/ceph/flags": {"data": {"noup": True}},
            "nodes/pve1/ceph/mon": {"data": [{"name": "mon.pve1", "type": None}]},
            "nodes/pve1/ceph/mgr": {"data": [{"name": "mgr.pve1", "type": "wrong"}]},
            "nodes/pve1/ceph/mds": {"data": []},
            "nodes/pve1/ceph/osd": {"data": [{"id": 0}]},
            "nodes/pve1/ceph/pool": {"data": [{"name": "rbd"}]},
            "nodes/pve1/ceph/fs": {"data": [{"name": "cephfs"}]},
            "nodes/pve1/ceph/crush": {"data": {"types": [], "nodes": []}},
            "nodes/pve1/ceph/rules": {"data": [{"name": "replicated_rule"}]},
        }
    )
    client = CephClient.from_sdk(sdk)

    assert await client.cluster.metadata()
    assert len(await client.cluster.flags()) == 1
    assert (await client.nodes.monitors("pve1"))[0].type == "mon"
    assert (await client.nodes.managers("pve1"))[0].type == "mgr"
    assert len(await client.nodes.metadata_servers("pve1")) == 0
    assert len(await client.nodes.osds("pve1")) == 1
    assert len(await client.nodes.pools("pve1")) == 1
    assert len(await client.nodes.filesystems("pve1")) == 1
    assert await client.nodes.crush("pve1")
    assert len(await client.nodes.rules("pve1")) == 1

    assert sdk.calls == [
        "cluster/ceph/metadata",
        "cluster/ceph/flags",
        "nodes/pve1/ceph/mon",
        "nodes/pve1/ceph/mgr",
        "nodes/pve1/ceph/mds",
        "nodes/pve1/ceph/osd",
        "nodes/pve1/ceph/pool",
        "nodes/pve1/ceph/fs",
        "nodes/pve1/ceph/crush",
        "nodes/pve1/ceph/rules",
    ]
