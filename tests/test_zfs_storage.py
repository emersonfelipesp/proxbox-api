"""Tiered ZFS storage retrieval tests."""

from __future__ import annotations

import pytest
from proxmox_sdk.sdk.exceptions import ResourceException

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
    def __init__(
        self,
        calls: list[str],
        pools: object,
        details: dict[str, object],
        node_name: str,
    ) -> None:
        self._calls = calls
        self._pools = pools
        self._details = details
        self._node_name = node_name

    def get(self) -> object:
        self._calls.append(f"nodes/{self._node_name}/disks/zfs")
        return self._pools

    def __call__(self, name: str) -> _Endpoint:
        return _Endpoint(
            self._details[name],
            self._calls,
            f"nodes/{self._node_name}/disks/zfs/{name}",
        )


class _DisksAccessor:
    def __init__(
        self,
        calls: list[str],
        pools: object,
        details: dict[str, object],
        node_name: str,
    ) -> None:
        self.zfs = _ZfsAccessor(calls, pools, details, node_name)


class _NodeAccessor:
    def __init__(
        self,
        calls: list[str],
        pools: object,
        details: dict[str, object],
        node_name: str,
    ) -> None:
        self.disks = _DisksAccessor(calls, pools, details, node_name)


class _NodesAccessor:
    def __init__(
        self,
        calls: list[str],
        pools: object,
        details: dict[str, object],
        node_name: str,
    ) -> None:
        self._calls = calls
        self._pools = pools
        self._details = details
        self._node_name = node_name

    def __call__(self, node: str) -> _NodeAccessor:
        assert node == self._node_name
        return _NodeAccessor(self._calls, self._pools, self._details, self._node_name)


class _Sdk:
    def __init__(
        self,
        calls: list[str],
        pools: object,
        details: dict[str, object],
        node_name: str = "pve1",
    ) -> None:
        self.nodes = _NodesAccessor(calls, pools, details, node_name)


class _FailingNodesDiscovery:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def get(self) -> object:
        async def _raise() -> object:
            raise self._error

        return _raise()


class _FailingDiscoverySdk:
    def __init__(self, error: Exception) -> None:
        self.nodes = _FailingNodesDiscovery(error)


