"""Regression tests: full update must forward ``behavior_flags`` to the node-interface sync.

``create_all_device_interfaces`` only takes the ``sync_node_interfaces``
full-topology path (which sets bridge ``hwaddress`` MACs) when it receives the
resolved ``behavior_flags``. Both full-update entry points used to omit that
argument, so the flag was silently ignored during a full update.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from proxbox_api.schemas.sync import SyncBehaviorFlags
from proxbox_api.services.netbox_bootstrap import BootstrapStatus


def _stub_stages(monkeypatch, node_interface_calls: list[dict]) -> None:
    """Stub every full-update stage; record the node-interface call kwargs."""

    async def _node_interfaces(**kwargs):
        node_interface_calls.append(kwargs)
        return []

    empty_list = {
        name: (lambda **kw: asyncio.sleep(0, result=[]))
        for name in (
            "create_proxmox_devices",
            "create_virtual_machines",
            "create_storages",
            "create_all_virtual_machine_backups",
            "_create_all_virtual_machine_backups",
            "create_only_vm_interfaces",
            "create_only_vm_ip_addresses",
        )
    }
    counts = {
        "create_virtual_disks": {"count": 0, "created": 0, "updated": 0, "skipped": 0},
        "create_all_virtual_machine_snapshots": {"count": 0, "created": 0, "skipped": 0},
        "_create_all_virtual_machine_snapshots": {"count": 0, "created": 0, "skipped": 0},
        "sync_all_virtual_machine_task_histories": {"count": 0, "created": 0, "skipped": 0},
        "sync_all_replications": {"created": 0, "updated": 0},
        "sync_all_backup_routines": {"created": 0, "updated": 0},
    }
    for name, fn in empty_list.items():
        monkeypatch.setattr(f"proxbox_api.app.full_update.{name}", fn)
    for name, result in counts.items():
        monkeypatch.setattr(
            f"proxbox_api.app.full_update.{name}",
            lambda result=result, **kw: asyncio.sleep(0, result=result),
        )
    monkeypatch.setattr(
        "proxbox_api.app.full_update.create_all_device_interfaces", _node_interfaces
    )


def _decode_sse_events(payload: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for frame in payload.split("\n\n"):
        event_name: str | None = None
        data: str | None = None
        for line in frame.splitlines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ")
            if line.startswith("data: "):
                data = line.removeprefix("data: ")
        if event_name and data:
            events.append((event_name, json.loads(data)))
    return events


_TAG = type("Tag", (), {"id": 1, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"})()


@pytest.mark.parametrize("flag_value", [True, False])
def test_full_update_forwards_behavior_flags_to_node_interfaces(monkeypatch, flag_value):
    from proxbox_api.app.full_update import full_update_sync

    calls: list[dict] = []
    _stub_stages(monkeypatch, calls)
    flags = SyncBehaviorFlags(sync_node_interfaces=flag_value)

    asyncio.run(
        full_update_sync(
            netbox_session=object(),
            _sync_deps=BootstrapStatus(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            tag=_TAG,
            behavior_flags=flags,
        )
    )

    assert len(calls) == 1
    assert calls[0]["behavior_flags"] is flags
    assert calls[0]["behavior_flags"].sync_node_interfaces is flag_value


@pytest.mark.parametrize("flag_value", [True, False])
def test_full_update_stream_forwards_behavior_flags_to_node_interfaces(monkeypatch, flag_value):
    from proxbox_api.main import full_update_sync_stream

    calls: list[dict] = []
    _stub_stages(monkeypatch, calls)
    flags = SyncBehaviorFlags(sync_node_interfaces=flag_value)

    async def _run() -> str:
        response = await full_update_sync_stream(
            _sync_deps=BootstrapStatus(),
            netbox_session=object(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[],
            tag=_TAG,
            behavior_flags=flags,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, str) else chunk.decode())
        return "".join(chunks)

    payload = asyncio.run(_run())

    assert len(calls) == 1, payload
    assert calls[0]["behavior_flags"] is flags
    assert calls[0]["behavior_flags"].sync_node_interfaces is flag_value
    # The stream still reports the node-interfaces step as started and completed.
    statuses = [
        data.get("status")
        for event, data in _decode_sse_events(payload)
        if event == "step" and data.get("step") == "node-interfaces"
    ]
    assert statuses == ["started", "completed"], payload


@pytest.mark.parametrize("path", ["/full-update", "/full-update/stream"])
def test_full_update_query_param_reaches_node_interfaces(auth_test_client, monkeypatch, path):
    """End-to-end over HTTP: ``?sync_node_interfaces=true`` is resolved and forwarded."""
    from types import SimpleNamespace

    from proxbox_api.dependencies import ensure_netbox_sync_dependencies, proxbox_tag
    from proxbox_api.main import app
    from proxbox_api.session.proxmox_providers import proxmox_sessions_dep

    async def _fake_bootstrap():
        return BootstrapStatus()

    calls: list[dict] = []
    _stub_stages(monkeypatch, calls)
    app.dependency_overrides[ensure_netbox_sync_dependencies] = _fake_bootstrap
    app.dependency_overrides[proxmox_sessions_dep] = lambda: []
    app.dependency_overrides[proxbox_tag] = lambda: SimpleNamespace(
        id=1, name="Proxbox", slug="proxbox", color="ff5722"
    )

    for query, expected in (("?sync_node_interfaces=true", True), ("", False)):
        calls.clear()
        response = auth_test_client.get(f"{path}{query}")
        assert response.status_code == 200
        assert len(calls) == 1
        assert calls[0]["behavior_flags"].sync_node_interfaces is expected
