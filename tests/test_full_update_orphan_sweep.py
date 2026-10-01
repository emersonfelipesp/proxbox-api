"""Full-update orphan sweep wiring: endpoint scope and VM-stage failure handling."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from proxbox_api.app import full_update
from proxbox_api.routes.virtualization.virtual_machines.sync_vm import SyncResultList
from proxbox_api.services.netbox_bootstrap import BootstrapStatus

_TAG = SimpleNamespace(id=1, name="Proxbox", slug="proxbox", color="ff5722")


_FRESH: dict[str, Any] = {"resources": None, "error": None}


async def _fresh_fetch(_pxs: object) -> object:
    if _FRESH["error"] is not None:
        raise _FRESH["error"]
    return _LIVE_RESOURCES if _FRESH["resources"] is None else _FRESH["resources"]


@pytest.fixture(autouse=True)
def _reset_fresh() -> None:
    _FRESH.update(resources=None, error=None)


def _stub_stages(
    monkeypatch: pytest.MonkeyPatch,
    *,
    vm_result: list[dict],
    sweep_calls: list[dict[str, Any]] | None = None,
) -> None:
    """Stub every stage; the VM stage returns ``vm_result``.

    The sweep is replaced by a recorder appending to ``sweep_calls`` unless it is ``None``,
    in which case the real ``run_orphan_vm_sweep`` runs.
    """

    async def _vm_stage(**_kwargs: Any) -> list[dict]:
        return vm_result

    async def _sweep(_nb: object, **kwargs: Any) -> dict[str, object]:
        assert sweep_calls is not None
        sweep_calls.append(kwargs)
        return {"enabled": True, "skipped_reason": None}

    empty_list_stages = (
        "create_proxmox_devices",
        "create_storages",
        "create_all_virtual_machine_backups",
        "_create_all_virtual_machine_backups",
        "create_all_device_interfaces",
        "create_only_vm_interfaces",
        "create_only_vm_ip_addresses",
    )
    summary_stages = {
        "create_virtual_disks": {"count": 0},
        "create_all_virtual_machine_snapshots": {"count": 0},
        "_create_all_virtual_machine_snapshots": {"count": 0},
        "sync_all_virtual_machine_task_histories": {"count": 0},
        "sync_all_replications": {"created": 0, "updated": 0},
        "sync_all_backup_routines": {"created": 0, "updated": 0},
    }
    for name in empty_list_stages:
        monkeypatch.setattr(
            f"proxbox_api.app.full_update.{name}",
            lambda **_kw: asyncio.sleep(0, result=[]),
        )
    for name, result in summary_stages.items():
        monkeypatch.setattr(
            f"proxbox_api.app.full_update.{name}",
            lambda result=result, **_kw: asyncio.sleep(0, result=result),
        )
    monkeypatch.setattr(full_update, "create_virtual_machines", _vm_stage)
    monkeypatch.setattr(full_update, "fetch_cluster_resources", _fresh_fetch)
    if sweep_calls is not None:
        monkeypatch.setattr(full_update, "run_orphan_vm_sweep", _sweep)


_LIVE_RESOURCES = [
    {"Lab": [{"vmid": 101, "type": "qemu"}, {"vmid": 7, "type": "lxc"}, {"type": "node"}]}
]


def _run_stream(**kwargs: Any) -> str:
    async def _run() -> str:
        response = await full_update.full_update_sync_stream(
            _sync_deps=BootstrapStatus(),
            netbox_session=object(),
            pxs=[],
            cluster_status=[],
            cluster_resources=_LIVE_RESOURCES,
            tag=_TAG,
            dry_run=False,
            **kwargs,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
        return "".join(chunks)

    return asyncio.run(_run())


def _run_rest(**kwargs: Any) -> dict:
    return asyncio.run(
        full_update.full_update_sync(
            netbox_session=object(),
            _sync_deps=BootstrapStatus(),
            pxs=[],
            cluster_status=[],
            cluster_resources=_LIVE_RESOURCES,
            tag=_TAG,
            **kwargs,
        )
    )


@pytest.fixture(autouse=True)
def _enable_orphan_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "true")


@pytest.mark.parametrize("runner", ["rest", "stream"])
def test_full_update_sweep_receives_the_endpoint_scope(monkeypatch, runner) -> None:
    calls: list[dict[str, Any]] = []
    _stub_stages(monkeypatch, vm_result=SyncResultList([{"id": 7}]), sweep_calls=calls)
    scope = frozenset({3, 4})

    if runner == "rest":
        _run_rest(sweep_endpoint_ids=scope)
    else:
        _run_stream(sweep_endpoint_ids=scope)

    assert len(calls) == 1
    assert calls[0]["endpoint_ids"] == scope
    assert calls[0]["vm_stage_failed"] is False
    assert calls[0]["touched_vm_ids"] == {7}


@pytest.mark.parametrize("runner", ["rest", "stream"])
def test_full_update_sweep_is_unscoped_by_default(monkeypatch, runner) -> None:
    calls: list[dict[str, Any]] = []
    _stub_stages(monkeypatch, vm_result=SyncResultList(), sweep_calls=calls)

    _run_rest() if runner == "rest" else _run_stream()

    assert calls[0]["endpoint_ids"] is None


@pytest.mark.parametrize("runner", ["rest", "stream"])
def test_full_update_tells_the_sweep_when_the_vm_stage_had_failures(monkeypatch, runner) -> None:
    calls: list[dict[str, Any]] = []
    _stub_stages(
        monkeypatch,
        vm_result=SyncResultList([{"id": 1}], failed_count=2),
        sweep_calls=calls,
    )

    _run_rest() if runner == "rest" else _run_stream()

    assert calls[0]["vm_stage_failed"] is True


def test_full_update_stream_reports_the_sweep_skip_reason_from_the_real_sweep(
    monkeypatch,
) -> None:
    """End to end through the real sweep: a failed VM stage means no scan and no PATCH."""
    from proxbox_api.services.sync import orphan_sweep

    async def _unexpected(*_args: Any, **_kwargs: Any) -> object:
        raise AssertionError("a failed VM stage must not scan or write")

    _stub_stages(monkeypatch, vm_result=SyncResultList([{"id": 1}], failed_count=1))
    monkeypatch.setattr(orphan_sweep, "scan_vm_sidecar_orphan_candidates", _unexpected)
    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _unexpected)

    payload = _run_stream()

    complete = [
        json.loads(line.removeprefix("data: "))
        for frame in payload.split("\n\n")
        if frame.startswith("event: complete")
        for line in frame.splitlines()
        if line.startswith("data: ")
    ]
    assert complete[0]["result"]["orphan_sweep"]["skipped_reason"] == "vm_stage_failed"


def _request(**query: str) -> Any:
    return SimpleNamespace(query_params=query)


def _sessions(*endpoint_ids: int | None) -> list[Any]:
    return [SimpleNamespace(db_endpoint_id=endpoint_id) for endpoint_id in endpoint_ids]


@pytest.mark.parametrize(
    ("query", "session_ids", "expected"),
    [
        ({}, (1, 2), None),
        ({"endpoint_ids": ""}, (1, 2), None),
        ({"proxmox_endpoint_ids": "  "}, (1, 2), None),
        ({"proxmox_endpoint_ids": "2"}, (2,), frozenset({2})),
        ({"endpoint_ids": "1,2"}, (1, 2), frozenset({1, 2})),
        ({"name": "pve-a"}, (5,), frozenset({5})),
        ({"domain": "pve.example"}, (5,), frozenset({5})),
        ({"ip_address": "192.0.2.10"}, (6,), frozenset({6})),
        # Sessions that carry no endpoint id cannot be attributed and are left out.
        ({"proxmox_endpoint_ids": "1"}, (1, None), frozenset({1})),
        # A scoped run that acquired no session may sweep nothing.
        ({"proxmox_endpoint_ids": "9"}, (), frozenset()),
    ],
)
def test_orphan_sweep_endpoint_scope_dependency(query, session_ids, expected) -> None:
    scope = full_update.orphan_sweep_endpoint_scope(_request(**query), _sessions(*session_ids))

    assert scope == expected


@pytest.mark.parametrize("path", ["/full-update", "/full-update/stream"])
def test_full_update_http_scoped_run_scopes_the_sweep(auth_test_client, monkeypatch, path) -> None:
    from proxbox_api.dependencies import ensure_netbox_sync_dependencies, proxbox_tag
    from proxbox_api.main import app
    from proxbox_api.routes.proxmox.cluster import cluster_resources, cluster_status
    from proxbox_api.session.proxmox_providers import proxmox_sessions_dep

    async def _fake_bootstrap() -> BootstrapStatus:
        return BootstrapStatus()

    calls: list[dict[str, Any]] = []
    _stub_stages(monkeypatch, vm_result=SyncResultList(), sweep_calls=calls)
    app.dependency_overrides[ensure_netbox_sync_dependencies] = _fake_bootstrap
    app.dependency_overrides[proxmox_sessions_dep] = lambda: _sessions(4)
    app.dependency_overrides[cluster_status] = lambda: []
    app.dependency_overrides[cluster_resources] = lambda: []
    app.dependency_overrides[proxbox_tag] = lambda: _TAG

    scoped = auth_test_client.get(f"{path}?proxmox_endpoint_ids=4")
    unscoped = auth_test_client.get(path)

    assert scoped.status_code == unscoped.status_code == 200
    assert [call["endpoint_ids"] for call in calls] == [frozenset({4}), None]


@pytest.mark.parametrize("runner", ["rest", "stream"])
def test_full_update_sweep_receives_live_vm_keys_from_cluster_resources(
    monkeypatch, runner
) -> None:
    calls: list[dict[str, Any]] = []
    _stub_stages(monkeypatch, vm_result=SyncResultList(), sweep_calls=calls)

    _run_rest() if runner == "rest" else _run_stream()

    assert calls[0]["live_vm_keys"] == frozenset({("lab", 101, "qemu"), ("lab", 7, "lxc")})


@pytest.mark.parametrize("runner", ["rest", "stream"])
def test_full_update_sweep_uses_fresh_inventory_not_the_request_snapshot(
    monkeypatch, runner
) -> None:
    """A guest created after the request's resource fetch is present in the fresh one."""
    calls: list[dict[str, Any]] = []
    _stub_stages(monkeypatch, vm_result=SyncResultList(), sweep_calls=calls)
    _FRESH["resources"] = [{"Lab": [{"vmid": 101, "type": "qemu"}, {"vmid": 900, "type": "qemu"}]}]

    _run_rest() if runner == "rest" else _run_stream()

    assert ("lab", 900, "qemu") in calls[0]["live_vm_keys"]
    assert calls[0]["live_inventory_unavailable"] is False


