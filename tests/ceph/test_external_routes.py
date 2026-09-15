"""HTTP tests for the Ceph v2 external-cluster routes (#97)."""

from __future__ import annotations


async def test_external_provider_listed_in_capabilities(ceph_http_client) -> None:
    resp = await ceph_http_client.get("/ceph/v2/capabilities", params={"provider": "external"})
    assert resp.status_code == 200
    provider = resp.json()["providers"][0]
    assert provider["provider"] == "external"
    assert provider["supported"] is True
    assert provider["apply"] is False
    assert provider["destructive_operations"] is False


async def test_register_list_external_cluster_redacts_secrets(ceph_http_client) -> None:
    create = await ceph_http_client.post(
        "/ceph/v2/external/clusters",
        json={
            "name": "lab-ceph",
            "cluster_ref": "ext:1",
            "ceph_version_hint": "18.2.4",
            "rgw_admin_url": "http://rgw:8080",
            "rgw_access_key": "AKIA",
            "rgw_secret_key": "topsecret",
        },
    )
    assert create.status_code == 201, create.text
    out = create.json()
    assert out["has_rgw_credentials"] is True
    assert "rgw_secret_key" not in out and "topsecret" not in create.text
    cluster_id = out["id"]

    dup = await ceph_http_client.post("/ceph/v2/external/clusters", json={"name": "lab-ceph"})
    assert dup.status_code == 409

    listed = await ceph_http_client.get("/ceph/v2/external/clusters")
    assert listed.status_code == 200
    assert any(c["name"] == "lab-ceph" and c["has_rgw_credentials"] for c in listed.json())
    assert cluster_id > 0


async def test_external_cluster_capability_detection(ceph_http_client) -> None:
    create = await ceph_http_client.post(
        "/ceph/v2/external/clusters",
        json={"name": "lab-2", "cluster_ref": "ext:2", "ceph_version_hint": "18.2.4"},
    )
    cluster_id = create.json()["id"]
    caps = await ceph_http_client.post(f"/ceph/v2/external/clusters/{cluster_id}/capabilities")
    assert caps.status_code == 200
    provider = caps.json()["providers"][0]
    assert provider["provider"] == "external"
    # no sub-providers configured -> writes/reads off
    assert provider["apply"] is False


async def test_capabilities_missing_cluster_404(ceph_http_client) -> None:
    resp = await ceph_http_client.post("/ceph/v2/external/clusters/9999/capabilities")
    assert resp.status_code == 404


async def test_external_metrics_warns_without_provider(ceph_http_client) -> None:
    resp = await ceph_http_client.get("/ceph/v2/metrics", params={"provider": "external"})
    assert resp.status_code == 200
    assert any("external cluster" in w for w in resp.json()["warnings"])
