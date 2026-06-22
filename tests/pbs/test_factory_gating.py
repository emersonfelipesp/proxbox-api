"""When PROXBOX_FEATURES=pbs, only /pbs/* (plus root/auth) are mounted."""

from __future__ import annotations


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


def test_default_app_mounts_pbs_alongside_proxmox():
    from proxbox_api.app.factory import create_app

    app = create_app()
    paths = _registered_paths(app)
    assert "/pbs/status" in paths
    assert "/pbs/endpoints" in paths
    # Sanity: non-PBS surface is also present in the default build.
    assert "/proxmox/endpoints" in paths
    assert "/full-update" in paths
    assert "/cache" in paths
    assert "/ws" in paths


def test_pbs_only_feature_flag_hides_other_routers(monkeypatch):
    monkeypatch.setenv("PROXBOX_FEATURES", "pbs")
    from proxbox_api.app.factory import create_app

    app = create_app()
    paths = _registered_paths(app)
    assert "/pbs/status" in paths
    assert "/pbs/endpoints" in paths
    assert "/proxmox/endpoints" not in paths
    assert "/dcim" not in {p for p in paths if p.startswith("/dcim")}
    assert "/full-update" not in paths
    assert "/cache" not in paths
    assert "/ws" not in paths
