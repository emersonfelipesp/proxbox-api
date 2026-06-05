"""HTTP tests for the Ceph v2 Prometheus metrics + source routes (#94)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from proxbox_api.ceph.v2_schemas import CephMetricSnapshot
from proxbox_api.main import app
from proxbox_api.session.proxmox_providers import proxmox_sessions_dep


@pytest.fixture
def prom_client(auth_test_client):
    app.dependency_overrides[proxmox_sessions_dep] = lambda: []
    yield auth_test_client
    app.dependency_overrides.pop(proxmox_sessions_dep, None)


def test_metrics_prometheus_without_source_warns(prom_client) -> None:
    resp = prom_client.get("/ceph/v2/metrics", params={"provider": "prometheus"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["snapshot"] is None
    assert any("no Prometheus source" in w for w in body["warnings"])


def test_register_list_and_validate_source(prom_client, monkeypatch) -> None:
    # Register a source; the bearer token must never be echoed back.
    create = prom_client.post(
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
    dup = prom_client.post(
        "/ceph/v2/metrics/sources", json={"name": "ceph-prod", "url": "http://x:9090"}
    )
    assert dup.status_code == 409

    listed = prom_client.get("/ceph/v2/metrics/sources")
    assert listed.status_code == 200
    assert any(s["name"] == "ceph-prod" and s["has_token"] for s in listed.json())

    # Validate probes the source; monkeypatch the network probe.
    async def fake_validate(config: Any) -> tuple[bool, str | None]:
        assert config.url == "http://prom:9090"
        assert config.bearer_token == "super-secret"  # decrypted for the probe
        return True, None

    monkeypatch.setattr("proxbox_api.ceph.v2_routes.validate_source", fake_validate)
    probe = prom_client.post(f"/ceph/v2/metrics/sources/{source_id}/validate")
    assert probe.status_code == 200
    assert probe.json() == {"id": source_id, "ok": True, "error": None}


def test_metrics_returns_typed_snapshot_when_source_configured(prom_client, monkeypatch) -> None:
    prom_client.post(
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
    resp = prom_client.get(
        "/ceph/v2/metrics", params={"provider": "prometheus", "object_ref": "cluster:1"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["snapshot"]["cluster_health"] == "HEALTH_WARN"
    assert body["snapshot"]["osd_up"] == 5
    # the source config must not leak into the echoed scope
    assert "prometheus_source" not in body["scope"]


def test_prometheus_listed_in_capabilities(prom_client) -> None:
    resp = prom_client.get("/ceph/v2/capabilities", params={"provider": "prometheus"})
    assert resp.status_code == 200
    providers = resp.json()["providers"]
    assert providers[0]["provider"] == "prometheus"
    assert providers[0]["supported"] is True
    assert providers[0]["metrics"] is True
