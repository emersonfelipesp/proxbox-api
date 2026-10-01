"""HTTP-level reporting of VMs dropped from a staged run.

A staged stage that cannot resolve one VM's Proxmox ownership finishes for the
others and reports the dropped VM, and ``degraded=true``, to the caller. These
tests drive the mounted application (authentication, dependencies, routing and
SSE framing) with ``auth_test_client`` and fake NetBox/Proxmox at the module
seams below the routes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from proxbox_api.dependencies import ensure_netbox_sync_dependencies, proxbox_tag
from proxbox_api.main import app
from proxbox_api.netbox_rest import BulkReconcileResult, RestRecord
from proxbox_api.routes.proxmox.cluster import cluster_resources, cluster_status
from proxbox_api.routes.virtualization.virtual_machines import backups_vm, sync_vm
from proxbox_api.services.netbox_bootstrap import BootstrapStatus
from proxbox_api.services.sync import snapshots as snapshots_module
from proxbox_api.services.sync import vm_filter
from proxbox_api.session.proxmox_providers import proxmox_sessions_dep

PREFIX = "/virtualization/virtual-machines"


def _px(endpoint_id: int, name: str = "cluster-a"):
    return SimpleNamespace(
        db_endpoint_id=endpoint_id,
        name=name,
        session=SimpleNamespace(
            storage=SimpleNamespace(
                get=lambda: [{"storage": "pbs", "nodes": "all", "content": "backup"}]
            )
        ),
    )


def _cluster(name: str, *nodes: str):
    return SimpleNamespace(name=name, node_list=[SimpleNamespace(name=node) for node in nodes])


def _sidecar(netbox_id: int, *, endpoint_id: int | None = 1, vmid: int = 101):
    row: dict[str, object] = {
        "virtual_machine": {"id": netbox_id},
        "proxmox_cluster_name": "cluster-a",
        "proxmox_vm_id": vmid,
        "proxmox_vm_type": "qemu",
    }
    if endpoint_id is not None:
        row["proxmox_endpoint_raw_id"] = endpoint_id
    return row


def _vm(netbox_id: int, vmid: int = 101):
    return {
        "id": netbox_id,
        "name": f"vm-{netbox_id}",
        "cluster": {"name": "cluster-a"},
        "proxmox_vm_id": vmid,
        "proxmox_vm_type": "qemu",
        "proxmox_endpoint_id": 1,
    }


@pytest.fixture
def staged_world(monkeypatch, auth_test_client, client_with_fake_netbox):
    """Mounted app with one Proxmox source; VM 7 is usable and VM 8 has no endpoint id."""

    resources = [
        {
            "cluster-a": [
                {"type": "qemu", "vmid": 101, "name": "vm-7", "node": "pve-a"},
                {"type": "qemu", "vmid": 102, "name": "vm-8", "node": "pve-a"},
            ]
        }
    ]
    app.dependency_overrides[proxmox_sessions_dep] = lambda: [_px(1)]
    app.dependency_overrides[cluster_status] = lambda: [_cluster("cluster-a", "pve-a")]
    app.dependency_overrides[cluster_resources] = lambda: resources
    app.dependency_overrides[proxbox_tag] = lambda: SimpleNamespace(
        id=1, name="Proxbox", slug="proxbox", color="ff5722"
    )
    app.dependency_overrides[ensure_netbox_sync_dependencies] = BootstrapStatus
    sidecars = [_sidecar(7), _sidecar(8, endpoint_id=None, vmid=102)]
    all_vms = [_vm(7), _vm(8, 102)]

    async def _lookup(_nb, path, *, query=None):
        requested = {int(vm_id) for vm_id in (query or {}).get("id", [])}
        return [vm for vm in all_vms if vm["id"] in requested]

    async def _scan(_nb):
        return SimpleNamespace(
            rows=tuple(sidecars), sidecar_unavailable=False, sidecar_read_failed=False
        )

    async def _get(id):
        return next((vm for vm in all_vms if vm["id"] == id), None)

    client_with_fake_netbox.virtualization = SimpleNamespace(
        virtual_machines=SimpleNamespace(get=_get)
    )
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _lookup)
    monkeypatch.setattr(vm_filter, "load_vm_sync_state_identities", _scan)
    yield SimpleNamespace(client=auth_test_client, all_vms=all_vms)
    for dependency in (
        proxmox_sessions_dep,
        cluster_status,
        cluster_resources,
        proxbox_tag,
        ensure_netbox_sync_dependencies,
    ):
        app.dependency_overrides.pop(dependency, None)


def _sse_events(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        name = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if name and data is not None:
            events.append((name, data))
    return events


def _complete_result(body: str) -> dict:
    complete = [data for name, data in _sse_events(body) if name == "complete"]
    assert len(complete) == 1, body
    assert complete[0]["ok"] is True, complete[0]
    return complete[0]["result"]


def _install_snapshot_stage(monkeypatch):
    fetched: list[int] = []

    async def _empty_storage(_nb):
        return {}

    async def _no_disk(*_args, **_kwargs):
        return None

    def _snapshots(*, vmid, **_kwargs):
        fetched.append(vmid)
        return [{"name": "snap", "type": "qemu"}]

    async def _bulk(_nb, _path, *, payloads, **_kwargs):
        return BulkReconcileResult(
            records=[], created=len(payloads), updated=0, unchanged=0, failed=0
        )

    monkeypatch.setattr(snapshots_module, "_load_storage_index", _empty_storage)
    monkeypatch.setattr(snapshots_module, "_resolve_snapshot_storage_record", _no_disk)
    monkeypatch.setattr(snapshots_module, "get_vm_snapshots", _snapshots)
    monkeypatch.setattr(snapshots_module, "rest_bulk_reconcile_async", _bulk)
    return fetched


def test_snapshots_rest_reports_dropped_vm_as_degraded_not_an_error(monkeypatch, staged_world):
    fetched = _install_snapshot_stage(monkeypatch)

    response = staged_world.client.get(f"{PREFIX}/snapshots/all/create?netbox_vm_ids=7,8")

    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is True
    assert body["count"] == 1
    assert [warning["netbox_vm_id"] for warning in body["warnings"]] == [8]
    assert fetched == [101]


def test_snapshots_sse_reports_dropped_vm_as_degraded(monkeypatch, staged_world):
    _install_snapshot_stage(monkeypatch)

    response = staged_world.client.get(f"{PREFIX}/snapshots/all/create/stream?netbox_vm_ids=7,8")

    assert response.status_code == 200
    result = _complete_result(response.text)
    assert result["degraded"] is True
    assert [warning["netbox_vm_id"] for warning in result["warnings"]] == [8]
    # The stage step event carries the same outcome.
    completed_steps = [
        data
        for name, data in _sse_events(response.text)
        if name == "step" and data.get("status") == "completed"
    ]
    assert completed_steps[-1]["result"]["degraded"] is True


@pytest.mark.parametrize(
    ("path", "installer"),
    [
        ("snapshots/create/stream", _install_snapshot_stage),
        ("backups/create/stream", None),
        ("virtual-disks/create/stream", None),
        ("create/stream", None),
    ],
)
def test_single_vm_routes_stay_strict_over_http(monkeypatch, staged_world, path, installer):
    if installer is not None:
        installer(monkeypatch)

    response = staged_world.client.get(f"{PREFIX}/8/{path}")

    assert response.status_code == 200
    events = _sse_events(response.text)
    complete = [data for name, data in events if name == "complete"]
    assert complete[-1]["ok"] is False
    assert "selected VM ownership" in json.dumps(events) or "ownership" in json.dumps(events)
    assert "degraded" not in json.dumps(complete[-1])


def _install_backup_stage(monkeypatch):
    reconciled: list[dict] = []

    async def _empty_storage(_nb):
        return {}

    async def _get_backups(_proxmox, **_kwargs):
        return [
            {
                "content": "backup",
                "vmid": 101,
                "volid": "pbs:backup/vm/101/current",
                "format": "pbs-vm",
                "subtype": "qemu",
            }
        ]

    async def _bulk(_nb, payloads, **_kwargs):
        reconciled.extend(payloads)
        return payloads, len(payloads), 0

    async def _existing(_nb, path, **_kwargs):
        return [RestRecord(SimpleNamespace(), path, {})][:0]

    monkeypatch.setattr(backups_vm, "dump_models", lambda items: items)
    monkeypatch.setattr(backups_vm, "_load_storage_index", _empty_storage)
    monkeypatch.setattr(backups_vm, "get_node_storage_content", _get_backups)
    monkeypatch.setattr(backups_vm, "_bulk_reconcile_backups", _bulk)
    monkeypatch.setattr(backups_vm, "rest_list_paginated_async", _existing)
    return reconciled


def test_backups_rest_wraps_a_degraded_result_and_keeps_a_clean_bare_list(
    monkeypatch, staged_world
):
    reconciled = _install_backup_stage(monkeypatch)

    degraded = staged_world.client.get(f"{PREFIX}/backups/all/create?netbox_vm_ids=7,8")

    assert degraded.status_code == 200
    body = degraded.json()
    assert body["degraded"] is True
    assert body["count"] == len(body["backups"])
    assert [warning["netbox_vm_id"] for warning in body["warnings"]] == [8]
    assert [payload["virtual_machine"] for payload in reconciled] == [7]

    clean = staged_world.client.get(f"{PREFIX}/backups/all/create?netbox_vm_ids=7")

    assert clean.status_code == 200
    assert isinstance(clean.json(), list)


def test_backups_sse_reports_dropped_vm_as_degraded(monkeypatch, staged_world):
    _install_backup_stage(monkeypatch)

    response = staged_world.client.get(f"{PREFIX}/backups/all/create/stream?netbox_vm_ids=7,8")

    assert response.status_code == 200
    result = _complete_result(response.text)
    assert result["degraded"] is True
    assert [warning["netbox_vm_id"] for warning in result["warnings"]] == [8]


@pytest.mark.parametrize(
    ("path", "result_key"),
    [
        ("interfaces/create", "vm_interfaces"),
        ("interfaces/ip-address/create", "vm_ip_addresses"),
    ],
)
def test_vm_interface_and_ip_rest_report_estate_skips(monkeypatch, staged_world, path, result_key):
    async def _snapshot(_nb):
        return [dict(vm) for vm in staged_world.all_vms]

    monkeypatch.setattr(sync_vm, "_load_netbox_virtual_machine_snapshot", _snapshot)

    response = staged_world.client.get(f"{PREFIX}/{path}")

    assert response.status_code == 200
    body = response.json()
    assert body["degraded"] is True
    assert result_key in body
    assert [warning["netbox_vm_id"] for warning in body["warnings"]] == [8]


@pytest.mark.parametrize(
    ("path", "stage"),
    [
        ("interfaces/create/stream", "vm-interfaces"),
        ("interfaces/ip-address/create/stream", "vm-ip-addresses"),
    ],
)
def test_vm_interface_and_ip_sse_report_estate_skips(monkeypatch, staged_world, path, stage):
    async def _snapshot(_nb):
        return [dict(vm) for vm in staged_world.all_vms]

    monkeypatch.setattr(sync_vm, "_load_netbox_virtual_machine_snapshot", _snapshot)

    response = staged_world.client.get(f"{PREFIX}/{path}")

    assert response.status_code == 200
    result = _complete_result(response.text)
    assert result["degraded"] is True
    assert [warning["netbox_vm_id"] for warning in result["warnings"]] == [8]


def test_vm_create_stream_reports_dropped_selected_vm(monkeypatch, staged_world):
    async def _fake_create_virtual_machines(**kwargs):
        # The route already filtered the resources: only VM 7's guest remains.
        assert kwargs["cluster_resources"] == [
            {"cluster-a": [{"type": "qemu", "vmid": 101, "name": "vm-7", "node": "pve-a"}]}
        ]
        return [{"id": 7}]

    monkeypatch.setattr(sync_vm, "create_virtual_machines", _fake_create_virtual_machines)

    response = staged_world.client.get(f"{PREFIX}/create/stream?netbox_vm_ids=7,8")

    assert response.status_code == 200
    result = _complete_result(response.text)
    assert result["count"] == 1
    assert result["degraded"] is True
    assert [warning["netbox_vm_id"] for warning in result["warnings"]] == [8]


def test_single_vm_create_rest_route_stays_strict_over_http(staged_world):
    response = staged_world.client.get(f"{PREFIX}/8/create")

    assert response.status_code == 502
    assert "selected VM ownership" in json.dumps(response.json())


def test_single_vm_create_fails_closed_when_the_lenient_entry_point_still_drops_the_vm(
    monkeypatch,
):
    """The by-id route validates strictly, then calls the lenient list entry point.

    If that entry point drops the VM anyway (ownership changed between the two
    reads), the strict route must fail instead of reporting a degraded success.
    """
    import asyncio

    from proxbox_api.exception import ProxboxException
    from proxbox_api.services.sync.stage_result import attach_skips_to_list

    async def _get(id):
        return {"id": id, "name": "vm-9", "cluster": None}

    async def _strict_filter(*_args, **_kwargs):
        return [{"cluster-a": []}]

    async def _lenient_create(**_kwargs):
        return attach_skips_to_list([], [{"netbox_vm_id": 9, "reason": "ownership changed"}])

    monkeypatch.setattr(sync_vm, "_filter_selected_vm_resource", _strict_filter)
    monkeypatch.setattr(sync_vm, "create_virtual_machines", _lenient_create)

    with pytest.raises(ProxboxException, match="selected VM ownership") as exc_info:
        asyncio.run(
            sync_vm._create_virtual_machine_by_netbox_id(
                netbox_vm_id=9,
                netbox_session=SimpleNamespace(
                    virtualization=SimpleNamespace(virtual_machines=SimpleNamespace(get=_get))
                ),
                pxs=[],
                cluster_status=[],
                cluster_resources=[],
                tag=SimpleNamespace(id=1),
            )
        )

    assert exc_info.value.http_status_code == 502
    assert "ownership changed" in str(exc_info.value.detail)


def test_vm_create_stream_keeps_surviving_vm_on_its_own_source(monkeypatch, staged_world):
    """Dropping the first source's VM must not rebind the second source's VM to it."""

    resources = [
        {"cluster-a": [{"type": "qemu", "vmid": 101, "name": "vm-7", "node": "pve-a"}]},
        {"cluster-b": [{"type": "qemu", "vmid": 201, "name": "vm-9", "node": "pve-b"}]},
    ]
    all_vms = [
        {**_vm(7), "cluster": {"name": "cluster-a"}},
        {**_vm(9, 201), "cluster": {"name": "cluster-b"}},
    ]
    sidecars = [
        _sidecar(7, endpoint_id=None),
        {**_sidecar(9, endpoint_id=2, vmid=201), "proxmox_cluster_name": "cluster-b"},
    ]

    async def _lookup(_nb, path, *, query=None):
        requested = {int(vm_id) for vm_id in (query or {}).get("id", [])}
        return [vm for vm in all_vms if vm["id"] in requested]

    async def _scan(_nb):
        return SimpleNamespace(
            rows=tuple(sidecars), sidecar_unavailable=False, sidecar_read_failed=False
        )

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _lookup)
    monkeypatch.setattr(vm_filter, "load_vm_sync_state_identities", _scan)
    app.dependency_overrides[proxmox_sessions_dep] = lambda: [
        _px(1, "cluster-a"),
        _px(2, "cluster-b"),
    ]
    app.dependency_overrides[cluster_status] = lambda: [
        _cluster("cluster-a", "pve-a"),
        _cluster("cluster-b", "pve-b"),
    ]
    app.dependency_overrides[cluster_resources] = lambda: resources

    async def _fake_create_virtual_machines(**kwargs):
        rows = kwargs["cluster_resources"]
        assert rows[0] == {"cluster-a": []}
        assert rows[1] == {
            "cluster-b": [{"type": "qemu", "vmid": 201, "name": "vm-9", "node": "pve-b"}]
        }
        # Row i must be paired with source i.
        assert [next(iter(row)) for row in rows] == [
            getattr(status, "name") for status in kwargs["cluster_status"]
        ]
        return [{"id": 9}]

    monkeypatch.setattr(sync_vm, "create_virtual_machines", _fake_create_virtual_machines)

    response = staged_world.client.get(f"{PREFIX}/create/stream?netbox_vm_ids=7,9")

    assert response.status_code == 200
    result = _complete_result(response.text)
    assert result["degraded"] is True
    assert [warning["netbox_vm_id"] for warning in result["warnings"]] == [7]
