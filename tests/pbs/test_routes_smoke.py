"""Smoke tests for the read-only ``/pbs/sync/*`` and ``/pbs/status`` routes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proxbox_api.pbs import routes as pbs_routes


class _FakeDatastores:
    def __init__(self, payload):
        self._payload = payload

    async def list(self):
        return self._payload


class _FakeSnapshots:
    def __init__(self, by_store: dict[str, list]):
        self._by_store = by_store

    async def list(self, store, **_kwargs):
        return self._by_store.get(store, [])


class _FakeJobs:
    def __init__(self, payload):
        self._payload = payload

    async def list(self, *_args, **_kwargs):
        return self._payload


class _FakeNodes:
    def __init__(self, payload):
        self._payload = payload

    async def status(self, _node):
        return self._payload


class _FakeVersion:
    def __init__(self, version: str):
        self.version = version


class _FakePBSClient:
    """Stand-in for ``proxmox_sdk.pbs.PBSClient`` used by routes."""

    def __init__(
        self,
        *,
        datastores=None,
        snapshots=None,
        jobs=None,
        node_status=None,
        version="3.4.2",
    ):
        self.datastores = _FakeDatastores(datastores or [])
        self.snapshots = _FakeSnapshots(snapshots or {})
        self.jobs = _FakeJobs(jobs or [])
        self.nodes = _FakeNodes(node_status or {"hostname": "pbs01"})
        self._version_str = version

    async def version(self):
        return _FakeVersion(self._version_str)

    async def close(self):
        return None


class _Store:
    def __init__(self, name: str):
        self.name = name


@pytest.fixture
def _patched_client(monkeypatch):
    captured: list[dict] = []

    fake = _FakePBSClient(
        datastores=[_Store("primary"), _Store("offsite")],
        snapshots={"primary": [object(), object()], "offsite": [object()]},
        jobs=[object(), object(), object()],
        node_status={"hostname": "pbs01"},
    )

    def _client_for(endpoint):
        captured.append({"endpoint": endpoint})
        return fake

    monkeypatch.setattr(pbs_routes, "_client_for", _client_for)
    return captured


def _create_endpoint(client: TestClient) -> int:
    response = client.post(
        "/pbs/endpoints",
        json={
            "name": "pbs-smoke",
            "host": "pbs.example.local",
            "port": 8007,
            "token_id": "root@pam!sync",
            "token_secret": "shhh",
        },
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_pbs_status_reports_reachable(client: TestClient, _patched_client):
    _create_endpoint(client)
    response = client.get("/pbs/status")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["reachable"] is True
    assert item["version"] == "3.4.2"


def test_pbs_status_empty_when_no_endpoints(client: TestClient):
    response = client.get("/pbs/status")
    assert response.status_code == 200
    assert response.json() == {"items": []}


@pytest.mark.parametrize(
    ("path", "expected_fetched"),
    [
        ("/pbs/sync/datastores", 2),
        ("/pbs/sync/snapshots", 3),
        ("/pbs/sync/jobs", 3),
        ("/pbs/sync/node", 1),
        ("/pbs/sync/full", 2 + 3 + 3),
    ],
)
def test_pbs_sync_routes_summary(
    client: TestClient, _patched_client, path: str, expected_fetched: int
):
    _create_endpoint(client)
    response = client.get(path)
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    summary = body["items"][0]
    assert summary["errors"] == []
    assert summary["fetched"] == expected_fetched


def test_pbs_sync_threads_branch_query_into_summary(client: TestClient, _patched_client):
    _create_endpoint(client)
    response = client.get(
        "/pbs/sync/datastores",
        params={"netbox_branch_schema_id": "br_abc123"},
    )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["netbox_branch_schema_id"] == "br_abc123"


def test_pbs_sync_records_client_errors(client: TestClient, monkeypatch):
    class _BrokenClient(_FakePBSClient):
        async def version(self):
            raise RuntimeError("connection refused")

        async def close(self):
            return None

    fake = _BrokenClient()
    fake.datastores = _FakeDatastores([])

    async def _boom(self):
        raise RuntimeError("connection refused")

    fake.datastores.list = _boom.__get__(fake.datastores)  # type: ignore[attr-defined]

    def _client_for(_endpoint):
        return fake

    monkeypatch.setattr(pbs_routes, "_client_for", _client_for)

    _create_endpoint(client)
    response = client.get("/pbs/sync/datastores")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["fetched"] == 0
    assert any("connection refused" in err for err in item["errors"])
