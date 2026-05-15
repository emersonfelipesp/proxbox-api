"""Factory feature-flag tests for the read-only Ceph surface."""

from __future__ import annotations


def test_default_app_mounts_ceph_alongside_existing_surfaces():
    from proxbox_api.app.factory import create_app

    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/ceph/status" in paths
    assert "/ceph/sync/full" in paths
    assert "/pbs/status" in paths
    assert "/proxmox/endpoints" in paths
    assert "/full-update" in paths


def test_ceph_only_feature_flag_hides_other_feature_and_core_routers(monkeypatch):
    monkeypatch.setenv("PROXBOX_FEATURES", "ceph")
    from proxbox_api.app.factory import create_app

    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/ceph/status" in paths
    assert "/ceph/sync/full" in paths
    assert "/pbs/status" not in paths
    assert "/proxmox/endpoints" not in paths
    assert "/full-update" not in paths
    assert "/cache" not in paths
    assert "/ws" not in paths


def test_pbs_ceph_feature_flag_mounts_only_sidecar_routers(monkeypatch):
    monkeypatch.setenv("PROXBOX_FEATURES", "pbs,ceph")
    from proxbox_api.app.factory import create_app

    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/pbs/status" in paths
    assert "/pbs/endpoints" in paths
    assert "/ceph/status" in paths
    assert "/ceph/sync/full" in paths
    assert "/proxmox/endpoints" not in paths
    assert "/full-update" not in paths
