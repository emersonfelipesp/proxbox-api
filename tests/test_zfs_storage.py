"""Tiered ZFS storage retrieval tests."""

from __future__ import annotations

import pytest

from proxbox_api.exception import ProxboxException
from proxbox_api.services import zfs as zfs_service
from proxbox_api.services.proxmox_helpers import get_node_zfs_pool_detail, get_node_zfs_pools
from proxbox_api.services.zfs import get_zfs_pool_detail, list_zfs_pools


class _Endpoint:
    def __init__(self, payload: object, calls: list[str], path: str) -> None:
        self._payload = payload
        self._calls = calls
        self._path = path

    def get(self) -> object:
        self._calls.append(self._path)
        return self._payload


class _ZfsAccessor:
    def __init__(self, calls: list[str], pools: object, details: dict[str, object]) -> None:
        self._calls = calls
        self._pools = pools
        self._details = details

    def get(self) -> object:
        self._calls.append("nodes/pve1/disks/zfs")
        return self._pools

    def __call__(self, name: str) -> _Endpoint:
        return _Endpoint(
            self._details[name],
            self._calls,
            f"nodes/pve1/disks/zfs/{name}",
        )


class _DisksAccessor:
    def __init__(self, calls: list[str], pools: object, details: dict[str, object]) -> None:
        self.zfs = _ZfsAccessor(calls, pools, details)


class _NodeAccessor:
    def __init__(self, calls: list[str], pools: object, details: dict[str, object]) -> None:
        self.disks = _DisksAccessor(calls, pools, details)


class _NodesAccessor:
    def __init__(self, calls: list[str], pools: object, details: dict[str, object]) -> None:
        self._calls = calls
        self._pools = pools
        self._details = details

    def __call__(self, node: str) -> _NodeAccessor:
        assert node == "pve1"
        return _NodeAccessor(self._calls, self._pools, self._details)


class _Sdk:
    def __init__(self, calls: list[str], pools: object, details: dict[str, object]) -> None:
        self.nodes = _NodesAccessor(calls, pools, details)


class _Session:
    def __init__(self, pools: object, details: dict[str, object] | None = None) -> None:
        self.calls: list[str] = []
        self.session = _Sdk(self.calls, pools, details or {})
        self.cluster_status = [{"type": "node", "name": "pve1"}]
        self.name = "lab"


def test_tier1_zfs_helpers_validate_generated_list_and_detail_models() -> None:
    session = _Session(
        pools=[
            {
                "name": "rpool",
                "size": 1000,
                "alloc": 400,
                "free": 600,
                "frag": 7,
                "dedup": 1.0,
                "health": "ONLINE",
            }
        ],
        details={
            "rpool": {
                "name": "rpool",
                "state": "ONLINE",
                "status": "all vdevs healthy",
                "action": None,
                "scan": "scrub repaired 0B",
                "errors": "No known data errors",
                "children": [
                    {
                        "name": "mirror-0",
                        "state": "ONLINE",
                        "read": 0,
                        "write": 0,
                        "cksum": 0,
                        "children": [{"name": "sda", "state": "ONLINE"}],
                    }
                ],
            }
        },
    )

    pools = get_node_zfs_pools(session, "pve1")
    detail = get_node_zfs_pool_detail(session, "pve1", "rpool")

    assert pools[0].name == "rpool"
    assert pools[0].size == 1000
    assert detail.name == "rpool"
    assert detail.children[0]["children"][0]["name"] == "sda"
    assert session.calls == ["nodes/pve1/disks/zfs", "nodes/pve1/disks/zfs/rpool"]


def test_zfs_error_detail_redacts_credentials() -> None:
    error = RuntimeError("request failed token=abc123 password:supersecret")

    assert zfs_service._safe_error_detail(error) == (
        "request failed token=[REDACTED] password:[REDACTED]"
    )


@pytest.mark.asyncio
async def test_zfs_pool_detail_parses_vdev_tree_from_tier1() -> None:
    session = _Session(
        pools=[{"name": "tank", "health": "ONLINE", "size": 2000}],
        details={
            "tank": {
                "name": "tank",
                "state": "ONLINE",
                "status": "healthy",
                "errors": "No known data errors",
                "children": [
                    {
                        "name": "raidz1-0",
                        "state": "ONLINE",
                        "read": "0",
                        "write": "1",
                        "cksum": "2",
                        "msg": "ok",
                        "children": [{"name": "nvme0n1", "state": "ONLINE"}],
                    }
                ],
            }
        },
    )

    response = await get_zfs_pool_detail([session], name="tank")

    assert response.source == "proxmox_api"
    assert [attempt.tier for attempt in response.attempted_sources] == ["proxmox_api"]
    pool = response.pools[0]
    assert pool.name == "tank"
    assert pool.health == "ONLINE"
    assert pool.children[0].name == "raidz1-0"
    assert pool.children[0].read == 0
    assert pool.children[0].children[0].name == "nvme0n1"


@pytest.mark.asyncio
async def test_zfs_tier_selection_falls_back_in_order_when_tier1_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _tier1(*_args: object, **_kwargs: object) -> list:
        raise ProxboxException(message="api unavailable")

    async def _influx() -> list:
        return []

    async def _ssh() -> list:
        return []

    monkeypatch.setattr(zfs_service, "_fetch_pools_from_proxmox_api", _tier1)
    monkeypatch.setattr(zfs_service, "_fetch_pools_from_influxdb", _influx)
    monkeypatch.setattr(zfs_service, "_fetch_pools_from_ssh_cli", _ssh)

    response = await list_zfs_pools([_Session(pools=[])])

    assert response.source is None
    assert [
        (attempt.tier, attempt.status, attempt.reason) for attempt in response.attempted_sources
    ] == [
        ("proxmox_api", "failed", "api unavailable"),
        ("influxdb", "skipped", "not_configured"),
        ("ssh_cli", "skipped", "not_implemented"),
    ]


@pytest.mark.asyncio
async def test_zfs_tier_selection_treats_empty_tier1_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _influx() -> list:
        raise AssertionError("Empty Proxmox API response is authoritative")

    monkeypatch.setattr(zfs_service, "_fetch_pools_from_influxdb", _influx)

    response = await list_zfs_pools([_Session(pools=[])])

    assert response.source == "proxmox_api"
    assert response.pools == []
    assert [(attempt.tier, attempt.status) for attempt in response.attempted_sources] == [
        ("proxmox_api", "success")
    ]


@pytest.mark.asyncio
async def test_zfs_tier_selection_stops_after_tier1_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _tier1(*_args: object, **_kwargs: object) -> list:
        return [
            zfs_service.ZfsPoolSummary(
                cluster_name="lab",
                node="pve1",
                name="rpool",
                health="ONLINE",
            )
        ]

    async def _influx() -> list:
        raise AssertionError("InfluxDB tier should not be called after Tier 1 success")

    monkeypatch.setattr(zfs_service, "_fetch_pools_from_proxmox_api", _tier1)
    monkeypatch.setattr(zfs_service, "_fetch_pools_from_influxdb", _influx)

    response = await list_zfs_pools([_Session(pools=[])])

    assert response.source == "proxmox_api"
    assert response.pools[0].name == "rpool"
    assert [(attempt.tier, attempt.status) for attempt in response.attempted_sources] == [
        ("proxmox_api", "success")
    ]
