"""HTTP tests for the Ceph v2 Dashboard endpoint routes (#98)."""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api.main import app
from proxbox_api.session.proxmox_providers import proxmox_sessions_dep


@pytest.fixture
def dash_client(auth_test_client):
    app.dependency_overrides[proxmox_sessions_dep] = lambda: []
    yield auth_test_client
    app.dependency_overrides.pop(proxmox_sessions_dep, None)


def test_dashboard_provider_listed_in_capabilities(dash_client) -> None:
    resp = dash_client.get("/ceph/v2/capabilities", params={"provider": "dashboard"})
    assert resp.status_code == 200
    provider = resp.json()["providers"][0]
    assert provider["provider"] == "dashboard"
    assert provider["supported"] is True


def test_register_list_validate_endpoint(dash_client, monkeypatch) -> None:
    create = dash_client.post(
        "/ceph/v2/dashboard/endpoints",
        json={
            "name": "ceph-prod",
            "base_url": "https://ceph:8443",
            "username": "admin",
            "password": "super-secret",
            "cluster_ref": "cluster:1",
        },
    )
    assert create.status_code == 201, create.text
    out = create.json()
    assert out["has_secret"] is True
    assert "password" not in out and "super-secret" not in create.text
    endpoint_id = out["id"]

    dup = dash_client.post(
        "/ceph/v2/dashboard/endpoints", json={"name": "ceph-prod", "base_url": "https://x:8443"}
    )
    assert dup.status_code == 409

    listed = dash_client.get("/ceph/v2/dashboard/endpoints")
    assert listed.status_code == 200
    assert any(e["name"] == "ceph-prod" and e["has_secret"] for e in listed.json())

    async def fake_validate(config: Any) -> tuple[bool, str | None]:
        assert config.base_url == "https://ceph:8443"
        assert config.password == "super-secret"  # decrypted for the probe
        return True, None

    monkeypatch.setattr("proxbox_api.ceph.v2_routes.validate_dashboard_endpoint", fake_validate)
    probe = dash_client.post(f"/ceph/v2/dashboard/endpoints/{endpoint_id}/validate")
    assert probe.status_code == 200
    assert probe.json() == {"id": endpoint_id, "ok": True, "error": None}


def test_validate_missing_endpoint_404(dash_client) -> None:
    resp = dash_client.post("/ceph/v2/dashboard/endpoints/9999/validate")
    assert resp.status_code == 404


def test_dashboard_metrics_warns_without_endpoint(dash_client) -> None:
    resp = dash_client.get("/ceph/v2/metrics", params={"provider": "dashboard"})
    assert resp.status_code == 200
    assert any("Dashboard endpoint" in w for w in resp.json()["warnings"])