class _Session:
    def __init__(
        self,
        pools: object,
        details: dict[str, object] | None = None,
        *,
        cluster_status: list[object] | None = None,
        name: str = "lab",
        node_name: str = "pve1",
        db_endpoint_id: int | None = None,
        domain: str | None = None,
        ip_address: str | None = None,
        http_port: int = 8006,
        sdk: object | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.session = sdk or _Sdk(self.calls, pools, details or {}, node_name)
        self.cluster_status = (
            [{"type": "node", "name": node_name}] if cluster_status is None else cluster_status
        )
        self.name = name
        self.db_endpoint_id = db_endpoint_id
        self.domain = domain
        self.ip_address = ip_address
        self.http_port = http_port


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
    error = RuntimeError(
        "request failed token=abc123 token_value=secret-token api_key=secret-api-key "
        "CSRFPreventionToken=secret-csrf password:supersecret "
        '{"client_secret": "json-secret"} https://user:pass@example.test/path '
        "Authorization: Bearer SEKRET Basic BASICSEKRET token TOKENSEKRET "
        "http://:empty-user-secret@example.test/path"
    )

    detail = zfs_service._safe_error_detail(error)

    assert "abc123" not in detail
    assert "secret-token" not in detail
    assert "secret-api-key" not in detail
    assert "secret-csrf" not in detail
    assert "supersecret" not in detail
    assert "json-secret" not in detail
    assert "user:pass" not in detail
    assert "SEKRET" not in detail
    assert "BASICSEKRET" not in detail
    assert "TOKENSEKRET" not in detail
    assert "empty-user-secret" not in detail
    assert "token=[REDACTED]" in detail
    assert "token_value=[REDACTED]" in detail
    assert "api_key=[REDACTED]" in detail
    assert "CSRFPreventionToken=[REDACTED]" in detail
    assert '"client_secret": "[REDACTED]"' in detail
    assert "https://[REDACTED]@example.test/path" in detail
    assert "Authorization: Bearer [REDACTED]" in detail
    assert "Basic [REDACTED]" in detail
    assert "token [REDACTED]" in detail
    assert "http://[REDACTED]@example.test/path" in detail


@pytest.mark.parametrize(
    ("message", "leaked_fragments", "expected_fragment"),
    [
        ('Authorization: "Bearer SECRET"', ["SECRET"], 'Authorization: "Bearer [REDACTED]"'),
        ("Authorization: Bearer SECRET", ["SECRET"], "Authorization: Bearer [REDACTED]"),
        ('Authorization: Bearer "SECRET"', ["SECRET"], "Authorization: Bearer [REDACTED]"),
        ("Authorization: Bearer 'SECRET'", ["SECRET"], "Authorization: Bearer [REDACTED]"),
        ("Bearer SECRET", ["SECRET"], "Bearer [REDACTED]"),
        ('Bearer "SECRET"', ["SECRET"], "Bearer [REDACTED]"),
        ("Bearer 'SECRET'", ["SECRET"], "Bearer [REDACTED]"),
        ('password="SECRET"', ["SECRET"], "password=[REDACTED]"),
        ("api_key='SECRET'", ["SECRET"], "api_key=[REDACTED]"),
        ("token=SECRET", ["SECRET"], "token=[REDACTED]"),
        ('secret : "SECRET"', ["SECRET"], "secret : [REDACTED]"),
        (str({"password": "SECRET"}), ["SECRET"], "'password': [REDACTED]"),
        (str({"api_key": "SECRET"}), ["SECRET"], "'api_key': [REDACTED]"),
        ("{'token': 'SECRET'}", ["SECRET"], "'token': [REDACTED]"),
        ("password=b'SECRET'", ["SECRET"], "password=[REDACTED]"),
        ('token=b"SECRET"', ["SECRET"], "token=[REDACTED]"),
        ("api_key=rb'SECRET'", ["SECRET"], "api_key=[REDACTED]"),
        ("secret=f'SECRET'", ["SECRET"], "secret=[REDACTED]"),
        ('{"password":"SECRET"}', ["SECRET"], '"password":"[REDACTED]"'),
        ('{"client_secret": "abc\\"def"}', ["abc", "def"], '"client_secret": "[REDACTED]"'),
        ('{"token": 12345}', ["12345"], '"token": "[REDACTED]"'),
        ('{"secret": false}', ["false"], '"secret": "[REDACTED]"'),
        ('{"password": "p@ss"}', ["p@ss"], '"password": "[REDACTED]"'),
        ("http://:secret@h/p", ["secret"], "http://[REDACTED]@h/p"),
        ("http://user:secret@h/p", ["user:secret"], "http://[REDACTED]@h/p"),
    ],
)
def test_zfs_error_detail_redacts_adversarial_secret_forms(
    message: str,
    leaked_fragments: list[str],
    expected_fragment: str,
) -> None:
    detail = zfs_service._safe_error_detail(RuntimeError(message))

    for leaked_fragment in leaked_fragments:
        assert leaked_fragment not in detail
    assert expected_fragment in detail


def test_zfs_vdev_tree_rejects_adversarial_depth() -> None:
    root: list[dict[str, object]] = [{"name": "root"}]
    current = root[0]
    for index in range(zfs_service._MAX_VDEV_TREE_DEPTH + 1):
        child: dict[str, object] = {"name": f"child-{index}"}
        current["children"] = [child]
        current = child

    with pytest.raises(ProxboxException, match="ZFS vdev tree exceeds maximum depth"):
        zfs_service._parse_vdev_tree(root)


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
async def test_zfs_node_discovery_failure_degrades_to_failed_source() -> None:
    error = ResourceException(
        status_code=403,
        status_message="Forbidden",
        content="api_key=secret-api-key denied",
    )
    session = _Session(
        pools=[],
        cluster_status=[],
        sdk=_FailingDiscoverySdk(error),
    )

    response = await list_zfs_pools([session], tiers=("proxmox_api",))

    assert response.source is None
    assert response.pools == []
    assert len(response.attempted_sources) == 1
    attempt = response.attempted_sources[0]
    assert attempt.tier == "proxmox_api"
    assert attempt.status == "failed"
    assert attempt.reason is not None
    assert "node discovery" in attempt.reason
    assert "secret-api-key" not in attempt.reason
    assert "api_key=[REDACTED]" in attempt.reason


@pytest.mark.asyncio
async def test_zfs_proxmox_api_dedupes_same_cluster_sessions() -> None:
    cluster_status = [
        {"type": "cluster", "name": "lab"},
        {"type": "node", "name": "pve1"},
    ]
    first = _Session(
        pools=[{"name": "tank", "health": "ONLINE"}],
        cluster_status=cluster_status,
        name="endpoint-a",
    )
    second = _Session(
        pools=[{"name": "tank", "health": "ONLINE"}],
        cluster_status=cluster_status,
        name="endpoint-b",
    )

    response = await list_zfs_pools([first, second], tiers=("proxmox_api",))

    assert [(pool.cluster_name, pool.node, pool.name) for pool in response.pools] == [
        ("lab", "pve1", "tank")
    ]
    assert first.calls == ["nodes/pve1/disks/zfs"]
    assert second.calls == []


@pytest.mark.asyncio
async def test_zfs_proxmox_api_keeps_distinct_standalone_endpoints_with_same_node_name() -> None:
    first = _Session(
        pools=[{"name": "tank-a", "health": "ONLINE"}],
        cluster_status=[{"type": "node", "name": "pve"}],
        name="pve",
        node_name="pve",
        db_endpoint_id=101,
    )
    second = _Session(
        pools=[{"name": "tank-b", "health": "ONLINE"}],
        cluster_status=[{"type": "node", "name": "pve"}],
        name="pve",
        node_name="pve",
        db_endpoint_id=202,
    )

    response = await list_zfs_pools([first, second], tiers=("proxmox_api",))

    assert [(pool.cluster_name, pool.node, pool.name) for pool in response.pools] == [
        ("pve", "pve", "tank-a"),
        ("pve", "pve", "tank-b"),
    ]
    assert first.calls == ["nodes/pve/disks/zfs"]
    assert second.calls == ["nodes/pve/disks/zfs"]


@pytest.mark.asyncio
async def test_zfs_proxmox_api_keeps_unknown_identity_standalone_sessions_separate() -> None:
    first = _Session(
        pools=[{"name": "tank-a", "health": "ONLINE"}],
        cluster_status=[{"type": "node", "name": "pve"}],
        name="pve",
        node_name="pve",
    )
    second = _Session(
        pools=[{"name": "tank-b", "health": "ONLINE"}],
        cluster_status=[{"type": "node", "name": "pve"}],
        name="pve",
        node_name="pve",
    )

    response = await list_zfs_pools([first, second], tiers=("proxmox_api",))

    assert [pool.name for pool in response.pools] == ["tank-a", "tank-b"]
    assert first.calls == ["nodes/pve/disks/zfs"]
    assert second.calls == ["nodes/pve/disks/zfs"]


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
