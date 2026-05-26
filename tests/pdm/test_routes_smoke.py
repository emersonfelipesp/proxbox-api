"""Smoke tests for the read-only ``/pdm/sync/*`` and ``/pdm/status`` routes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from proxbox_api.pdm import routes as pdm_routes


class _FakeRemotes:
    def __init__(self, payload):
        self._payload = payload

    async def list(self):
        return self._payload


class _FakeResources:
    def __init__(self, payload):
        self._payload = payload

    async def list(self, **kwargs):
        resource_type = kwargs.get("type")
        if resource_type is None:
            return self._payload
        return [item for item in self._payload if getattr(item, "type", None) == resource_type]


class _FakePBS:
    def __init__(self, payload):
        self._payload = payload

    async def datastores(self, _remote_id):
        return self._payload


class _FakeVersion:
    def __init__(self, version: str):
        self.version = version


class _FakePDMClient:
    """Stand-in for ``proxmox_sdk.pdm.PDMClient`` used by routes."""

    def __init__(self, *, remotes=None, resources=None, datastores=None, version="0.9.0"):
        self.remotes = _FakeRemotes(remotes or [])
        self.resources = _FakeResources(resources or [])
        self.pbs = _FakePBS(datastores or [])
        self._version_str = version

    async def version(self):
        return _FakeVersion(self._version_str)

    async def close(self):
        return None


class _Remote:
    def __init__(self, name: str, remote_type: str):
        self.name = name
        self.id = name
        self.type = remote_type


class _Resource:
    def __init__(self, resource_type: str):
        self.type = resource_type


@pytest.fixture
def _patched_client(monkeypatch):
    captured: list[dict] = []

    fake = _FakePDMClient(
        remotes=[_Remote("pve-a", "pve"), _Remote("pbs-a", "pbs")],
        resources=[_Resource("vm"), _Resource("vm"), _Resource("ct"), _Resource("node")],
        datastores=[object(), object()],
    )

    def _client_for(endpoint):
        captured.append({"endpoint": endpoint})
        return fake

    monkeypatch.setattr(pdm_routes, "_client_for", _client_for)
    return captured


def _create_endpoint(client: TestClient, *, enabled: bool = True) -> int:
    response = client.post(
        "/pdm/endpoints",
        json={
            "name": "pdm-smoke",
            "host": "pdm.example.local",
            "port": 8443,
            "token_id": "root@pam!sync",
            "token_secret": "shhh",
            "enabled": enabled,
        },
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_pdm_status_reports_reachable(client: TestClient, _patched_client):
    _create_endpoint(client)
    response = client.get("/pdm/status")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["reachable"] is True
    assert item["version"] == "0.9.0"


def test_pdm_status_empty_when_no_endpoints(client: TestClient):
    response = client.get("/pdm/status")
    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_pdm_status_skips_disabled_endpoints(client: TestClient, _patched_client):
    _create_endpoint(client, enabled=False)
    response = client.get("/pdm/status")
    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert _patched_client == []


@pytest.mark.parametrize(
    ("path", "expected_fetched"),
    [
        ("/pdm/sync/remotes", 2),
        ("/pdm/sync/guests", 3),
        ("/pdm/sync/datastores", 2),
        ("/pdm/sync/resources", 4),
        ("/pdm/sync/full", 2 + 3 + 2 + 4),
    ],
)
def test_pdm_sync_routes_summary(
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


def test_pdm_sync_threads_branch_query_into_summary(client: TestClient, _patched_client):
    _create_endpoint(client)
    response = client.get(
        "/pdm/sync/remotes",
        params={"netbox_branch_schema_id": "br_abc123"},
    )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["netbox_branch_schema_id"] == "br_abc123"


def test_pdm_sync_skips_disabled_endpoints(client: TestClient, _patched_client):
    _create_endpoint(client, enabled=False)
    response = client.get("/pdm/sync/remotes")
    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert _patched_client == []


def test_pdm_sync_records_client_errors(client: TestClient, monkeypatch):
    class _BrokenClient(_FakePDMClient):
        async def version(self):
            raise RuntimeError("connection refused")

        async def close(self):
            return None

    fake = _BrokenClient()

    async def _boom():
        raise RuntimeError("connection refused")

    fake.remotes.list = _boom  # type: ignore[method-assign]

    def _client_for(_endpoint):
        return fake

    monkeypatch.setattr(pdm_routes, "_client_for", _client_for)

    _create_endpoint(client)
    response = client.get("/pdm/sync/remotes")
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["fetched"] == 0
    assert any("connection refused" in err for err in item["errors"])
