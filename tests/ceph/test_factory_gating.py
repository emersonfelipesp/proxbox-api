"""Factory feature-flag tests for the read-only Ceph surface."""

from __future__ import annotations

import subprocess
import sys

import pytest


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


def _prepare_fast_lifespan(monkeypatch) -> None:
    from proxbox_api.app import bootstrap, factory

    async def _skip_netbox_object_bootstrap(app) -> None:  # noqa: ARG001
        return None

    monkeypatch.setenv("PROXBOX_SKIP_NETBOX_BOOTSTRAP", "1")
    monkeypatch.setattr(factory, "register_generated_proxmox_routes", lambda app: None)
    monkeypatch.setattr(factory, "_run_bootstrap_pass", _skip_netbox_object_bootstrap)
    monkeypatch.setattr(bootstrap, "_configure_backend_file_logging", lambda: None)


async def test_ceph_task_claim_collision_refuses_application_startup(monkeypatch):
    """Ambiguous legacy task evidence must prevent the app from ever serving."""

    from proxbox_api.app import bootstrap, factory
    from proxbox_api.database import CephProviderTaskClaimMigrationError

    reason = "ceph_provider_task_claim_cross_endpoint_collision"

    def _raise_collision(environ=None) -> None:
        raise CephProviderTaskClaimMigrationError(reason)

    _prepare_fast_lifespan(monkeypatch)
    monkeypatch.setattr(bootstrap, "initialize_database_and_schema", _raise_collision)
    application = factory.create_app()

    with pytest.raises(CephProviderTaskClaimMigrationError, match=f"^{reason}$"):
        async with factory._lifespan(application):
            pytest.fail("The application served traffic despite a task-claim collision")

    assert bootstrap.init_ok is False
    assert bootstrap.last_init_error == reason


async def test_ceph_task_claim_collision_is_not_downgraded_to_a_startup_error(monkeypatch):
    """The generic schema-failure translation must not mask a claim collision."""

    from proxbox_api.app import bootstrap, factory
    from proxbox_api.database import (
        CephProviderTaskClaimMigrationError,
        DatabaseStartupError,
    )

    reason = "ceph_provider_task_claim_cross_endpoint_collision"

    def _raise_collision(environ=None) -> None:
        raise CephProviderTaskClaimMigrationError(reason)

    _prepare_fast_lifespan(monkeypatch)
    monkeypatch.setattr(bootstrap, "initialize_database_and_schema", _raise_collision)
    application = factory.create_app()

    with pytest.raises(CephProviderTaskClaimMigrationError) as excinfo:
        async with factory._lifespan(application):
            pytest.fail("The application served traffic despite a task-claim collision")

    assert not isinstance(excinfo.value, DatabaseStartupError)
    assert bootstrap.last_init_error == reason


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
