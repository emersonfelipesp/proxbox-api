"""Smoke tests for the read-only ``/ceph/*`` route helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from proxbox_api.ceph import routes as ceph_routes


class _FakeCluster:
    async def status(self):
        return SimpleNamespace(health={"status": "HEALTH_OK"}, fsid="fsid-1")

    async def metadata(self):
        return {"mon": {"a": {}}, "osd": {"0": {}}}

    async def flags(self):
        return [object(), object()]


class _FakeNodes:
    async def monitors(self, _node):
        return [object()]

    async def managers(self, _node):
        return [object()]

    async def metadata_servers(self, _node):
        return []

    async def osds(self, _node):
        return [object(), object()]

    async def pools(self, _node):
        return [object(), object(), object()]

    async def filesystems(self, _node):
        return [object()]

    async def crush(self, _node):
        return {"types": [], "nodes": []}

    async def rules(self, _node):
        return [object()]


class _FakeCephClient:
    def __init__(self):
        self.cluster = _FakeCluster()
        self.nodes = _FakeNodes()

    async def status(self):
        return await self.cluster.status()


def _fake_session(name: str = "pve-cluster"):
    return SimpleNamespace(
        name=name,
        cluster_name=name,
        node_name=None,
        domain="pve.example.local",
        ip_address="10.0.0.10",
        http_port=8006,
        cluster_status=[
            {"type": "node", "name": "pve1"},
            {"type": "node", "name": "pve2"},
        ],
        session=object(),
    )


@pytest.fixture
def _patched_client(monkeypatch):
    calls: list[str] = []

    def _client_for(px):
        calls.append(px.name)
        return _FakeCephClient()

    monkeypatch.setattr(ceph_routes, "_client_for", _client_for)
    return calls


async def test_ceph_status_reports_reachable(_patched_client):
    response = await ceph_routes.ceph_status([_fake_session()])
    assert len(response.items) == 1
    item = response.items[0]
    assert item.reachable is True
    assert item.health == {"status": "HEALTH_OK"}
    assert item.fsid == "fsid-1"


async def test_ceph_sync_threads_branch_query_into_summary(_patched_client):
    response = await ceph_routes.ceph_sync_pools(
        [_fake_session()],
        netbox_branch_schema_id="br_abc123",
    )
    item = response.items[0]
    assert item.resource == "pools"
    assert item.fetched == 6
    assert item.nodes == ["pve1", "pve2"]
    assert item.netbox_branch_schema_id == "br_abc123"


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        (ceph_routes.ceph_sync_status, 2),
        (ceph_routes.ceph_sync_daemons, 4),
        (ceph_routes.ceph_sync_osds, 4),
        (ceph_routes.ceph_sync_pools, 6),
        (ceph_routes.ceph_sync_filesystems, 2),
        (ceph_routes.ceph_sync_crush, 4),
        (ceph_routes.ceph_sync_flags, 2),
        (ceph_routes.ceph_sync_full, 24),
    ],
)
async def test_ceph_sync_routes_summary(_patched_client, handler, expected):
    response = await handler([_fake_session()])
    item = response.items[0]
    assert item.errors == []
    assert item.fetched == expected


async def test_ceph_sync_records_client_errors(monkeypatch):
    class _BrokenCluster(_FakeCluster):
        async def flags(self):
            raise RuntimeError("connection refused")

    class _BrokenClient(_FakeCephClient):
        def __init__(self):
            super().__init__()
            self.cluster = _BrokenCluster()

    monkeypatch.setattr(ceph_routes, "_client_for", lambda _px: _BrokenClient())

    response = await ceph_routes.ceph_sync_flags([_fake_session()])
    item = response.items[0]
    assert item.fetched == 0
    assert any("connection refused" in err for err in item.errors)


async def test_ceph_sync_falls_back_to_localhost_for_unknown_nodes(_patched_client):
    session = _fake_session()
    session.cluster_status = []
    session.node_name = None
    response = await ceph_routes.ceph_sync_osds([session])
    item = response.items[0]
    assert item.nodes == ["localhost"]
    assert item.fetched == 2
