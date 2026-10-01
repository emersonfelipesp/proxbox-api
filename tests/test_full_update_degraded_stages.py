"""Full update includes VMs a stage dropped, and reports the run as degraded."""

from __future__ import annotations

import asyncio
import json

from proxbox_api.services.netbox_bootstrap import BootstrapStatus
from proxbox_api.services.sync.stage_result import WarningList

_TAG = type("Tag", (), {"id": 1, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"})()

DISK_SKIP = {"netbox_vm_id": 11, "reason": "disk ownership incomplete"}
BACKUP_SKIP = {"netbox_vm_id": 12, "reason": "backup ownership incomplete"}
SNAPSHOT_SKIP = {"netbox_vm_id": 13, "reason": "snapshot ownership incomplete"}
IFACE_SKIP = {"netbox_vm_id": 14, "reason": "interface ownership incomplete"}
IP_SKIP = {"netbox_vm_id": 15, "reason": "ip ownership incomplete"}
PARTIAL_FAILURE = {"phase": "vm-interfaces", "failed": 1, "message": "partial"}


def _stage(result):
    async def _fake(**_kwargs):
        return result

    return _fake


def _stub_stages(monkeypatch, *, degraded: bool, empty_backups: bool = False) -> None:
    disks = {"count": 1, "created": 1, "updated": 0, "skipped": 0}
    snapshots = {"count": 1, "created": 1, "updated": 0, "skipped": 0, "deleted": 0}
    backups = WarningList(
        [] if empty_backups else [{"id": 1}], warnings=[BACKUP_SKIP] if degraded else []
    )
    interfaces = WarningList(
        [{"id": 2}], warnings=[IFACE_SKIP, PARTIAL_FAILURE] if degraded else []
    )
    ips = WarningList([{"id": 3}], warnings=[IP_SKIP] if degraded else [])
    if degraded:
        disks = {**disks, "degraded": True, "warnings": [DISK_SKIP]}
        snapshots = {**snapshots, "degraded": True, "warnings": [SNAPSHOT_SKIP]}

    stages = {
        "create_proxmox_devices": [],
        "create_virtual_machines": [],
        "create_storages": [],
        "create_virtual_disks": disks,
        "create_all_virtual_machine_backups": backups,
        "_create_all_virtual_machine_backups": backups,
        "create_all_virtual_machine_snapshots": snapshots,
        "_create_all_virtual_machine_snapshots": snapshots,
        "sync_all_virtual_machine_task_histories": {"count": 0, "created": 0, "skipped": 0},
        "create_all_device_interfaces": [],
        "create_only_vm_interfaces": interfaces,
        "create_only_vm_ip_addresses": ips,
        "sync_all_replications": {"created": 0, "updated": 0},
        "sync_all_backup_routines": {"created": 0, "updated": 0},
    }
    for name, result in stages.items():
        monkeypatch.setattr(f"proxbox_api.app.full_update.{name}", _stage(result))


def _sse_events(payload: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in payload.split("\n\n"):
        name = None
        data = None
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            if line.startswith("data: "):
                data = line.removeprefix("data: ")
        if name and data:
            events.append((name, json.loads(data)))
    return events


def _by_phase(warnings: list[dict]) -> dict[str, dict]:
    return {warning["phase"]: warning for warning in warnings if "netbox_vm_id" in warning}


def test_full_update_rest_includes_every_stage_skip_and_is_degraded(monkeypatch):
    from proxbox_api.app.full_update import full_update_sync

    _stub_stages(monkeypatch, degraded=True)

    result = asyncio.run(
        full_update_sync(
            netbox_session=object(),
            _sync_deps=BootstrapStatus(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            tag=_TAG,
        )
    )

    assert result["status"] == "completed"
    assert result["degraded"] is True
    phases = _by_phase(result["warnings"])
    assert {phase: warning["netbox_vm_id"] for phase, warning in phases.items()} == {
        "virtual-disks": 11,
        "backups": 12,
        "snapshots": 13,
        "vm-interfaces": 14,
        "vm-ip-addresses": 15,
    }
    # A pre-existing partial-failure warning keeps its own phase and payload.
    assert PARTIAL_FAILURE in result["warnings"]
    assert result["backups_count"] == 1


def test_full_update_rest_clean_run_is_not_degraded(monkeypatch):
    from proxbox_api.app.full_update import full_update_sync

    _stub_stages(monkeypatch, degraded=False)

    result = asyncio.run(
        full_update_sync(
            netbox_session=object(),
            _sync_deps=BootstrapStatus(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            tag=_TAG,
        )
    )

    assert "degraded" not in result
    assert "warnings" not in result


def test_full_update_stream_reports_skips_per_step_and_in_the_final_result(monkeypatch):
    from proxbox_api.main import full_update_sync_stream

    _stub_stages(monkeypatch, degraded=True)

    async def _run() -> str:
        response = await full_update_sync_stream(
            _sync_deps=BootstrapStatus(),
            netbox_session=object(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            tag=_TAG,
            dry_run=False,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
        return "".join(chunks)

    events = _sse_events(asyncio.run(_run()))

    step_results = {
        data["step"]: data["result"]
        for name, data in events
        if name == "step" and data.get("status") == "completed"
    }
    for step, netbox_vm_id in (
        ("virtual-disks", 11),
        ("backups", 12),
        ("snapshots", 13),
        ("vm-interfaces", 14),
        ("vm-ip-addresses", 15),
    ):
        assert step_results[step]["degraded"] is True, step
        assert netbox_vm_id in [w.get("netbox_vm_id") for w in step_results[step]["warnings"]], step
    assert "degraded" not in step_results["virtual-machines"]

    complete = [data for name, data in events if name == "complete"][-1]
    assert complete["ok"] is True, events[-3:]
    assert complete["result"]["degraded"] is True
    assert sorted(_by_phase(complete["result"]["warnings"])) == [
        "backups",
        "snapshots",
        "virtual-disks",
        "vm-interfaces",
        "vm-ip-addresses",
    ]


def test_full_update_keeps_warnings_of_an_empty_backup_result(monkeypatch):
    """Estate backups often find nothing in Proxmox; the drop must still be reported.

    An empty result that carries warnings is falsy, so a defensive ``or []`` would
    silently discard exactly the VM the run could not resolve.
    """
    from proxbox_api.app.full_update import full_update_sync
    from proxbox_api.main import full_update_sync_stream

    _stub_stages(monkeypatch, degraded=True, empty_backups=True)

    rest = asyncio.run(
        full_update_sync(
            netbox_session=object(),
            _sync_deps=BootstrapStatus(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            tag=_TAG,
        )
    )

    assert rest["backups_count"] == 0
    assert _by_phase(rest["warnings"])["backups"]["netbox_vm_id"] == 12

    async def _run() -> str:
        response = await full_update_sync_stream(
            _sync_deps=BootstrapStatus(),
            netbox_session=object(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            tag=_TAG,
            dry_run=False,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
        return "".join(chunks)

    events = _sse_events(asyncio.run(_run()))
    backups_step = next(
        data["result"]
        for name, data in events
        if name == "step" and data.get("step") == "backups" and data.get("status") == "completed"
    )
    assert backups_step["count"] == 0
    assert backups_step["degraded"] is True
    complete = [data for name, data in events if name == "complete"][-1]
    assert _by_phase(complete["result"]["warnings"])["backups"]["netbox_vm_id"] == 12
