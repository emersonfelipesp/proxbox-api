"""Regression tests for per-cluster resource deduplication.

These cover the multi-endpoint failure mode where two *separate* Proxmox
clusters reuse the same VMID (so their cluster resources share an id like
``qemu/100``). A previous global dedup set silently dropped the second
cluster's resource. Dedup must be scoped per cluster identity, while still
collapsing duplicates reported by multiple endpoints of the *same* cluster.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import proxbox_api.routes.proxmox.cluster as cluster_module
from proxbox_api.routes.proxmox.cluster import cluster_resources


class _FakeResource:
    """Minimal stand-in for a typed Proxmox cluster-resource model."""

    def __init__(self, resource_id: str, vmid: int) -> None:
        self.id = resource_id
        self._data = {"id": resource_id, "vmid": vmid, "type": "qemu"}

    def model_dump(self, **_kwargs: object) -> dict[str, object]:
        return dict(self._data)


def _resources_by_cluster(result: list[dict]) -> dict[str, list]:
    return {name: items for entry in result for name, items in entry.items()}


def test_distinct_clusters_keep_overlapping_vmid(monkeypatch):
    """Two separate clusters that both own VMID 100 must both retain it."""
    px_a = SimpleNamespace(name="cluster-a")
    px_b = SimpleNamespace(name="cluster-b")

    async def fake_get(_px, resource_type=None):
        return [_FakeResource("qemu/100", 100)]

    monkeypatch.setattr(cluster_module, "get_typed_cluster_resources", fake_get)

    result = asyncio.run(cluster_resources(pxs=[px_a, px_b], type=None))
    by_cluster = _resources_by_cluster(result)

    assert set(by_cluster) == {"cluster-a", "cluster-b"}
    assert len(by_cluster["cluster-a"]) == 1
    assert len(by_cluster["cluster-b"]) == 1


def test_same_cluster_multiple_endpoints_dedupe(monkeypatch):
    """Two endpoints that are nodes of the SAME cluster collapse duplicates."""
    px1 = SimpleNamespace(name="shared-cluster")
    px2 = SimpleNamespace(name="shared-cluster")

    async def fake_get(_px, resource_type=None):
        return [_FakeResource("qemu/100", 100), _FakeResource("lxc/101", 101)]

    monkeypatch.setattr(cluster_module, "get_typed_cluster_resources", fake_get)

    result = asyncio.run(cluster_resources(pxs=[px1, px2], type=None))
    by_cluster = _resources_by_cluster(result)

    assert list(by_cluster) == ["shared-cluster"]
    assert len(by_cluster["shared-cluster"]) == 2