@pytest.mark.parametrize("runner", ["rest", "stream"])
@pytest.mark.parametrize(
    "failure",
    [
        ("error", RuntimeError("proxmox down")),
        ("resources", [{"Lab": [{"type": "qemu"}]}]),
    ],
)
def test_full_update_sweep_fails_closed_when_fresh_inventory_unavailable(
    monkeypatch, runner, failure
) -> None:
    calls: list[dict[str, Any]] = []
    _stub_stages(monkeypatch, vm_result=SyncResultList(), sweep_calls=calls)
    _FRESH[failure[0]] = failure[1]

    _run_rest() if runner == "rest" else _run_stream()

    assert calls[0]["live_vm_keys"] is None
    assert calls[0]["live_inventory_unavailable"] is True


def test_full_update_real_sweep_skips_with_live_inventory_unavailable(monkeypatch) -> None:
    from proxbox_api.services.sync import orphan_sweep

    async def _unexpected(*_args: Any, **_kwargs: Any) -> object:
        raise AssertionError("an unavailable fresh inventory must not scan or write")

    _stub_stages(monkeypatch, vm_result=SyncResultList())
    monkeypatch.setattr(orphan_sweep, "scan_vm_sidecar_orphan_candidates", _unexpected)
    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _unexpected)
    _FRESH["error"] = RuntimeError("proxmox down")

    result = _run_rest()

    assert result["orphan_sweep"]["skipped_reason"] == "live_inventory_unavailable"
