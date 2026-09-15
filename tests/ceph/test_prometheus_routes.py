"""HTTP tests for the Ceph v2 Prometheus metrics + source routes (#94)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from proxbox_api.ceph.v2_schemas import CephMetricSnapshot


async def test_metrics_prometheus_without_source_warns(ceph_http_client) -> None:
    resp = await ceph_http_client.get("/ceph/v2/metrics", params={"provider": "prometheus"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["snapshot"] is None
    assert any("no Prometheus source" in w for w in body["warnings"])


async def test_register_list_and_validate_source(ceph_http_client, monkeypatch) -> None:
    # Register a source; the bearer token must never be echoed back.
    create = await ceph_http_client.post(
        "/ceph/v2/metrics/sources",
        json={
            "name": "ceph-prod",
            "url": "http://prom:9090",
            "bearer_token": "super-secret",
            "cluster_ref": "cluster:1",
        },
    )
    assert create.status_code == 201, create.text
    out = create.json()
    assert out["has_token"] is True
    assert "bearer_token" not in out and "super-secret" not in create.text
    source_id = out["id"]

    # Duplicate name -> 409.
    dup = await ceph_http_client.post(
        "/ceph/v2/metrics/sources", json={"name": "ceph-prod", "url": "http://x:9090"}
    )
    assert dup.status_code == 409

    listed = await ceph_http_client.get("/ceph/v2/metrics/sources")
    assert listed.status_code == 200
    assert any(s["name"] == "ceph-prod" and s["has_token"] for s in listed.json())

    # Validate probes the source; monkeypatch the network probe.
    async def fake_validate(config: Any) -> tuple[bool, str | None]:
        assert config.url == "http://prom:9090"
        assert config.bearer_token == "super-secret"  # decrypted for the probe
        return True, None

    monkeypatch.setattr("proxbox_api.ceph.v2_routes.validate_source", fake_validate)
    probe = await ceph_http_client.post(f"/ceph/v2/metrics/sources/{source_id}/validate")
    assert probe.status_code == 200
    assert probe.json() == {"id": source_id, "ok": True, "error": None}


async def test_validate_source_never_reflects_http_exception_text(
    ceph_http_client,
    monkeypatch,
) -> None:
    created = await ceph_http_client.post(
        "/ceph/v2/metrics/sources",
        json={"name": "unsafe-probe", "url": "https://prom.invalid"},
    )
    source_id = created.json()["id"]

    async def unsafe_error(_config: Any) -> tuple[bool, str | None]:
        return False, "https://operator:prom-secret@prom.invalid?token=canary"

    monkeypatch.setattr("proxbox_api.ceph.v2_routes.validate_source", unsafe_error)
    response = await ceph_http_client.post(f"/ceph/v2/metrics/sources/{source_id}/validate")

    assert response.status_code == 200
    assert response.json()["error"] == "Prometheus source validation failed."
    assert "prom-secret" not in response.text


async def test_metrics_returns_typed_snapshot_when_source_configured(
    ceph_http_client, monkeypatch
) -> None:
    await ceph_http_client.post(
        "/ceph/v2/metrics/sources",
        json={"name": "ceph-1", "url": "http://prom:9090", "cluster_ref": "cluster:1"},
    )
    snap = CephMetricSnapshot(
        cluster_health="HEALTH_WARN",
        captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        osd_up=5,
        recovering_pgs=2,
    )

    async def fake_fetch(config: Any) -> CephMetricSnapshot:
        return snap

    monkeypatch.setattr("proxbox_api.ceph.v2_providers.prometheus.fetch_snapshot", fake_fetch)
    resp = await ceph_http_client.get(
        "/ceph/v2/metrics", params={"provider": "prometheus", "object_ref": "cluster:1"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["snapshot"]["cluster_health"] == "HEALTH_WARN"
    assert body["snapshot"]["osd_up"] == 5
    # the source config must not leak into the echoed scope
    assert "prometheus_source" not in body["scope"]


async def test_prometheus_listed_in_capabilities(ceph_http_client) -> None:
    resp = await ceph_http_client.get("/ceph/v2/capabilities", params={"provider": "prometheus"})
    assert resp.status_code == 200
    providers = resp.json()["providers"]
    assert providers[0]["provider"] == "prometheus"
    assert providers[0]["supported"] is True
    assert providers[0]["metrics"] is True
