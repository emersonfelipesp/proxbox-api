"""Tests for the standalone orphan VM sweep routes (REST and SSE)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from proxbox_api.routes.virtualization.virtual_machines import orphans_vm
from proxbox_api.services.sync import orphan_sweep
from proxbox_api.session.proxmox_providers import (
    ProxmoxPartialSessions,
    proxmox_sessions_partial,
)

REST_PATH = "/virtualization/virtual-machines/orphans/sweep"
STREAM_PATH = f"{REST_PATH}/stream"


def _install_sweep(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _fake_sweep(_nb: object, **kwargs: Any) -> dict[str, object]:
        calls.append(kwargs)
        return {
            "enabled": kwargs["enabled"],
            "run_id": kwargs["run_id"],
            "dry_run": kwargs["dry_run"],
            "candidates": 0,
            "skipped_reason": None,
        }

    monkeypatch.setattr(orphans_vm, "run_orphan_vm_sweep", _fake_sweep)
    return calls


def _sse_events(payload: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in payload.split("\n\n"):
        name: str | None = None
        data: str | None = None
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            if line.startswith("data: "):
                data = line.removeprefix("data: ")
        if name and data:
            events.append((name, json.loads(data)))
    return events


def test_rest_route_forwards_run_scope_and_flags(auth_test_client, monkeypatch) -> None:
    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "true")
    calls = _install_sweep(monkeypatch)

    response = auth_test_client.get(
        REST_PATH,
        params={
            "run_id": "run-1",
            "dry_run": "true",
            "vm_stage_failed": "true",
            "proxmox_endpoint_ids": "3, 5",
        },
    )

    assert response.status_code == 200
    assert response.json()["run_id"] == "run-1"
    assert len(calls) == 1
    assert calls[0]["run_id"] == "run-1"
    assert calls[0]["enabled"] is True
    assert calls[0]["dry_run"] is True
    assert calls[0]["vm_stage_failed"] is True
    assert calls[0]["endpoint_ids"] == frozenset({3, 5})


def test_rest_route_defaults_are_unscoped_and_not_failed(auth_test_client, monkeypatch) -> None:
    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "true")
    calls = _install_sweep(monkeypatch)

    response = auth_test_client.get(REST_PATH, params={"run_id": "run-2", "dry_run": "true"})

    assert response.status_code == 200
    assert calls[0]["vm_stage_failed"] is False
    assert calls[0]["endpoint_ids"] is None


@pytest.mark.parametrize("path", [REST_PATH, STREAM_PATH])
@pytest.mark.parametrize("extra", [{}, {"vm_stage_failed": "true"}])
def test_live_sweep_without_endpoint_scope_is_rejected(
    auth_test_client, monkeypatch, path, extra
) -> None:
    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "true")
    calls = _install_sweep(monkeypatch)

    response = auth_test_client.get(path, params={"run_id": "run-2b", **extra})

    assert response.status_code == 422
    assert "endpoint_ids" in response.json()["detail"]
    assert calls == []


def test_rest_route_endpoint_ids_alias_takes_precedence(auth_test_client, monkeypatch) -> None:
    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "true")
    calls = _install_sweep(monkeypatch)

    auth_test_client.get(
        REST_PATH,
        params={"run_id": "run-3", "endpoint_ids": "1", "proxmox_endpoint_ids": "2"},
    )
    auth_test_client.get(REST_PATH, params={"run_id": "run-3", "endpoint_ids": "1"})

    assert [call["endpoint_ids"] for call in calls] == [frozenset({2}), frozenset({1})]


@pytest.mark.parametrize("path", [REST_PATH, STREAM_PATH])
def test_routes_require_a_non_empty_run_id(auth_test_client, monkeypatch, path) -> None:
    calls = _install_sweep(monkeypatch)

    assert auth_test_client.get(path).status_code == 422
    assert auth_test_client.get(path, params={"run_id": ""}).status_code == 422
    assert calls == []


@pytest.mark.parametrize("path", [REST_PATH, STREAM_PATH])
def test_routes_reject_malformed_endpoint_ids_before_sweeping(
    auth_test_client, monkeypatch, path
) -> None:
    calls = _install_sweep(monkeypatch)

    response = auth_test_client.get(path, params={"run_id": "run-4", "endpoint_ids": "1,abc"})

    assert response.status_code >= 400
    assert response.headers["content-type"].startswith("application/json")
    assert calls == []


def test_rest_route_with_setting_off_reports_disabled_and_never_patches(
    auth_test_client, monkeypatch
) -> None:
    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "false")

    async def _unexpected(*_args: Any, **_kwargs: Any) -> object:
        raise AssertionError("a disabled sweep must not scan or write")

    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _unexpected)
    monkeypatch.setattr(orphan_sweep, "scan_vm_sidecar_orphan_candidates", _unexpected)

    response = auth_test_client.get(REST_PATH, params={"run_id": "run-5", "endpoint_ids": "1"})

    body = response.json()
    assert response.status_code == 200
    assert body["enabled"] is False
    assert body["skipped_reason"] == "disabled"
    assert body["soft_deleted"] == 0


def test_rest_route_dry_run_previews_even_when_setting_is_off(
    auth_test_client, monkeypatch
) -> None:
    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "false")
    calls = _install_sweep(monkeypatch)

    response = auth_test_client.get(REST_PATH, params={"run_id": "run-6", "dry_run": "true"})

    assert response.status_code == 200
    assert calls[0]["enabled"] is False
    assert calls[0]["dry_run"] is True


def test_rest_route_vm_stage_failure_skips_without_scan_or_writes(
    auth_test_client, monkeypatch
) -> None:
    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "true")

    async def _unexpected(*_args: Any, **_kwargs: Any) -> object:
        raise AssertionError("a failed VM stage must not scan or write")

    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _unexpected)
    monkeypatch.setattr(orphan_sweep, "scan_vm_sidecar_orphan_candidates", _unexpected)

    response = auth_test_client.get(
        REST_PATH, params={"run_id": "run-7", "endpoint_ids": "1", "vm_stage_failed": "true"}
    )

    assert response.status_code == 200
    assert response.json()["skipped_reason"] == "vm_stage_failed"


def test_stream_route_forwards_arguments_and_completes(auth_test_client, monkeypatch) -> None:
    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "true")
    calls = _install_sweep(monkeypatch)

    response = auth_test_client.get(
        STREAM_PATH,
        params={"run_id": "run-8", "endpoint_ids": "7", "vm_stage_failed": "true"},
    )

    events = _sse_events(response.text)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert calls[0]["run_id"] == "run-8"
    assert calls[0]["endpoint_ids"] == frozenset({7})
    assert calls[0]["vm_stage_failed"] is True
    assert calls[0]["stream"] is not None
    step_names = [(name, data.get("status")) for name, data in events if name == "step"]
    assert step_names == [("step", "started"), ("step", "completed")]
    complete = [data for name, data in events if name == "complete"]
    assert complete[0]["ok"] is True
    assert complete[0]["result"]["run_id"] == "run-8"


def test_stream_route_reports_skipped_reason_end_to_end(auth_test_client, monkeypatch) -> None:
    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "true")

    async def _unexpected(*_args: Any, **_kwargs: Any) -> object:
        raise AssertionError("a failed VM stage must not scan or write")

    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _unexpected)
    monkeypatch.setattr(orphan_sweep, "scan_vm_sidecar_orphan_candidates", _unexpected)

    response = auth_test_client.get(
        STREAM_PATH, params={"run_id": "run-9", "endpoint_ids": "1", "vm_stage_failed": "true"}
    )

    complete = [data for name, data in _sse_events(response.text) if name == "complete"]
    assert complete[0]["ok"] is True
    assert complete[0]["result"]["skipped_reason"] == "vm_stage_failed"


def _install_sessions(
    auth_test_client,
    monkeypatch: pytest.MonkeyPatch,
    *,
    endpoint_ids: tuple[int, ...] = (1,),
    failures: tuple[object, ...] = (),
    resources: object = None,
    resources_error: Exception | None = None,
) -> None:
    async def _partial() -> ProxmoxPartialSessions:
        sessions = [SimpleNamespace(db_endpoint_id=eid) for eid in endpoint_ids]
        return ProxmoxPartialSessions(sessions=sessions, failures=list(failures))  # type: ignore[arg-type]

    async def _resources(_sessions: object) -> object:
        if resources_error is not None:
            raise resources_error
        return resources

    overrides = auth_test_client.app.dependency_overrides
    overrides[proxmox_sessions_partial] = _partial
    monkeypatch.setattr(orphans_vm, "cluster_resources", _resources)
    monkeypatch.setattr(orphans_vm, "close_proxmox_sessions", lambda _s: _noop())
    try:
        yield
    finally:
        overrides.pop(proxmox_sessions_partial, None)


async def _noop() -> None:
    return None


@pytest.fixture
def live_inventory_ok(auth_test_client, monkeypatch):
    gen = _install_sessions(
        auth_test_client,
        monkeypatch,
        resources=[{"Lab": [{"vmid": 101, "type": "qemu"}]}],
    )
    next(gen)
    yield
    gen.close()


def test_live_sweep_route_builds_live_keys_from_scoped_sessions(
    auth_test_client, monkeypatch, live_inventory_ok
) -> None:
    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "true")
    calls = _install_sweep(monkeypatch)

    for path in (REST_PATH, STREAM_PATH):
        auth_test_client.get(path, params={"run_id": "r", "endpoint_ids": "1"})

    assert len(calls) == 2
    for call in calls:
        assert call["live_vm_keys"] == frozenset({("lab", 101, "qemu")})
        assert call["live_inventory_unavailable"] is False


def test_dry_run_route_does_not_fetch_live_inventory(auth_test_client, monkeypatch) -> None:
    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "true")
    calls = _install_sweep(monkeypatch)

    auth_test_client.get(REST_PATH, params={"run_id": "r", "dry_run": "true"})

    assert calls[0]["live_vm_keys"] is None
    assert calls[0]["live_inventory_unavailable"] is False


@pytest.mark.parametrize(
    "case",
    [
        {"failures": (object(),)},
        {"endpoint_ids": ()},
        {"endpoint_ids": (1,), "scope": "1,2"},
        {"resources_error": RuntimeError("proxmox down")},
    ],
    ids=["session_failed", "no_sessions", "scoped_session_missing", "fetch_error"],
)
@pytest.mark.parametrize("path", [REST_PATH, STREAM_PATH])
def test_live_sweep_fails_closed_when_inventory_cannot_be_verified(
    auth_test_client, monkeypatch, path, case
) -> None:
    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "true")
    scope = case.get("scope", "1")
    options = {k: v for k, v in case.items() if k != "scope"}
    gen = _install_sessions(auth_test_client, monkeypatch, resources=[], **options)
    next(gen)
    try:
        calls = _install_sweep(monkeypatch)
        auth_test_client.get(path, params={"run_id": "r", "endpoint_ids": scope})
    finally:
        gen.close()

    assert calls[0]["live_inventory_unavailable"] is True
    assert calls[0]["live_vm_keys"] is None


def test_unavailable_inventory_end_to_end_never_patches(auth_test_client, monkeypatch) -> None:
    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "true")

    async def _unexpected(*_args: Any, **_kwargs: Any) -> object:
        raise AssertionError("an unverifiable sweep must not scan or write")

    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _unexpected)
    monkeypatch.setattr(orphan_sweep, "scan_vm_sidecar_orphan_candidates", _unexpected)
    gen = _install_sessions(auth_test_client, monkeypatch, failures=(object(),))
    next(gen)
    try:
        response = auth_test_client.get(REST_PATH, params={"run_id": "r", "endpoint_ids": "1"})
    finally:
        gen.close()

    assert response.json()["skipped_reason"] == "live_inventory_unavailable"
