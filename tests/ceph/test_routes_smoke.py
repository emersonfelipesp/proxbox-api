"""Smoke tests for the read-only ``/ceph/*`` route helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import Response
from proxmox_sdk.sdk.exceptions import ResourceException

from proxbox_api.ceph import routes as ceph_routes
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.exception import ProxboxException
from proxbox_api.session.proxmox import ProxmoxSession


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
        return [
            {"name": "rbd", "application": "rbd"},
            {"name": "rgw.meta", "application_list": ["rgw"]},
            {"name": "cephfs_data", "application": "cephfs"},
        ]

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
        self.rgw = _FakeRGW()
        self.rbd = _FakeRBD()

    async def status(self):
        return await self.cluster.status()


class _FakeRGW:
    async def realms(self):
        return [{"name": "realm-a", "is_default": True}]

    async def zonegroups(self):
        return [
            {
                "name": "zg-a",
                "realm_name": "realm-a",
                "is_master": True,
                "endpoints": ["https://s3.example.local"],
            }
        ]

    async def zones(self):
        return [{"name": "zone-a", "zonegroup_name": "zg-a"}]

    async def placement_targets(self):
        return [{"name": "default-placement", "storage_classes": ["STANDARD"]}]

    async def list_users(self):
        return [
            {
                "user_id": "alice",
                "display_name": "Alice",
                "keys": [{"access_key": "AK", "secret_key": "SK"}],
                "swift_keys": [{"access_key": "SAK", "secret_key": "SSK"}],
                "access_keys": [{"access_key": "AK2"}],
            }
        ]

    async def list_buckets(self):
        return ["backups"]

    async def get_bucket(self, bucket):
        return {
            "bucket": bucket,
            "owner": "alice",
            "num_objects": 12,
            "size_kb_actual": 4,
            "placement_rule": "default-placement",
        }


class _FakeRBD:
    async def list_images(self, pool_name=None):
        return [
            {
                "pool_name": pool_name or "rbd",
                "name": "vm-100-disk-0",
                "id": "img-1",
                "size": 1024,
                "obj_size": 4096,
                "features_name": ["layering"],
                "num_objs": 1,
                "snapshots": [{"name": "base", "id": 1, "protected": True}],
            }
        ]

    async def children(self, pool_name, image_name, snapshot_name):
        if pool_name == "rbd" and image_name == "vm-100-disk-0" and snapshot_name == "base":
            return [{"child_pool_name": "rbd", "child_name": "vm-101-disk-0"}]
        return []


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


def _assert_session_error_response(
    response: Response,
    *,
    status_code: int,
    error_type: str,
    upstream_status: int | None = None,
) -> None:
    detail: dict[str, object] = {
        "reason": "proxmox_session_acquisition_failed",
        "error_type": error_type,
    }
    if upstream_status is not None:
        detail["upstream_status"] = upstream_status
    assert (response.status_code, response.json()) == (
        status_code,
        {
            "message": "Could not return Proxmox Sessions",
            "detail": detail,
            "python_exception": error_type,
        },
    )


def _assert_response_excludes(response: Response, *canaries: str) -> None:
    assert all(canary not in response.text for canary in canaries)


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


def test_ceph_status_reports_unconfigured_endpoint(auth_test_client):
    response = auth_test_client.get("/ceph/status")

    assert response.status_code == 404
    assert response.json() == {
        "message": "No Proxmox endpoint is configured for Ceph status",
        "detail": {
            "reason": "ceph_endpoint_not_configured",
            "message": "Configure at least one Proxmox endpoint before querying Ceph status.",
        },
        "python_exception": None,
    }


def test_ceph_status_reports_typed_sanitized_session_acquisition_failures(
    monkeypatch,
    db_session,
    auth_test_client,
):
    db_session.add(
        ProxmoxEndpoint(
            name="pve01",
            ip_address="10.0.0.10",
            domain="pve.local",
            port=8006,
            username="root@pam",
            password="password",
            verify_ssl=False,
        )
    )
    db_session.commit()

    class MisleadingFailure(RuntimeError):
        status_code = 418

    async def fail_with_misleading_status(*_args, **_kwargs):
        raise MisleadingFailure("secret-internal-token")

    monkeypatch.setattr(ProxmoxSession, "create", fail_with_misleading_status)
    response = auth_test_client.get("/ceph/status")
    _assert_session_error_response(
        response,
        status_code=502,
        error_type="MisleadingFailure",
    )
    _assert_response_excludes(response, "secret-internal-token")

    async def fail_with_direct_sdk_error(*_args, **_kwargs):
        raise ResourceException(401, "Unauthorized", "secret-direct-upstream-body")

    monkeypatch.setattr(ProxmoxSession, "create", fail_with_direct_sdk_error)
    response = auth_test_client.get("/ceph/status")
    _assert_session_error_response(
        response,
        status_code=401,
        error_type="ResourceException",
        upstream_status=401,
    )
    _assert_response_excludes(response, "secret-direct-upstream-body")

    async def fail_with_wrapped_sdk_error(*_args, **_kwargs):
        try:
            raise ResourceException(503, "Unavailable", "secret-wrapped-upstream-body")
        except ResourceException as error:
            raise ProxboxException(
                message=(
                    "Authentication failed for pve.secret.example using token secret-token-name"
                ),
                python_exception=str(error),
            ) from error

    monkeypatch.setattr(ProxmoxSession, "create", fail_with_wrapped_sdk_error)
    response = auth_test_client.get("/ceph/status")
    _assert_session_error_response(
        response,
        status_code=503,
        error_type="ProxboxException",
        upstream_status=503,
    )
    _assert_response_excludes(
        response,
        "pve.secret.example",
        "secret-token-name",
        "secret-wrapped-upstream-body",
    )


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
        (ceph_routes.ceph_sync_rgw, 7),
        (ceph_routes.ceph_sync_rbd, 4),
        (ceph_routes.ceph_sync_full, 35),
    ],
)
async def test_ceph_sync_routes_summary(_patched_client, handler, expected):
    response = await handler([_fake_session()])
    item = response.items[0]
    assert item.errors == []
    assert item.fetched == expected


async def test_ceph_sync_rgw_returns_reflected_inventory(_patched_client):
    response = await ceph_routes.ceph_sync_rgw([_fake_session()])
    item = response.items[0]
    assert item.resource == "rgw"
    assert item.errors == []
    assert item.fetched == 7
    assert response.raw is not None
    rgw = response.raw["rgw"]
    assert rgw["realms"][0]["name"] == "realm-a"
    assert rgw["users"][0]["uid"] == "alice"
    assert rgw["users"][0]["status"]["keys"] == "[redacted]"
    assert rgw["users"][0]["status"]["swift_keys"] == "[redacted]"
    assert rgw["users"][0]["status"]["access_keys"] == "[redacted]"
    assert rgw["buckets"][0]["size_bytes"] == 4096
    assert response.raw["clusters"][0]["inventory"]["pools"][0]["name"] == "rgw.meta"


async def test_ceph_sync_rbd_returns_images_snapshots_and_clones(_patched_client):
    response = await ceph_routes.ceph_sync_rbd([_fake_session()])
    item = response.items[0]
    assert item.resource == "rbd"
    assert item.errors == []
    assert item.fetched == 4
    assert response.raw is not None
    rbd = response.raw["rbd"]
    assert rbd["pools"][0]["name"] == "rbd"
    assert rbd["images"][0]["name"] == "vm-100-disk-0"
    assert rbd["images"][0]["features"] == ["layering"]
    assert rbd["snapshots"][0]["name"] == "base"
    assert rbd["snapshots"][0]["protected"] is True
    assert rbd["clones"][0]["parent_image"] == {
        "pool_name": "rbd",
        "namespace": "",
        "name": "vm-100-disk-0",
    }
    assert rbd["clones"][0]["parent_snapshot"] == {
        "pool_name": "rbd",
        "namespace": "",
        "image_name": "vm-100-disk-0",
        "name": "base",
    }
    assert rbd["clones"][0]["parent_pool_name"] == "rbd"
    assert rbd["clones"][0]["parent_image_name"] == "vm-100-disk-0"
    assert rbd["clones"][0]["parent_snapshot_name"] == "base"
    assert rbd["clones"][0]["child_pool_name"] == "rbd"
    assert rbd["clones"][0]["child_name"] == "vm-101-disk-0"


async def test_ceph_sync_full_returns_rgw_and_rbd_inventory(_patched_client):
    response = await ceph_routes.ceph_sync_full([_fake_session()])
    item = response.items[0]
    assert item.resource == "full"
    assert item.errors == []
    assert item.fetched == 35
    assert response.raw is not None
    assert response.raw["resource"] == "full"
    assert response.raw["rgw"]["realms"][0]["name"] == "realm-a"
    assert response.raw["rbd"]["clones"][0]["child_name"] == "vm-101-disk-0"
    assert response.raw["clusters"][0]["inventory"]["rgw"]["buckets"][0]["name"] == "backups"
    assert (
        response.raw["clusters"][0]["inventory"]["rbd"]["clones"][0]["parent_snapshot_name"]
        == "base"
    )


async def test_ceph_sync_rgw_rbd_empty_when_not_configured(monkeypatch):
    class _EmptyNodes(_FakeNodes):
        async def pools(self, _node):
            return [{"name": "cephfs_data", "application": "cephfs"}]

    class _EmptyClient:
        def __init__(self):
            self.nodes = _EmptyNodes()

    monkeypatch.setattr(ceph_routes, "_client_for", lambda _px: _EmptyClient())

    rgw_response = await ceph_routes.ceph_sync_rgw([_fake_session()])
    rbd_response = await ceph_routes.ceph_sync_rbd([_fake_session()])

    assert rgw_response.items[0].errors == []
    assert rgw_response.items[0].fetched == 0
    assert rgw_response.raw is not None
    assert rgw_response.raw["rgw"]["realms"] == []
    assert rgw_response.raw["rgw"]["buckets"] == []

    assert rbd_response.items[0].errors == []
    assert rbd_response.items[0].fetched == 0
    assert rbd_response.raw is not None
    assert rbd_response.raw["rbd"]["images"] == []
    assert rbd_response.raw["rbd"]["snapshots"] == []
    assert rbd_response.raw["rbd"]["clones"] == []


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


async def test_ceph_sync_keeps_unknown_nodes_empty_without_localhost_fallback(_patched_client):
    session = _fake_session()
    session.cluster_status = []
    session.node_name = None
    response = await ceph_routes.ceph_sync_osds([session])
    item = response.items[0]
    assert item.nodes == []
    assert item.fetched == 0
