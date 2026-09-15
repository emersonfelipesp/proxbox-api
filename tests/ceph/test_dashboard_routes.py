"""HTTP tests for the Ceph v2 Dashboard endpoint routes (#98)."""

from __future__ import annotations

from typing import Any


async def test_dashboard_provider_listed_in_capabilities(ceph_http_client) -> None:
    resp = await ceph_http_client.get("/ceph/v2/capabilities", params={"provider": "dashboard"})
    assert resp.status_code == 200
    provider = resp.json()["providers"][0]
    assert provider["provider"] == "dashboard"
    assert provider["supported"] is True
    assert provider["apply"] is False
    assert provider["destructive_operations"] is False


async def test_register_list_validate_endpoint(ceph_http_client, monkeypatch) -> None:
    create = await ceph_http_client.post(
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

    dup = await ceph_http_client.post(
        "/ceph/v2/dashboard/endpoints", json={"name": "ceph-prod", "base_url": "https://x:8443"}
    )
    assert dup.status_code == 409

    listed = await ceph_http_client.get("/ceph/v2/dashboard/endpoints")
    assert listed.status_code == 200
    assert any(e["name"] == "ceph-prod" and e["has_secret"] for e in listed.json())

    async def fake_validate(config: Any) -> tuple[bool, str | None]:
        assert config.base_url == "https://ceph:8443"
        assert config.password == "super-secret"  # decrypted for the probe
        return True, None

    monkeypatch.setattr("proxbox_api.ceph.v2_routes.validate_dashboard_endpoint", fake_validate)
    probe = await ceph_http_client.post(f"/ceph/v2/dashboard/endpoints/{endpoint_id}/validate")
    assert probe.status_code == 200
    assert probe.json() == {"id": endpoint_id, "ok": True, "error": None}


async def test_validate_missing_endpoint_404(ceph_http_client) -> None:
    resp = await ceph_http_client.post("/ceph/v2/dashboard/endpoints/9999/validate")
    assert resp.status_code == 404


async def test_validate_endpoint_never_reflects_sdk_exception_text(
    ceph_http_client,
    monkeypatch,
) -> None:
    created = await ceph_http_client.post(
        "/ceph/v2/dashboard/endpoints",
        json={"name": "unsafe-probe", "base_url": "https://ceph.invalid"},
    )
    endpoint_id = created.json()["id"]

    async def unsafe_error(_config: Any) -> tuple[bool, str | None]:
        return False, "https://operator:dashboard-secret@ceph.invalid?token=canary"

    monkeypatch.setattr(
        "proxbox_api.ceph.v2_routes.validate_dashboard_endpoint",
        unsafe_error,
    )
    response = await ceph_http_client.post(f"/ceph/v2/dashboard/endpoints/{endpoint_id}/validate")

    assert response.status_code == 200
    assert response.json()["error"] == "Ceph Dashboard endpoint validation failed."
    assert "dashboard-secret" not in response.text


async def test_dashboard_metrics_warns_without_endpoint(ceph_http_client) -> None:
    resp = await ceph_http_client.get("/ceph/v2/metrics", params={"provider": "dashboard"})
    assert resp.status_code == 200
    assert any("Dashboard endpoint" in w for w in resp.json()["warnings"])
