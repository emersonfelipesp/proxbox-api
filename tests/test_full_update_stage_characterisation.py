"""Characterisation tests for the full-update stage orchestration contract."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from proxbox_api.app import full_update
from proxbox_api.exception import ProxboxException
from proxbox_api.services.netbox_bootstrap import BootstrapStatus

_OPERATION_ID = "00000000-0000-0000-0000-000000000522"
_TAG = type(
    "Tag",
    (),
    {"id": 1, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"},
)()


@dataclass(frozen=True)
class StageCase:
    name: str
    rest_target: str
    stream_target: str
    rest_error_message: str
    rest_log_message: str


STAGES = (
    StageCase(
        "devices",
        "create_proxmox_devices",
        "create_proxmox_devices",
        "Error while syncing nodes.",
        "Error while syncing nodes during full-update",
    ),
    StageCase(
        "storage",
        "create_storages",
        "create_storages",
        "Error while syncing storages.",
        "Error while syncing storages during full-update",
    ),
    StageCase(
        "virtual-machines",
        "create_virtual_machines",
        "create_virtual_machines",
        "Error while syncing virtual machines.",
        "Error while syncing virtual machines during full-update",
    ),
    StageCase(
        "virtual-disks",
        "create_virtual_disks",
        "create_virtual_disks",
        "Error while syncing virtual disks.",
        "Error while syncing virtual disks during full-update",
    ),
    StageCase(
        "task-history",
        "sync_all_virtual_machine_task_histories",
        "sync_all_virtual_machine_task_histories",
        "Error while syncing task history.",
        "Error while syncing task history during full-update",
    ),
    StageCase(
        "backups",
        "create_all_virtual_machine_backups",
        "_create_all_virtual_machine_backups",
        "Error while syncing backups.",
        "Error while syncing backups during full-update",
    ),
    StageCase(
        "snapshots",
        "create_all_virtual_machine_snapshots",
        "_create_all_virtual_machine_snapshots",
        "Error while syncing snapshots.",
        "Error while syncing snapshots during full-update",
    ),
    StageCase(
        "node-interfaces",
        "create_all_device_interfaces",
        "create_all_device_interfaces",
        "Error while syncing node interfaces.",
        "Error while syncing node interfaces during full-update",
    ),
    StageCase(
        "vm-interfaces",
        "create_only_vm_interfaces",
        "create_only_vm_interfaces",
        "Error while syncing VM interfaces.",
        "Error while syncing VM interfaces during full-update",
    ),
    StageCase(
        "vm-ip-addresses",
        "create_only_vm_ip_addresses",
        "create_only_vm_ip_addresses",
        "Error while syncing VM IP addresses.",
        "Error while syncing VM IP addresses during full-update",
    ),
    StageCase(
        "replications",
        "sync_all_replications",
        "sync_all_replications",
        "Error while syncing replications.",
        "Error while syncing replications during full-update",
    ),
    StageCase(
        "backup-routines",
        "sync_all_backup_routines",
        "sync_all_backup_routines",
        "Error while syncing backup routines.",
        "Error while syncing backup routines during full-update",
    ),
    StageCase(
        "sweep-orphans",
        "run_orphan_vm_sweep",
        "run_orphan_vm_sweep",
        "Error while sweeping orphan virtual machines.",
        "Error while sweeping orphan virtual machines during full-update",
    ),
)

REST_ORDER = (
    "devices",
    "storage",
    "virtual-machines",
    "task-history",
    "virtual-disks",
    "backups",
    "snapshots",
    "node-interfaces",
    "vm-interfaces",
    "vm-ip-addresses",
    "replications",
    "backup-routines",
    "sweep-orphans",
)
STREAM_ORDER = (
    "devices",
    "storage",
    "virtual-machines",
    "virtual-disks",
    "task-history",
    "backups",
    "snapshots",
    "node-interfaces",
    "vm-interfaces",
    "vm-ip-addresses",
    "replications",
    "backup-routines",
    "sweep-orphans",
)

_REST_RESULTS: dict[str, object] = {
    "devices": [],
    "storage": [{"id": "storage-1"}],
    "virtual-machines": {
        "virtual_machines": [{"id": "vm-1"}],
        "failed_count": 0,
    },
    "virtual-disks": {"count": 2, "created": 2, "updated": 0},
    "task-history": {"created": 1, "skipped": 0},
    "backups": {"backups": [{"id": "backup-1"}]},
    "snapshots": {"created": 3, "skipped": 0},
    "node-interfaces": [{"id": "node-interface-1"}],
    "vm-interfaces": [{"id": "vm-interface-1"}, {"id": "vm-interface-2"}],
    "vm-ip-addresses": [{"id": "ip-1"}],
    "replications": {"created": 1, "updated": 2},
    "backup-routines": {"created": 2, "updated": 3},
    "sweep-orphans": {"enabled": True, "deleted": 4},
}
_STREAM_RESULTS = {**_REST_RESULTS, "backups": [{"id": "backup-1"}]}

_START_MESSAGES = {
    "devices": "Starting devices synchronization.",
    "storage": "Starting storage synchronization.",
    "virtual-machines": "Starting virtual machines synchronization.",
    "virtual-disks": "Starting virtual disks synchronization.",
    "task-history": "Starting task history synchronization.",
    "backups": "Starting backup synchronization.",
    "snapshots": "Starting snapshot synchronization.",
    "node-interfaces": "Starting node interfaces synchronization.",
    "vm-interfaces": "Starting VM interfaces synchronization.",
    "vm-ip-addresses": "Starting VM IP address synchronization.",
    "replications": "Starting replications synchronization.",
    "backup-routines": "Starting backup routines synchronization.",
    "sweep-orphans": "Starting orphan virtual machine sweep.",
}
_COMPLETED_MESSAGES = {
    "devices": "Devices synchronization finished.",
    "storage": "Storage synchronization finished.",
    "virtual-machines": "Virtual machines synchronization finished.",
    "virtual-disks": "Virtual disks synchronization finished.",
    "task-history": "Task history synchronization finished.",
    "backups": "Backup synchronization finished.",
    "snapshots": "Snapshot synchronization finished.",
    "node-interfaces": "Node interfaces synchronization finished.",
    "vm-interfaces": "VM interfaces synchronization finished.",
    "vm-ip-addresses": "VM IP address synchronization finished.",
    "replications": "Replications synchronization finished.",
    "backup-routines": "Backup routines synchronization finished.",
    "sweep-orphans": "Orphan virtual machine sweep finished.",
}
_STREAM_STEP_RESULTS = {
    "devices": {"count": 0},
    "storage": {"count": 1},
    "virtual-machines": {"count": 1},
    "virtual-disks": {"count": 2},
    "task-history": {"count": 1},
    "backups": {"count": 1},
    "snapshots": {"count": 3},
    "node-interfaces": {"count": 1},
    "vm-interfaces": {"count": 2},
    "vm-ip-addresses": {"count": 1},
    "replications": {"created": 1, "updated": 2},
    "backup-routines": {"created": 2, "updated": 3},
    "sweep-orphans": {"enabled": True, "deleted": 4},
}


def _stage_case(name: str) -> StageCase:
    return next(stage for stage in STAGES if stage.name == name)


def _stage_stub(
    name: str,
    result: object,
    calls: list[tuple[str, dict[str, Any]]],
) -> Callable[..., Awaitable[object]]:
    async def _stub(*_args: object, **kwargs: Any) -> object:
        calls.append((name, kwargs))
        return result

    return _stub


def _failure_stub(error: BaseException) -> Callable[..., Awaitable[object]]:
    async def _fail(*_args: object, **_kwargs: Any) -> object:
        raise error

    return _fail


def _install_stages(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner: str,
    failing_stage: str | None = None,
    error: BaseException | None = None,
    delete_orphans_enabled: bool = True,
) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    results = _REST_RESULTS if runner == "rest" else _STREAM_RESULTS
    target_attr = "rest_target" if runner == "rest" else "stream_target"
    for stage in STAGES:
        target = getattr(stage, target_attr)
        function = (
            _failure_stub(error)
            if stage.name == failing_stage and error is not None
            else _stage_stub(stage.name, results[stage.name], calls)
        )
        monkeypatch.setattr(full_update, target, function)

    monkeypatch.setattr(
        full_update,
        "is_delete_orphans_enabled",
        lambda: delete_orphans_enabled,
    )
    monkeypatch.setattr(full_update, "fetch_cluster_resources", _failure_stub(AssertionError()))

    async def _fresh_inventory(_pxs: object) -> dict[str, object]:
        return {"live_vm_keys": frozenset(), "live_inventory_unavailable": False}

    monkeypatch.setattr(full_update, "_fresh_sweep_inventory", _fresh_inventory)
    monkeypatch.setattr(full_update.uuid, "uuid4", lambda: _OPERATION_ID)
    monkeypatch.setattr(full_update.time, "monotonic", lambda: 100.0)
    return calls


def _run_rest() -> dict[str, object]:
    return asyncio.run(
        full_update.full_update_sync(
            netbox_session=object(),
            _sync_deps=BootstrapStatus(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            tag=_TAG,
            fetch_max_concurrency=5,
        )
    )


def _decode_sse(payload: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in payload.split("\n\n"):
        lines = frame.splitlines()
        event = next(
            (line.removeprefix("event: ") for line in lines if line.startswith("event: ")), None
        )
        data = next(
            (line.removeprefix("data: ") for line in lines if line.startswith("data: ")), None
        )
        if event is not None and data is not None:
            events.append((event, json.loads(data)))
    return events


def _run_stream(*, dry_run: bool = False) -> list[tuple[str, dict[str, Any]]]:
    async def _collect() -> str:
        response = await full_update.full_update_sync_stream(
            netbox_session=object(),
            _sync_deps=BootstrapStatus(created=["tag:characterisation"]),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            tag=_TAG,
            fetch_max_concurrency=5,
            dry_run=dry_run,
        )
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
        return "".join(chunks)

    return _decode_sse(asyncio.run(_collect()))


def _expected_stage_start(
    stage: str,
    *,
    delete_orphans_enabled: bool,
    dry_run: bool,
) -> dict[str, object]:
    message = _START_MESSAGES[stage]
    if stage == "sweep-orphans" and dry_run:
        message = "Previewing orphan virtual machine sweep."
    payload: dict[str, object] = {
        "step": stage,
        "status": "started",
        "message": message,
    }
    if stage == "sweep-orphans":
        payload["metadata"] = {
            "operation_id": _OPERATION_ID,
            "delete_orphans": delete_orphans_enabled,
            "dry_run": dry_run,
        }
    return payload


def _expected_stage_completion(stage: str, *, dry_run: bool) -> dict[str, object]:
    message = _COMPLETED_MESSAGES[stage]
    if stage == "sweep-orphans" and dry_run:
        message = "Orphan virtual machine sweep preview finished."
    return {
        "step": stage,
        "status": "completed",
        "message": message,
        "result": _STREAM_STEP_RESULTS[stage],
        "duration_seconds": 0.0,
    }


def _expected_stage_events(
    stages: tuple[str, ...],
    *,
    delete_orphans_enabled: bool,
    dry_run: bool,
) -> list[dict[str, object]]:
    return [
        event
        for stage in stages
        for event in (
            _expected_stage_start(
                stage,
                delete_orphans_enabled=delete_orphans_enabled,
                dry_run=dry_run,
            ),
            _expected_stage_completion(stage, dry_run=dry_run),
        )
    ]


def test_rest_stage_order_success_mapping_and_operation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_stages(monkeypatch, runner="rest")

    result = _run_rest()

    assert [name for name, _kwargs in calls] == list(REST_ORDER)
    assert result == {
        "status": "completed",
        "devices": [],
        "storage": [{"id": "storage-1"}],
        "virtual_machines": [{"id": "vm-1"}],
        "virtual_disks": {"count": 2, "created": 2, "updated": 0},
        "task_history": {"created": 1, "skipped": 0},
        "backups": [{"id": "backup-1"}],
        "snapshots": {"created": 3, "skipped": 0},
        "replications": {"created": 1, "updated": 2},
        "backup_routines": {"created": 2, "updated": 3},
        "node_interfaces": [{"id": "node-interface-1"}],
        "vm_interfaces": [{"id": "vm-interface-1"}, {"id": "vm-interface-2"}],
        "vm_ip_addresses": [{"id": "ip-1"}],
        "devices_count": 0,
        "storage_count": 1,
        "virtual_machines_count": 1,
        "virtual_disks_count": 2,
        "task_history_count": 1,
        "backups_count": 1,
        "snapshots_count": 3,
        "replications_count": 3,
        "backup_routines_count": 5,
        "node_interfaces_count": 1,
        "vm_interfaces_count": 2,
        "vm_ip_addresses_count": 1,
        "orphan_sweep": {"enabled": True, "deleted": 4},
    }
    call_kwargs = dict(calls)
    assert call_kwargs["virtual-machines"]["run_id"] == _OPERATION_ID
    assert call_kwargs["sweep-orphans"]["run_id"] == _OPERATION_ID
    assert call_kwargs["storage"]["fetch_concurrency"] == 5
    assert call_kwargs["task-history"]["fetch_max_concurrency"] == 5


def test_stream_stage_order_event_shapes_and_operation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_stages(monkeypatch, runner="stream")

    events = _run_stream()

    assert [name for name, _kwargs in calls] == list(STREAM_ORDER)
    assert events[:3] == [
        (
            "bootstrap_done",
            {
                "ok": True,
                "skipped": False,
                "reason": None,
                "warnings": [],
                "created": ["tag:characterisation"],
                "patched": [],
                "unchanged": [],
            },
        ),
        (
            "step",
            {
                "step": "stream",
                "status": "started",
                "message": f"Full update stream connected (operation_id={_OPERATION_ID})",
            },
        ),
        (
            "discovery",
            {
                "event": "discovery",
                "phase": "full-update",
                "status": "discovered",
                "message": "Discovered 12 sync stage(s) for full update",
                "count": 12,
                "items": [
                    {"name": name, "type": "stage"}
                    for name in (
                        "devices",
                        "storage",
                        "virtual-machines",
                        "virtual-disks",
                        "task-history",
                        "backups",
                        "snapshots",
                        "replications",
                        "backup-routines",
                        "node-interfaces",
                        "vm-interfaces",
                        "vm-ip-addresses",
                    )
                ],
                "progress": {"current": 0, "total": 12, "percent": 0},
                "metadata": {"operation_id": _OPERATION_ID},
            },
        ),
    ]
    step_events = [data for event, data in events if event == "step" and data["step"] != "stream"]
    assert step_events == _expected_stage_events(
        STREAM_ORDER,
        delete_orphans_enabled=True,
        dry_run=False,
    )
    assert events[-1] == (
        "complete",
        {
            "ok": True,
            "message": "Full update sync completed.",
            "result": {
                "devices": [],
                "storage": [{"id": "storage-1"}],
                "virtual_machines": [{"id": "vm-1"}],
                "virtual_disks": {"count": 2, "created": 2, "updated": 0},
                "task_history": {"created": 1, "skipped": 0},
                "backups": [{"id": "backup-1"}],
                "snapshots": {"created": 3, "skipped": 0},
                "node_interfaces": [{"id": "node-interface-1"}],
                "vm_interfaces": [{"id": "vm-interface-1"}, {"id": "vm-interface-2"}],
                "vm_ip_addresses": [{"id": "ip-1"}],
                "replications": {"created": 1, "updated": 2},
                "backup_routines": {"created": 2, "updated": 3},
                "devices_count": 0,
                "storage_count": 1,
                "virtual_machines_count": 1,
                "virtual_disks_count": 2,
                "task_history_count": 1,
                "backups_count": 1,
                "snapshots_count": 3,
                "node_interfaces_count": 1,
                "vm_interfaces_count": 2,
                "vm_ip_addresses_count": 1,
                "replications_count": 3,
                "backup_routines_count": 5,
                "orphan_sweep": {"enabled": True, "deleted": 4},
            },
        },
    )
    call_kwargs = dict(calls)
    assert call_kwargs["virtual-machines"]["run_id"] == _OPERATION_ID
    assert call_kwargs["sweep-orphans"]["run_id"] == _OPERATION_ID
    assert call_kwargs["storage"]["fetch_concurrency"] == 5
    assert call_kwargs["task-history"]["fetch_max_concurrency"] == 5


def test_stream_sweep_preview_start_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_stages(
        monkeypatch,
        runner="stream",
        delete_orphans_enabled=False,
    )

    events = _run_stream(dry_run=True)

    step_events = [data for event, data in events if event == "step" and data["step"] != "stream"]
    assert step_events == _expected_stage_events(
        STREAM_ORDER,
        delete_orphans_enabled=False,
        dry_run=True,
    )
    call_kwargs = dict(calls)
    assert call_kwargs["sweep-orphans"]["enabled"] is False
    assert call_kwargs["sweep-orphans"]["dry_run"] is True


def test_stream_skips_disabled_sweep_without_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_stages(
        monkeypatch,
        runner="stream",
        delete_orphans_enabled=False,
    )

    events = _run_stream()

    assert [name for name, _kwargs in calls] == list(STREAM_ORDER[:-1])
    step_events = [data for event, data in events if event == "step" and data["step"] != "stream"]
    assert step_events == _expected_stage_events(
        STREAM_ORDER[:-1],
        delete_orphans_enabled=False,
        dry_run=False,
    )
    assert "orphan_sweep" not in events[-1][1]["result"]


@pytest.mark.parametrize("stage", STAGES, ids=lambda stage: stage.name)
def test_rest_wraps_each_unexpected_stage_failure(
    monkeypatch: pytest.MonkeyPatch,
    proxbox_log_capture: Any,
    stage: StageCase,
) -> None:
    error = RuntimeError(f"boom-{stage.name}")
    calls = _install_stages(
        monkeypatch,
        runner="rest",
        failing_stage=stage.name,
        error=error,
    )

    with pytest.raises(ProxboxException) as raised:
        _run_rest()

    assert raised.value.message == stage.rest_error_message
    assert raised.value.detail is None
    assert raised.value.python_exception == f"boom-{stage.name}"
    assert raised.value.__cause__ is error
    assert [name for name, _kwargs in calls] == list(REST_ORDER[: REST_ORDER.index(stage.name)])
    assert stage.rest_log_message in proxbox_log_capture.messages()


@pytest.mark.parametrize("stage", STAGES, ids=lambda stage: stage.name)
def test_rest_reraises_each_proxbox_stage_failure_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    stage: StageCase,
) -> None:
    error = ProxboxException(
        message=f"proxbox-{stage.name}",
        detail={"stage": stage.name},
        http_status_code=502,
    )
    _install_stages(
        monkeypatch,
        runner="rest",
        failing_stage=stage.name,
        error=error,
    )

    with pytest.raises(ProxboxException) as raised:
        _run_rest()

    assert raised.value is error
    assert raised.value.http_status_code == 502


@pytest.mark.parametrize("stage", STAGES, ids=lambda stage: stage.name)
def test_stream_maps_each_unexpected_stage_failure_and_stops(
    monkeypatch: pytest.MonkeyPatch,
    stage: StageCase,
) -> None:
    calls = _install_stages(
        monkeypatch,
        runner="stream",
        failing_stage=stage.name,
        error=RuntimeError(f"boom-{stage.name}"),
    )

    events = _run_stream()

    assert [name for name, _kwargs in calls] == list(STREAM_ORDER[: STREAM_ORDER.index(stage.name)])
    stage_index = STREAM_ORDER.index(stage.name)
    step_events = [data for event, data in events if event == "step" and data["step"] != "stream"]
    assert step_events == [
        *_expected_stage_events(
            STREAM_ORDER[:stage_index],
            delete_orphans_enabled=True,
            dry_run=False,
        ),
        _expected_stage_start(
            stage.name,
            delete_orphans_enabled=True,
            dry_run=False,
        ),
    ]
    assert events[-3:] == [
        (
            "error_detail",
            {
                "event": "error_detail",
                "phase": "full-update",
                "category": "internal",
                "message": "Unexpected error during full update",
                "detail": f"boom-{stage.name}",
                "suggestion": "Check backend logs for stack trace and retry",
            },
        ),
        (
            "error",
            {
                "step": "full-update",
                "status": "failed",
                "error": f"boom-{stage.name}",
                "detail": f"boom-{stage.name}",
            },
        ),
        (
            "complete",
            {
                "ok": False,
                "message": "Full update sync failed.",
                "errors": [{"detail": f"boom-{stage.name}"}],
            },
        ),
    ]


@pytest.mark.parametrize("stage", STAGES, ids=lambda stage: stage.name)
def test_stream_maps_each_proxbox_stage_failure_and_stops(
    monkeypatch: pytest.MonkeyPatch,
    stage: StageCase,
) -> None:
    error = ProxboxException(
        message=f"proxbox-{stage.name}",
        detail=f"detail-{stage.name}",
        http_status_code=502,
    )
    _install_stages(
        monkeypatch,
        runner="stream",
        failing_stage=stage.name,
        error=error,
    )

    events = _run_stream()

    stage_index = STREAM_ORDER.index(stage.name)
    step_events = [data for event, data in events if event == "step" and data["step"] != "stream"]
    assert step_events == [
        *_expected_stage_events(
            STREAM_ORDER[:stage_index],
            delete_orphans_enabled=True,
            dry_run=False,
        ),
        _expected_stage_start(
            stage.name,
            delete_orphans_enabled=True,
            dry_run=False,
        ),
    ]
    assert events[-3:] == [
        (
            "error_detail",
            {
                "event": "error_detail",
                "phase": "full-update",
                "category": "internal",
                "message": "Full update synchronization failed",
                "detail": f"detail-{stage.name}",
                "suggestion": "Review backend logs and retry the full update",
            },
        ),
        (
            "error",
            {
                "step": "full-update",
                "status": "failed",
                "error": f"proxbox-{stage.name}",
                "detail": f"detail-{stage.name}",
            },
        ),
        (
            "complete",
            {
                "ok": False,
                "message": f"proxbox-{stage.name}",
                "errors": [{"detail": f"detail-{stage.name}"}],
            },
        ),
    ]


def test_rest_propagates_stage_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stages(
        monkeypatch,
        runner="rest",
        failing_stage="devices",
        error=asyncio.CancelledError(),
    )

    with pytest.raises(asyncio.CancelledError):
        _run_rest()


def test_stream_maps_stage_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stages(
        monkeypatch,
        runner="stream",
        failing_stage="devices",
        error=asyncio.CancelledError(),
    )

    events = _run_stream()

    assert events[-2:] == [
        (
            "error",
            {
                "step": "full-update",
                "status": "failed",
                "error": "Server shutdown or request cancelled.",
                "detail": "Server shutdown or request cancelled.",
            },
        ),
        (
            "complete",
            {
                "ok": False,
                "message": "Full update sync cancelled.",
                "errors": [{"detail": "Server shutdown or request cancelled."}],
            },
        ),
    ]
