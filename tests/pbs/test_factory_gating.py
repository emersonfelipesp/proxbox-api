"""When PROXBOX_FEATURES=pbs, only /pbs/* (plus root/auth) are mounted."""

from __future__ import annotations


def test_default_app_mounts_pbs_alongside_proxmox():
    from proxbox_api.app.factory import create_app

    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/pbs/status" in paths
    assert "/pbs/endpoints" in paths
    # Sanity: non-PBS surface is also present in the default build.
    assert "/proxmox/endpoints" in paths


def test_pbs_only_feature_flag_hides_other_routers(monkeypatch):
    monkeypatch.setenv("PROXBOX_FEATURES", "pbs")
    from proxbox_api.app.factory import create_app

    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/pbs/status" in paths
    assert "/pbs/endpoints" in paths
    assert "/proxmox/endpoints" not in paths
    assert "/dcim" not in {p for p in paths if p.startswith("/dcim")}
