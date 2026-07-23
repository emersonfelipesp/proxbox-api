"""Factory feature-flag tests for the read-only Ceph surface."""

from __future__ import annotations

import subprocess
import sys


def test_ceph_v2_endpoint_binding_and_routes_import_in_a_cold_process():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from proxbox_api.ceph.endpoint_binding import BoundProxmoxSession; "
                "from proxbox_api.ceph.v2_routes import router; "
                "assert BoundProxmoxSession and router"
            ),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr + result.stdout


def _join_mount_path(prefix: str, path: str) -> str:
    if not prefix:
        return path
    if path == "/":
        return prefix
    return f"{prefix.rstrip('/')}/{path.lstrip('/')}"


def _registered_paths(app) -> set[str]:
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            paths.add(path)
        include_context = getattr(route, "include_context", None)
        prefix = getattr(include_context, "prefix", "") or ""
        original_router = getattr(route, "original_router", None)
        for original_route in getattr(original_router, "routes", ()) or ():
            original_path = getattr(original_route, "path", None)
            if isinstance(original_path, str):
                paths.add(_join_mount_path(prefix, original_path))
        effective_route_contexts = getattr(route, "effective_route_contexts", None)
        if callable(effective_route_contexts):
            paths.update(
                context.path
                for context in effective_route_contexts()
                if isinstance(getattr(context, "path", None), str)
            )
    return paths


def test_default_app_mounts_ceph_alongside_existing_surfaces():
    from proxbox_api.app.factory import create_app

    app = create_app()
    paths = _registered_paths(app)
    assert "/ceph/status" in paths
    assert "/ceph/v2/capabilities" in paths
    assert "/ceph/sync/full" in paths
    assert "/ceph/sync/rgw" in paths
    assert "/ceph/sync/rbd" in paths
    assert "/pbs/status" in paths
    assert "/proxmox/endpoints" in paths
    assert "/full-update" in paths


def test_ceph_only_feature_flag_hides_other_feature_and_core_routers(monkeypatch):
    monkeypatch.setenv("PROXBOX_FEATURES", "ceph")
    from proxbox_api.app.factory import create_app

    app = create_app()
    paths = _registered_paths(app)
    assert "/ceph/status" in paths
    assert "/ceph/v2/capabilities" in paths
    assert "/ceph/sync/full" in paths
    assert "/ceph/sync/rgw" in paths
    assert "/ceph/sync/rbd" in paths
    assert "/pbs/status" not in paths
    assert "/proxmox/endpoints" not in paths
    assert "/full-update" not in paths
    assert "/cache" not in paths
    assert "/ws" not in paths


def test_pbs_ceph_feature_flag_mounts_only_sidecar_routers(monkeypatch):
    monkeypatch.setenv("PROXBOX_FEATURES", "pbs,ceph")
    from proxbox_api.app.factory import create_app

    app = create_app()
    paths = _registered_paths(app)
    assert "/pbs/status" in paths
    assert "/pbs/endpoints" in paths
    assert "/ceph/status" in paths
    assert "/ceph/v2/capabilities" in paths
    assert "/ceph/sync/full" in paths
    assert "/ceph/sync/rgw" in paths
    assert "/ceph/sync/rbd" in paths
    assert "/proxmox/endpoints" not in paths
    assert "/full-update" not in paths
