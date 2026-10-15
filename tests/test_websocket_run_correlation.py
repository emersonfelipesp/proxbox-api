"""Run-correlation contracts for the legacy synchronization WebSocket."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from proxbox_api.app import websockets
from proxbox_api.dependencies import proxbox_tag
from proxbox_api.routes.proxmox.cluster import cluster_resources, cluster_status
from proxbox_api.routes.virtualization.virtual_machines import sync_vm
from proxbox_api.routes.virtualization.virtual_machines.sync_vm import SyncResultList
from proxbox_api.services.sync import devices as device_sync
from proxbox_api.services.sync import sync_state_reader
from proxbox_api.session.proxmox_providers import proxmox_sessions_dep
from tests.websocket_test_support import websocket_session

_INVALID_COMMAND_TEXT = [
    "Invalid sync command",
    "Valid commands: 'Sync Nodes', 'Sync Virtual Machines', 'Full Update Sync'",
]


@pytest.fixture
def sync_websocket_client(legacy_auth_test_client, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(websockets, "check_auth_header", lambda *_args: (True, None))

    async def sessions():
        yield []

    app = legacy_auth_test_client.app
    app.dependency_overrides.update(
        {
            proxmox_sessions_dep: sessions,
            cluster_status: lambda: [],
            cluster_resources: lambda: [],
            proxbox_tag: object,
        }
    )
    return legacy_auth_test_client


@contextmanager
def _connected(client: Any):
    with websocket_session(client, "/ws") as websocket:
        websocket.send_json({"api_key": client.headers["X-Proxbox-API-Key"]})
        assert websocket.receive_text() == "Connected!"
        assert websocket.receive_text() == "Connected 2!"
        yield websocket


def _install_completed_stages(
    monkeypatch: pytest.MonkeyPatch, expected_run_id: str | None = None
) -> None:
    async def devices(**kwargs):
        await kwargs["websocket"].send_text("device-progress")
        await kwargs["websocket"].send_json({"object": "device", "end": True})
        return [{"id": 1}]

    async def virtual_machines(**kwargs):
        assert kwargs["run_id"] == expected_run_id
        await kwargs["websocket"].send_text("vm-progress")
        await kwargs["websocket"].send_json({"object": "virtual_machine", "end": True})
        return [{"id": 2}]

    monkeypatch.setattr(websockets, "create_proxmox_devices", devices)
    monkeypatch.setattr(websockets, "create_virtual_machines", virtual_machines)


def _install_real_device_stage(
    monkeypatch: pytest.MonkeyPatch,
    *,
    successful_records: int,
) -> None:
    clusters = [
        SimpleNamespace(
            name="cluster-a",
            mode="cluster",
            db_endpoint_id=None,
            endpoint_name="endpoint-a",
            node_device_name_template="{node}",
            node_list=[SimpleNamespace(name="pve01"), SimpleNamespace(name="pve02")],
        )
    ]

    class Record:
        def __init__(self, record_id: int) -> None:
            self.record_id = record_id

        def serialize(self) -> dict[str, object]:
            return {
                "id": self.record_id,
                "name": f"pve0{self.record_id}",
                "display_url": f"/devices/{self.record_id}/",
            }

    records = {
        (None, "cluster-a", f"pve0{index}"): Record(index)
        for index in range(1, successful_records + 1)
    }

    async def reconcile(*_args, **_kwargs):
        return records

    async def hardware(*_args, **_kwargs):
        return None

    async def devices(**kwargs):
        result = await device_sync.create_proxmox_devices(
            netbox_session=kwargs["netbox_session"],
            clusters_status=clusters,
            tag=SimpleNamespace(name="Proxbox", slug="proxbox", color="ff5722"),
            websocket=kwargs["websocket"],
            use_websocket=True,
        )
        assert isinstance(result, device_sync.DeviceSyncResultList)
        assert result.failed_count == 2 - successful_records
        assert json.loads(json.dumps(result)) == list(result)
        return result

    monkeypatch.setattr(device_sync, "_reconcile_devices_or_raise", reconcile)
    monkeypatch.setattr(device_sync, "_run_hardware_discovery", hardware)
    monkeypatch.setattr(websockets, "create_proxmox_devices", devices)


def _install_real_failing_vm_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reconcile(*args, **kwargs):
        if len(args) > 1 and args[1] == "/api/virtualization/virtual-machines/":
            return {"id": 101, "name": "vm-101", "primary_ip4": None}
        return {"id": 1, "name": kwargs.get("payload", {}).get("name")}

    async def create(_nb, _path, payload, **_kwargs):
        return {"id": 101, "name": payload["name"], "primary_ip4": None}

    async def ensure(*_args, **_kwargs):
        return SimpleNamespace(id=1)

    async def list_records(*_args, **_kwargs):
        return []

    async def list_paginated(*_args, **_kwargs):
        return []

    async def fail_interface(**_kwargs):
        raise RuntimeError("permanent NetBox interface failure")

    async def vm_stage(**kwargs):
        result = await sync_vm.create_virtual_machines(
            netbox_session=kwargs["netbox_session"],
            pxs=[],
            cluster_status=[SimpleNamespace(name="cluster-a", mode="cluster")],
            cluster_resources=[
                {
                    "cluster-a": [
                        {
                            "type": "qemu",
                            "name": "vm-101",
                            "vmid": 101,
                            "node": "pve01",
                        }
                    ]
                }
            ],
            tag=SimpleNamespace(id=1, name="Proxbox", slug="proxbox", color="ff5722"),
            websocket=kwargs["websocket"],
            use_websocket=True,
            run_id=kwargs["run_id"],
            sync_task_history=False,
            sync_mode_vm="always",
            sync_mode_vm_template="always",
            vm_interface_sync_strategy="guest_os_model",
        )
        assert isinstance(result, SyncResultList)
        assert json.loads(json.dumps(result)) == list(result)
        return result

    monkeypatch.setattr(sync_vm, "rest_reconcile_async", reconcile)
    monkeypatch.setattr(sync_vm, "rest_create_async", create)
    monkeypatch.setattr(sync_vm, "rest_list_async", list_records)
    monkeypatch.setattr(sync_vm, "rest_list_paginated_async", list_paginated)
    monkeypatch.setattr(sync_state_reader, "rest_list_async", list_records)
    sync_state_reader.reset_sidecar_reader_availability_cache()
    monkeypatch.setattr(
        sync_vm,
        "get_vm_config",
        lambda **_kwargs: {
            "onboot": 1,
            "agent": 1,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,ip=1.1.1.3/24",
        },
    )
    for name in (
        "_ensure_cluster_type",
        "_ensure_cluster",
        "_ensure_manufacturer",
        "_ensure_device_type",
        "_ensure_site",
        "_ensure_device",
        "_ensure_proxmox_node_role",
        "ensure_vm_type",
    ):
        monkeypatch.setattr(sync_vm, name, ensure)
    monkeypatch.setattr(
        sync_vm,
        "build_netbox_virtual_machine_payload",
        lambda **_kwargs: {"name": "vm-101", "status": "active", "cluster": 1},
    )
    monkeypatch.setattr(sync_vm, "_create_vm_interface_parallel", fail_interface)
    monkeypatch.setattr(websockets, "create_virtual_machines", vm_stage)


def test_legacy_full_update_frames_are_byte_identical(
    sync_websocket_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_completed_stages(monkeypatch)

    with _connected(sync_websocket_client) as websocket:
        websocket.send_text("Full Update")
        websocket.send_text("sentinel")
        frames = [websocket.receive_text() for _ in range(6)]

    assert frames[:4] == [
        "device-progress",
        '{"object":"device","end":true}',
        "vm-progress",
        '{"object":"virtual_machine","end":true}',
    ]
    assert frames[4:] == _INVALID_COMMAND_TEXT
    assert all('"object":"full-update"' not in frame for frame in frames)


def test_correlated_full_update_adds_run_id_and_completed_aggregate(
    sync_websocket_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "run-completed-01"
    _install_completed_stages(monkeypatch, run_id)

    with _connected(sync_websocket_client) as websocket:
        websocket.send_text(json.dumps({"command": "Full Update", "run_id": run_id}))
        websocket.send_json({"command": "sentinel", "run_id": run_id})
        frames = [websocket.receive_json() for _ in range(7)]

    assert all(frame["run_id"] == run_id for frame in frames)
    assert frames[4] == {
        "object": "full-update",
        "end": True,
        "status": "completed",
        "stages": {"devices": "completed", "virtual-machines": "completed"},
        "run_id": run_id,
    }
    assert sum(frame.get("object") == "full-update" for frame in frames) == 1
    assert [frame["message"] for frame in frames[-2:]] == _INVALID_COMMAND_TEXT


@pytest.mark.parametrize(
    ("command", "expected_frames"),
    [
        (
            "Sync Nodes",
            [
                "Sync Nodes",
                "device-progress",
                '{"object":"device","end":true}',
            ],
        ),
        (
            "Sync Virtual Machines",
            ["vm-progress", '{"object":"virtual_machine","end":true}'],
        ),
    ],
)
def test_legacy_individual_command_frames_are_byte_identical(
    sync_websocket_client,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    expected_frames: list[str],
) -> None:
    _install_completed_stages(monkeypatch)

    with _connected(sync_websocket_client) as websocket:
        websocket.send_text(command)
        websocket.send_text("sentinel")
        frames = [websocket.receive_text() for _ in range(len(expected_frames) + 2)]

    assert frames[: len(expected_frames)] == expected_frames
    assert frames[-2:] == _INVALID_COMMAND_TEXT
    assert all('"object":"full-update"' not in frame for frame in frames)


@pytest.mark.parametrize("run_id", ["abcdefgh", "x" * 64, "valid_RUN-123"])
def test_command_envelope_accepts_run_id_boundaries(run_id: str) -> None:
    parsed = websockets._parse_sync_command_frame(
        json.dumps({"command": "Sync Nodes", "run_id": run_id})
    )

    assert parsed.command == "Sync Nodes"
    assert parsed.run_id == run_id


def test_correlated_full_update_marks_vm_stage_skipped(
    sync_websocket_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "run-skipped-001"

    async def devices(**kwargs):
        await kwargs["websocket"].send_json({"object": "device", "end": True})
        return []

    async def virtual_machines(**kwargs):
        pytest.fail("A full update without synchronized nodes ran the VM stage")

    monkeypatch.setattr(websockets, "create_proxmox_devices", devices)
    monkeypatch.setattr(websockets, "create_virtual_machines", virtual_machines)

    with _connected(sync_websocket_client) as websocket:
        websocket.send_json({"command": "Full Update Sync", "run_id": run_id})
        terminal = websocket.receive_json()
        aggregate = websocket.receive_json()

    assert terminal == {"object": "device", "end": True, "run_id": run_id}
    assert aggregate == {
        "object": "full-update",
        "end": True,
        "status": "completed",
        "stages": {"devices": "completed", "virtual-machines": "skipped"},
        "run_id": run_id,
    }


@pytest.mark.parametrize(
    ("failed_stage", "expected_stages"),
    [
        ("devices", {"devices": "failed", "virtual-machines": "skipped"}),
        (
            "virtual-machines",
            {"devices": "completed", "virtual-machines": "failed"},
        ),
    ],
)
def test_correlated_full_update_reports_failed_stage_and_aggregate(
    sync_websocket_client,
    monkeypatch: pytest.MonkeyPatch,
    failed_stage: str,
    expected_stages: dict[str, str],
) -> None:
    run_id = "run-failed-0001"

    async def devices(**kwargs):
        if failed_stage == "devices":
            raise RuntimeError("synthetic device failure")
        return [{"id": 1}]

    async def virtual_machines(**kwargs):
        raise RuntimeError("synthetic VM failure")

    monkeypatch.setattr(websockets, "create_proxmox_devices", devices)
    monkeypatch.setattr(websockets, "create_virtual_machines", virtual_machines)

    with _connected(sync_websocket_client) as websocket:
        websocket.send_json({"command": "Full Update", "run_id": run_id})
        error = websocket.receive_json()
        aggregate = websocket.receive_json()

    assert error == {
        "event": "error_detail",
        "phase": failed_stage,
        "category": "internal",
        "message": f"{failed_stage} synchronization failed.",
        "run_id": run_id,
    }
    assert aggregate == {
        "object": "full-update",
        "end": True,
        "status": "failed",
        "stages": expected_stages,
        "run_id": run_id,
    }


@pytest.mark.parametrize(
    ("successful_records", "expected_stages", "vm_runs"),
    [
        (
            1,
            {"devices": "failed", "virtual-machines": "completed"},
            True,
        ),
        (
            0,
            {"devices": "failed", "virtual-machines": "skipped"},
            False,
        ),
    ],
)
def test_real_device_finalizer_failure_count_reaches_failed_aggregate(
    sync_websocket_client,
    monkeypatch: pytest.MonkeyPatch,
    successful_records: int,
    expected_stages: dict[str, str],
    vm_runs: bool,
) -> None:
    run_id = f"device-failed-{successful_records}"
    _install_real_device_stage(monkeypatch, successful_records=successful_records)
    vm_calls = 0

    async def virtual_machines(**kwargs):
        nonlocal vm_calls
        vm_calls += 1
        await kwargs["websocket"].send_json({"object": "virtual_machine", "end": True})
        return []

    monkeypatch.setattr(websockets, "create_virtual_machines", virtual_machines)
    frame_count = 9 if successful_records else 7

    with _connected(sync_websocket_client) as websocket:
        websocket.send_json({"command": "Full Update", "run_id": run_id})
        websocket.send_json({"command": "sentinel", "run_id": run_id})
        frames = [websocket.receive_json() for _ in range(frame_count)]

    assert vm_calls == int(vm_runs)
    assert all(frame["run_id"] == run_id for frame in frames)
    assert sum(frame.get("object") == "full-update" for frame in frames) == 1
    assert [frame["message"] for frame in frames[-2:]] == _INVALID_COMMAND_TEXT
    assert {key: value for key, value in frames[-3].items() if key != "run_id"} == {
        "object": "full-update",
        "end": True,
        "status": "failed",
        "stages": expected_stages,
    }
    assert any(
        frame.get("event") == "error_detail" and frame.get("phase") == "devices" for frame in frames
    )


def test_failed_vm_result_emits_error_before_failed_aggregate(
    sync_websocket_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "vm-result-failed"

    async def devices(**_kwargs):
        return [{"id": 1}]

    async def virtual_machines(**kwargs):
        await kwargs["websocket"].send_json({"object": "virtual_machine", "end": True})
        return SyncResultList([], failed_count=1)

    monkeypatch.setattr(websockets, "create_proxmox_devices", devices)
    monkeypatch.setattr(websockets, "create_virtual_machines", virtual_machines)

    with _connected(sync_websocket_client) as websocket:
        websocket.send_json({"command": "Full Update", "run_id": run_id})
        frames = [websocket.receive_json() for _ in range(3)]

    assert frames == [
        {"object": "virtual_machine", "end": True, "run_id": run_id},
        {
            "event": "error_detail",
            "phase": "virtual-machines",
            "category": "internal",
            "message": "virtual-machines synchronization failed.",
            "run_id": run_id,
        },
        {
            "object": "full-update",
            "end": True,
            "status": "failed",
            "stages": {"devices": "completed", "virtual-machines": "failed"},
            "run_id": run_id,
        },
    ]


@pytest.mark.parametrize(
    ("command", "result_factory", "terminal_object", "command_frame_count"),
    [
        (
            "Sync Nodes",
            lambda: device_sync.DeviceSyncResultList([], failed_count=1),
            "device",
            2,
        ),
        (
            "Sync Virtual Machines",
            lambda: SyncResultList([], failed_count=1),
            "virtual_machine",
            1,
        ),
    ],
)
def test_correlated_individual_failed_result_emits_error_and_connection_stays_usable(
    sync_websocket_client,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    result_factory,
    terminal_object: str,
    command_frame_count: int,
) -> None:
    run_id = "individual-failed-01"

    async def devices(**kwargs):
        await kwargs["websocket"].send_json({"object": "device", "end": True})
        return result_factory()

    async def virtual_machines(**kwargs):
        await kwargs["websocket"].send_json({"object": "virtual_machine", "end": True})
        return result_factory()

    monkeypatch.setattr(websockets, "create_proxmox_devices", devices)
    monkeypatch.setattr(websockets, "create_virtual_machines", virtual_machines)

    with _connected(sync_websocket_client) as websocket:
        websocket.send_json({"command": command, "run_id": run_id})
        command_frames = [websocket.receive_json() for _ in range(command_frame_count)]
        websocket.send_json({"command": "sentinel", "run_id": run_id})
        outcome = websocket.receive_json()
        sentinel_frames = [websocket.receive_json() for _ in range(2)]

    frames = [*command_frames, outcome, *sentinel_frames]
    stage = "devices" if command == "Sync Nodes" else "virtual-machines"

    assert all(frame["run_id"] == run_id for frame in frames)
    assert any(frame.get("object") == terminal_object and frame.get("end") for frame in frames)
    assert outcome == {
        "event": "error_detail",
        "phase": stage,
        "category": "internal",
        "message": f"{stage} synchronization failed.",
        "run_id": run_id,
    }
    assert [frame["message"] for frame in frames[-2:]] == _INVALID_COMMAND_TEXT
    assert all(frame.get("object") != "full-update" for frame in frames)


def test_real_vm_interface_failure_reaches_failed_aggregate_and_connection_stays_usable(
    sync_websocket_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "vm-interface-failed"
    _install_real_failing_vm_stage(monkeypatch)

    async def devices(**_kwargs):
        return [{"id": 1}]

    monkeypatch.setattr(websockets, "create_proxmox_devices", devices)

    with _connected(sync_websocket_client) as websocket:
        websocket.send_json({"command": "Full Update", "run_id": run_id})
        command_frames = []
        for _ in range(6):
            frame = websocket.receive_json()
            command_frames.append(frame)
            if frame.get("object") == "full-update":
                break
        else:
            pytest.fail("Correlated full update did not emit an aggregate terminal")
        websocket.send_json({"command": "sentinel", "run_id": run_id})
        sentinel_frames = [websocket.receive_json() for _ in range(2)]

    frames = [*command_frames, *sentinel_frames]

    assert all(frame["run_id"] == run_id for frame in frames)
    assert sum(frame.get("event") == "error_detail" for frame in frames) == 1
    aggregates = [frame for frame in frames if frame.get("object") == "full-update"]
    assert aggregates == [
        {
            "object": "full-update",
            "end": True,
            "status": "failed",
            "stages": {"devices": "completed", "virtual-machines": "failed"},
            "run_id": run_id,
        }
    ]
    assert [frame["message"] for frame in frames[-2:]] == _INVALID_COMMAND_TEXT


@pytest.mark.parametrize(
    "payload",
    [
        {"command": "Sync Nodes", "run_id": "short"},
        {"command": "Sync Nodes", "run_id": "x" * 65},
        {"command": "Sync Nodes", "run_id": "invalid.run.id"},
        {"command": "Sync Nodes", "run_id": 12345678},
        {"command": "Sync Nodes"},
    ],
)
def test_invalid_run_id_rejected_without_execution(
    sync_websocket_client,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
) -> None:
    calls = 0

    async def devices(**kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(websockets, "create_proxmox_devices", devices)

    with _connected(sync_websocket_client) as websocket:
        websocket.send_json(payload)
        assert websocket.receive_text() == "Invalid sync command"
        assert websocket.receive_text() == (
            "Valid commands: 'Sync Nodes', 'Sync Virtual Machines', 'Full Update Sync'"
        )

    assert calls == 0


def test_command_envelope_rejects_extra_keys_without_execution(
    sync_websocket_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    run_id = "run-extra-0001"

    async def devices(**kwargs):
        nonlocal calls
        calls += 1

    monkeypatch.setattr(websockets, "create_proxmox_devices", devices)

    with _connected(sync_websocket_client) as websocket:
        websocket.send_json({"command": "Sync Nodes", "run_id": run_id, "unexpected": True})
        first = websocket.receive_json()
        second = websocket.receive_json()

    assert first == {"message": "Invalid sync command", "run_id": run_id}
    assert second == {
        "message": "Valid commands: 'Sync Nodes', 'Sync Virtual Machines', 'Full Update Sync'",
        "run_id": run_id,
    }
    assert calls == 0


@pytest.mark.parametrize(
    "frame",
    [
        json.dumps({"command": "Sync Nodes", "run_id": "oversized-run"})
        + (" " * websockets._MAX_SYNC_COMMAND_ENVELOPE_BYTES),
        '{"command":' + ("[" * 1500) + "0" + ("]" * 1500) + "}",
        "9" * 5000,
    ],
    ids=["oversized-envelope", "deeply-nested", "large-integer"],
)
def test_invalid_large_frames_keep_connection_usable(
    sync_websocket_client,
    monkeypatch: pytest.MonkeyPatch,
    frame: str,
) -> None:
    _install_completed_stages(monkeypatch)

    with _connected(sync_websocket_client) as websocket:
        websocket.send_text(frame)
        invalid_frames = [websocket.receive_text() for _ in range(2)]
        websocket.send_text("Sync Nodes")
        valid_frames = [websocket.receive_text() for _ in range(3)]

    assert invalid_frames == _INVALID_COMMAND_TEXT
    assert valid_frames == [
        "Sync Nodes",
        "device-progress",
        '{"object":"device","end":true}',
    ]


def test_correlated_legacy_correlated_sequence_does_not_leak_run_id(
    sync_websocket_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_completed_stages(monkeypatch)
    run_a = "sequence-run-a"
    run_b = "sequence-run-b"

    with _connected(sync_websocket_client) as websocket:
        websocket.send_json({"command": "Sync Nodes", "run_id": run_a})
        correlated_a = [websocket.receive_json() for _ in range(3)]
        websocket.send_text("Sync Nodes")
        legacy = [websocket.receive_text() for _ in range(3)]
        websocket.send_json({"command": "Sync Nodes", "run_id": run_b})
        correlated_b = [websocket.receive_json() for _ in range(3)]

    assert {frame["run_id"] for frame in correlated_a} == {run_a}
    assert legacy == [
        "Sync Nodes",
        "device-progress",
        '{"object":"device","end":true}',
    ]
    assert {frame["run_id"] for frame in correlated_b} == {run_b}


def test_disconnect_after_correlated_command_does_not_affect_next_connection(
    sync_websocket_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_completed_stages(monkeypatch)
    run_id = "disconnect-run-01"

    with _connected(sync_websocket_client) as websocket:
        websocket.send_json({"command": "Sync Nodes", "run_id": run_id})
        correlated = [websocket.receive_json() for _ in range(3)]

    with _connected(sync_websocket_client) as websocket:
        websocket.send_text("Sync Nodes")
        legacy = [websocket.receive_text() for _ in range(3)]

    assert {frame["run_id"] for frame in correlated} == {run_id}
    assert legacy == [
        "Sync Nodes",
        "device-progress",
        '{"object":"device","end":true}',
    ]


def test_concurrent_connections_keep_run_ids_isolated(
    sync_websocket_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    state: dict[str, object] = {"arrived": 0, "gate": None}

    async def devices(**kwargs):
        gate = state["gate"]
        if gate is None:
            gate = asyncio.Event()
            state["gate"] = gate
        state["arrived"] = int(state["arrived"]) + 1
        if state["arrived"] == 2:
            gate.set()
        await asyncio.wait_for(gate.wait(), timeout=2)
        await kwargs["websocket"].send_json({"object": "device", "end": True})
        return []

    monkeypatch.setattr(websockets, "create_proxmox_devices", devices)
    run_a = "run-connect-a01"
    run_b = "run-connect-b02"

    with _connected(sync_websocket_client) as websocket_a:
        with _connected(sync_websocket_client) as websocket_b:
            websocket_a.send_json({"command": "Sync Nodes", "run_id": run_a})
            websocket_b.send_json({"command": "Sync Nodes", "run_id": run_b})
            frames_a = [websocket_a.receive_json() for _ in range(2)]
            frames_b = [websocket_b.receive_json() for _ in range(2)]

    assert {frame["run_id"] for frame in frames_a} == {run_a}
    assert {frame["run_id"] for frame in frames_b} == {run_b}
