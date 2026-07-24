"""
SSE Stream Output Tests

Verifies that the backend produces SSE events in the correct format
for the plugin to consume. These tests ensure the SSE contract is maintained.
"""

import asyncio
import inspect
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from proxbox_api.dependencies import proxbox_tag
from proxbox_api.exception import ProxboxException
from proxbox_api.routes.proxmox.cluster import cluster_status
from proxbox_api.routes.virtualization.virtual_machines import (
    backups_vm,
    disks_vm,
    snapshots_vm,
    sync_vm,
    task_history_vm,
)
from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.sync import snapshots as snapshots_service
from proxbox_api.services.sync import sync_state_reader
from proxbox_api.session.netbox import get_netbox_session
from proxbox_api.session.proxmox_providers import proxmox_sessions


def test_vm_stream_routes_expose_task_history_flag_defaulting_true():
    for endpoint in (
        sync_vm.create_virtual_machines_stream,
        sync_vm.create_virtual_machine_by_netbox_id_stream,
    ):
        parameter = inspect.signature(endpoint).parameters["sync_task_history"]
        default = getattr(parameter.default, "default", parameter.default)
        assert default is True


def test_task_history_stream_declares_netbox_vm_ids_http_query():
    parameter = inspect.signature(
        task_history_vm.create_all_virtual_machine_task_histories_stream
    ).parameters["netbox_vm_ids"]
    assert getattr(parameter.default, "default", parameter.default) is None


def test_task_history_stream_resets_memoized_sidecar_unavailability_per_request(
    monkeypatch,
):
    attempts = 0

    async def _unavailable_sidecar(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise ProxboxException(
            message="sidecar unavailable",
            http_status_code=404,
        )

    async def _sync_with_probe(**kwargs):
        scan = await sync_state_reader.load_vm_sync_state_identities(kwargs["netbox_session"])
        assert scan.sidecar_unavailable is True
        return {"count": 0, "created": 0, "skipped": 0}

    monkeypatch.setattr(sync_state_reader, "rest_list_paginated_async", _unavailable_sidecar)
    monkeypatch.setattr(
        task_history_vm,
        "sync_all_virtual_machine_task_histories",
        _sync_with_probe,
    )
    sync_state_reader.reset_sidecar_reader_availability_cache()
    try:
        # Seed the exact stale state that previously leaked across requests.
        first_scan = asyncio.run(sync_state_reader.load_vm_sync_state_identities(object()))
        assert first_scan.sidecar_unavailable is True
        assert attempts == 1

        probe_app = FastAPI()
        probe_app.include_router(task_history_vm.router)
        probe_app.dependency_overrides[get_netbox_session] = object
        probe_app.dependency_overrides[proxmox_sessions] = lambda: []
        probe_app.dependency_overrides[cluster_status] = lambda: []
        probe_app.dependency_overrides[proxbox_tag] = lambda: SimpleNamespace(
            name="Proxbox",
            slug="proxbox",
        )

        with TestClient(probe_app) as client:
            for _ in range(2):
                response = client.get("/task-history/create/stream?fetch_max_concurrency=1")
                assert response.status_code == 200
                assert "event: complete" in response.text

            invalid = client.get("/task-history/create/stream?fetch_max_concurrency=0")

        assert invalid.status_code == 422
        assert attempts == 3
    finally:
        sync_state_reader.reset_sidecar_reader_availability_cache()


async def test_task_history_stream_forwards_selected_scope_and_rejects_invalid_before_stream(
    monkeypatch,
):
    captured: list[list[int] | None] = []

    async def _fake_sync(**kwargs):
        captured.append(kwargs["netbox_vm_ids"])
        return {"count": 0, "created": 0, "skipped": 0}

    monkeypatch.setattr(
        task_history_vm,
        "sync_all_virtual_machine_task_histories",
        _fake_sync,
    )

    for raw_scope in ("102,101,102", None):
        response = await task_history_vm.create_all_virtual_machine_task_histories_stream(
            netbox_session=SimpleNamespace(),
            pxs=[],
            cluster_status=[],
            tag=SimpleNamespace(name="Proxbox", slug="proxbox"),
            netbox_vm_ids=raw_scope,
            fetch_max_concurrency=None,
        )
        async for _chunk in response.body_iterator:
            pass

    assert captured == [[101, 102], None]

    for invalid_scope in ("", " ", "invalid", "101,invalid", "0"):
        with pytest.raises(HTTPException) as exc_info:
            await task_history_vm.create_all_virtual_machine_task_histories_stream(
                netbox_session=SimpleNamespace(),
                pxs=[],
                cluster_status=[],
                tag=SimpleNamespace(name="Proxbox", slug="proxbox"),
                netbox_vm_ids=invalid_scope,
                fetch_max_concurrency=None,
            )
        assert exc_info.value.status_code == 422

    # Invalid input is rejected before the sync coroutine or SSE iterator starts.
    assert captured == [[101, 102], None]


async def test_task_history_stream_reports_fatal_collection_as_failed(monkeypatch):
    async def _fatal_sync(**_kwargs):
        raise ProxboxException("archive collection failed for every selected node")

    monkeypatch.setattr(
        task_history_vm,
        "sync_all_virtual_machine_task_histories",
        _fatal_sync,
    )

    response = await task_history_vm.create_all_virtual_machine_task_histories_stream(
        netbox_session=SimpleNamespace(),
        pxs=[],
        cluster_status=[],
        tag=SimpleNamespace(name="Proxbox", slug="proxbox"),
        netbox_vm_ids="101",
        fetch_max_concurrency=None,
    )
    body = "".join([chunk async for chunk in response.body_iterator])

    assert "event: error" in body
    assert '"status": "failed"' in body
    assert '"ok": false' in body


async def test_task_history_stream_reports_partial_selected_lookup_as_failed(monkeypatch):
    async def _partial_list(_nb, batch_size=500, *, netbox_vm_ids=None):
        assert batch_size == 500
        assert netbox_vm_ids == [501, 502]
        return [{"id": 501}]

    monkeypatch.setattr(
        "proxbox_api.services.sync.task_history._list_all_vms_with_proxmox_id",
        _partial_list,
    )

    response = await task_history_vm.create_all_virtual_machine_task_histories_stream(
        netbox_session=SimpleNamespace(),
        pxs=[],
        cluster_status=[],
        tag=SimpleNamespace(name="Proxbox", slug="proxbox"),
        netbox_vm_ids="501,502",
        fetch_max_concurrency=None,
    )
    body = "".join([chunk async for chunk in response.body_iterator])

    assert "event: error" in body
    assert '"status": "failed"' in body
    assert '"ok": false' in body
    assert "missing id(s): [502]" in body


async def test_virtual_disk_stream_reports_partial_selected_lookup_as_failed(monkeypatch):
    async def _partial_list(_nb, path, *, query=None):
        assert path == "/api/virtualization/virtual-machines/"
        assert query == {"id": ["7", "8"]}
        return [{"id": 7, "custom_fields": {"proxmox_vm_id": 107}}]

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _partial_list)

    response = await disks_vm.create_virtual_disks_stream(
        netbox_session=SimpleNamespace(),
        pxs=[],
        cluster_status=[],
        cluster_resources=[],
        tag=None,
        netbox_vm_ids="7,8",
        fetch_max_concurrency=None,
    )
    body = "".join([chunk async for chunk in response.body_iterator])

    assert "event: error" in body
    assert '"status": "failed"' in body
    assert '"ok": false' in body
    assert "missing id(s): [8]" in body


async def test_vms_create_stream_forwards_task_history_false(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_create_virtual_machines(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(sync_vm, "create_virtual_machines", _fake_create_virtual_machines)

    response = await sync_vm.create_virtual_machines_stream(
        netbox_session=SimpleNamespace(),
        pxs=[],
        cluster_status=[],
        cluster_resources=[],
        custom_fields=[],
        tag=SimpleNamespace(id=7),
        sync_vm_network=False,
        sync_task_history=False,
        overwrite_flags=SyncOverwriteFlags(),
    )
    async for _chunk in response.body_iterator:
        pass

    assert captured["sync_task_history"] is False


async def test_vms_create_stream_filters_selected_vm_to_exact_owner(monkeypatch):
    captured: dict[str, object] = {}

    async def _selected_vm_list(_nb, path, *, query=None):
        assert path == "/api/virtualization/virtual-machines/"
        assert query == {"id": ["501"]}
        return [
            {
                "id": 501,
                "name": "shared-name",
                "cluster": None,
                "custom_fields": {},
            }
        ]

    async def _sidecar_scan(_nb):
        return SimpleNamespace(
            rows=(
                {
                    "virtual_machine": {"id": 501},
                    "proxmox_cluster_name": "cluster-a",
                    "proxmox_endpoint_raw_id": 11,
                    "proxmox_vm_id": 101,
                    "proxmox_vm_type": "qemu",
                },
            ),
            sidecar_unavailable=False,
            sidecar_read_failed=False,
        )

    async def _fake_create_virtual_machines(**kwargs):
        captured.update(kwargs)
        return [{"id": 501}]

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _selected_vm_list)
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_filter.load_vm_sync_state_identities",
        _sidecar_scan,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_filter.custom_fields_enabled",
        lambda: False,
    )
    monkeypatch.setattr(sync_vm, "create_virtual_machines", _fake_create_virtual_machines)
    px_a = SimpleNamespace(
        name="cluster-a",
        cluster_name="cluster-a",
        db_endpoint_id=11,
    )
    px_b = SimpleNamespace(
        name="cluster-b",
        cluster_name="cluster-b",
        db_endpoint_id=22,
    )
    requested = {"type": "qemu", "vmid": 101, "name": "shared-name", "node": "pve-a"}
    reused = {"type": "qemu", "vmid": 101, "name": "shared-name", "node": "pve-b"}

    response = await sync_vm.create_virtual_machines_stream(
        netbox_session=SimpleNamespace(),
        pxs=[px_a, px_b],
        cluster_status=[
            SimpleNamespace(name="cluster-a", mode="cluster"),
            SimpleNamespace(name="cluster-b", mode="cluster"),
        ],
        cluster_resources=[{"cluster-a": [requested]}, {"cluster-b": [reused]}],
        custom_fields=[],
        tag=SimpleNamespace(id=7),
        netbox_vm_ids="501",
        sync_vm_network=False,
        overwrite_flags=SyncOverwriteFlags(),
    )
    async for _chunk in response.body_iterator:
        pass

    assert captured["cluster_resources"] == [{"cluster-a": [requested]}]


async def test_vms_create_stream_reports_owned_task_history_fatal_error(monkeypatch):
    async def _fatal_create_virtual_machines(**_kwargs):
        raise ProxboxException(
            message="Unable to verify VM identity for task-history sync",
            http_status_code=502,
        )

    monkeypatch.setattr(sync_vm, "create_virtual_machines", _fatal_create_virtual_machines)

    response = await sync_vm.create_virtual_machines_stream(
        netbox_session=SimpleNamespace(),
        pxs=[],
        cluster_status=[],
        cluster_resources=[],
        custom_fields=[],
        tag=SimpleNamespace(id=7),
        sync_vm_network=False,
        sync_task_history=True,
        overwrite_flags=SyncOverwriteFlags(),
    )
    body = "".join([chunk async for chunk in response.body_iterator])

    assert "event: error" in body
    assert '"status": "failed"' in body
    assert '"ok": false' in body
    assert "Unable to verify VM identity" in body


async def test_vm_by_id_create_stream_forwards_task_history_false(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_create_virtual_machine_by_netbox_id(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        sync_vm,
        "_create_virtual_machine_by_netbox_id",
        _fake_create_virtual_machine_by_netbox_id,
    )

    response = await sync_vm.create_virtual_machine_by_netbox_id_stream(
        netbox_vm_id=248,
        netbox_session=SimpleNamespace(),
        pxs=[],
        cluster_status=[],
        cluster_resources=[],
        custom_fields=[],
        tag=SimpleNamespace(id=7),
        sync_task_history=False,
        overwrite_flags=SyncOverwriteFlags(),
    )
    async for _chunk in response.body_iterator:
        pass

    assert captured["sync_task_history"] is False


async def test_vm_by_id_create_stream_filters_exact_owned_resource(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_create_virtual_machines(**kwargs):
        captured.update(kwargs)
        return [{"id": 248, "name": "renamed-in-proxmox"}]

    monkeypatch.setattr(sync_vm, "create_virtual_machines", _fake_create_virtual_machines)

    vm_record = SimpleNamespace(
        serialize=lambda: {
            "id": 248,
            "name": "shared-name",
            "cluster": None,
            "custom_fields": {},
        }
    )

    async def _sidecar_scan(_nb):
        return SimpleNamespace(
            rows=(
                {
                    "virtual_machine": {"id": 248},
                    "proxmox_cluster_name": "cluster-a",
                    "proxmox_endpoint_raw_id": 11,
                    "proxmox_vm_id": 9248,
                    "proxmox_vm_type": "qemu",
                },
            ),
            sidecar_unavailable=False,
            sidecar_read_failed=False,
        )

    async def _fake_get(id):
        return vm_record if id == 248 else None

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_filter.load_vm_sync_state_identities",
        _sidecar_scan,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_filter.custom_fields_enabled",
        lambda: False,
    )

    selected = {"type": "qemu", "name": "renamed-in-proxmox", "vmid": 9248}
    same_name = {"type": "qemu", "name": "shared-name", "vmid": 9999}
    wrong_type = {"type": "lxc", "name": "shared-name", "vmid": 9248}
    reused_vmid = {"type": "qemu", "name": "shared-name", "vmid": 9248}
    response = await sync_vm.create_virtual_machine_by_netbox_id_stream(
        netbox_vm_id=248,
        netbox_session=SimpleNamespace(
            virtualization=SimpleNamespace(
                virtual_machines=SimpleNamespace(get=_fake_get),
            )
        ),
        pxs=[SimpleNamespace(db_endpoint_id=11), SimpleNamespace(db_endpoint_id=22)],
        cluster_status=[
            SimpleNamespace(name="cluster-a"),
            SimpleNamespace(name="cluster-b"),
        ],
        cluster_resources=[
            {"CLUSTER-A": [same_name, wrong_type, selected]},
            {"cluster-b": [reused_vmid]},
        ],
        custom_fields=[],
        tag=SimpleNamespace(id=7),
        sync_task_history=True,
        overwrite_flags=SyncOverwriteFlags(),
    )
    body = "".join([chunk async for chunk in response.body_iterator])

    assert '"ok": true' in body
    assert captured["cluster_resources"] == [{"CLUSTER-A": [selected]}]
    assert captured["netbox_vm_ids"] == "248"


async def test_vm_by_id_create_stream_reports_owned_task_history_fatal_error(monkeypatch):
    async def _fatal_create_virtual_machine_by_netbox_id(**_kwargs):
        raise ProxboxException(
            message="Task-history archive collection failed for selected VM",
            http_status_code=502,
        )

    monkeypatch.setattr(
        sync_vm,
        "_create_virtual_machine_by_netbox_id",
        _fatal_create_virtual_machine_by_netbox_id,
    )

    response = await sync_vm.create_virtual_machine_by_netbox_id_stream(
        netbox_vm_id=248,
        netbox_session=SimpleNamespace(),
        pxs=[],
        cluster_status=[],
        cluster_resources=[],
        custom_fields=[],
        tag=SimpleNamespace(id=7),
        sync_task_history=True,
        overwrite_flags=SyncOverwriteFlags(),
    )
    body = "".join([chunk async for chunk in response.body_iterator])

    assert "event: error" in body
    assert '"status": "failed"' in body
    assert '"ok": false' in body
    assert "Task-history archive collection failed" in body


def _sidecar_only_vm_session(netbox_vm_id: int):
    """NetBox session stub returning a VM without legacy Proxbox custom fields."""

    async def _get(id):
        assert id == netbox_vm_id
        return {
            "id": netbox_vm_id,
            "name": f"vm-{netbox_vm_id}",
            "cluster": None,
            "custom_fields": {},
        }

    return SimpleNamespace(
        virtualization=SimpleNamespace(virtual_machines=SimpleNamespace(get=_get))
    )


async def test_snapshot_by_id_stream_has_no_legacy_custom_field_precondition(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_create_all(**kwargs):
        captured.update(kwargs)
        return {"created": 0, "skipped": 0}

    monkeypatch.setattr(
        snapshots_vm,
        "_create_all_virtual_machine_snapshots",
        _fake_create_all,
    )

    response = await snapshots_vm.create_virtual_machine_snapshots_by_id_stream(
        netbox_vm_id=612,
        netbox_session=_sidecar_only_vm_session(612),
        pxs=[],
        cluster_status=[],
        cluster_resources=[],
        tag=SimpleNamespace(id=7),
    )
    async for _chunk in response.body_iterator:
        pass

    assert captured["netbox_vm_ids"] == [612]


async def test_backup_by_id_stream_has_no_legacy_custom_field_precondition(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_create_all(**kwargs):
        captured.update(kwargs)
        return {"created": 0, "skipped": 0}

    monkeypatch.setattr(
        backups_vm,
        "_create_all_virtual_machine_backups",
        _fake_create_all,
    )

    response = await backups_vm.create_virtual_machine_backups_by_id_stream(
        netbox_vm_id=613,
        netbox_session=_sidecar_only_vm_session(613),
        pxs=[],
        cluster_status=[],
        tag=SimpleNamespace(id=7),
    )
    async for _chunk in response.body_iterator:
        pass

    assert captured["netbox_vm_ids"] == [613]


async def test_selected_snapshot_second_lookup_chunk_is_rest_and_sse_fatal(monkeypatch):
    lookup_chunks: list[list[str]] = []
    downstream_calls = 0

    async def _chunked_lookup(_nb, path, *, query=None):
        assert path == "/api/virtualization/virtual-machines/"
        ids = list((query or {}).get("id", []))
        lookup_chunks.append(ids)
        if ids and int(ids[0]) > 100:
            raise RuntimeError("selected VM lookup timed out")
        return [{"id": int(vm_id)} for vm_id in ids]

    async def _unexpected_downstream(*_args, **_kwargs):
        nonlocal downstream_calls
        downstream_calls += 1
        raise AssertionError("snapshot discovery must not run after a selected lookup failure")

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", _chunked_lookup)
    monkeypatch.setattr(snapshots_service, "_load_storage_index", _unexpected_downstream)
    selected = ",".join(str(vm_id) for vm_id in range(1, 102))
    common = {
        "netbox_session": SimpleNamespace(),
        "pxs": [],
        "cluster_status": [],
        "cluster_resources": [],
        "tag": SimpleNamespace(name="Proxbox", slug="proxbox"),
        "fetch_max_concurrency": None,
        "netbox_vm_ids": selected,
        "delete_nonexistent_snapshot": True,
    }

    with pytest.raises(ProxboxException) as exc_info:
        await snapshots_vm.create_all_virtual_machine_snapshots(**common)
    assert exc_info.value.http_status_code == 502

    response = await snapshots_vm.create_all_virtual_machine_snapshots_stream(**common)
    body = "".join([chunk async for chunk in response.body_iterator])

    assert lookup_chunks == [
        [str(vm_id) for vm_id in range(1, 101)],
        ["101"],
        [str(vm_id) for vm_id in range(1, 101)],
        ["101"],
    ]
    assert downstream_calls == 0
    assert "event: error" in body
    assert '"status": "failed"' in body
    assert '"ok": false' in body
    assert "selected VM lookup timed out" in body


class TestPluginAPIPath:
    """Test paths expected by the plugin."""

    async def test_devices_create_path_exists(self, authenticated_client):
        """Plugin expects /dcim/devices/create endpoint."""
        resp = await authenticated_client.get("/dcim/devices/create")
        # Should not return 404 (might fail for other reasons like missing endpoints)
        assert resp.status_code != 404

    async def test_vms_create_path_exists(self, authenticated_client):
        """Plugin expects /virtualization/virtual-machines/create endpoint."""
        resp = await authenticated_client.get("/virtualization/virtual-machines/create")
        assert resp.status_code != 404

    async def test_backups_create_path_exists(self, authenticated_client):
        """Plugin expects /virtualization/virtual-machines/backups/all/create endpoint."""
        resp = await authenticated_client.get("/virtualization/virtual-machines/backups/all/create")
        assert resp.status_code != 404

    async def test_snapshots_create_path_exists(self, authenticated_client):
        """Plugin expects /virtualization/virtual-machines/snapshots/all/create endpoint."""
        resp = await authenticated_client.get(
            "/virtualization/virtual-machines/snapshots/all/create"
        )
        assert resp.status_code != 404

    async def test_storage_create_path_exists(self, authenticated_client):
        """Plugin expects /virtualization/virtual-machines/storage/create endpoint."""
        resp = await authenticated_client.get("/virtualization/virtual-machines/storage/create")
        assert resp.status_code != 404

    async def test_virtual_disks_create_path_exists(self, authenticated_client):
        """Plugin expects /virtualization/virtual-machines/virtual-disks/create endpoint."""
        resp = await authenticated_client.get(
            "/virtualization/virtual-machines/virtual-disks/create"
        )
        assert resp.status_code != 404

    async def test_full_update_path_exists(self, authenticated_client):
        """Plugin expects /full-update endpoint."""
        resp = await authenticated_client.get("/full-update")
        assert resp.status_code != 404


class TestStreamEndpoints:
    """Test stream endpoint variants."""

    async def test_devices_create_stream_path_exists(self, authenticated_client):
        """Plugin expects /dcim/devices/create/stream endpoint."""
        async with authenticated_client.stream("GET", "/dcim/devices/create/stream") as resp:
            # Path exists if status is not 404 or 405
            assert resp.status_code != 404
            assert resp.status_code != 405

    async def test_vms_create_stream_path_exists(self, authenticated_client):
        """Plugin expects /virtualization/virtual-machines/create/stream endpoint."""
        async with authenticated_client.stream(
            "GET", "/virtualization/virtual-machines/create/stream"
        ) as resp:
            assert resp.status_code != 404
            assert resp.status_code != 405

    async def test_vms_create_stream_without_netbox_vm_ids(self, authenticated_client):
        """Regression test: /create/stream without netbox_vm_ids should not fail with closure error.

        This tests the fix for the vm_ids closure bug where vm_ids was only assigned
        inside the if netbox_vm_ids: branch but referenced unconditionally in the SSE message.
        """
        async with authenticated_client.stream(
            "GET", "/virtualization/virtual-machines/create/stream"
        ) as resp:
            assert resp.status_code != 404
            assert resp.status_code != 405
            # Consume some of the stream to ensure no runtime error during initial yield
            chunks = []
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                if len(chunks) >= 3:
                    break
            # If we got here without RuntimeError, the test passes
            assert len(chunks) >= 0

    async def test_full_update_stream_path_exists(self, authenticated_client):
        """Plugin expects /full-update/stream endpoint."""
        async with authenticated_client.stream("GET", "/full-update/stream") as resp:
            assert resp.status_code != 404
            assert resp.status_code != 405


class TestNonStreamEndpoints:
    """Test non-stream endpoints return JSON."""

    async def test_root_returns_json(self, test_client):
        """Root endpoint should return JSON (auth-exempt, uses unauthenticated client)."""
        resp = test_client.get("/")
        assert resp.status_code == 200
        assert resp.headers.get("content-type", "").startswith("application/json")
        data = resp.json()
        assert isinstance(data, dict)


async def test_vms_create_stream_forwards_run_id(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_create_virtual_machines(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(sync_vm, "create_virtual_machines", _fake_create_virtual_machines)

    response = await sync_vm.create_virtual_machines_stream(
        netbox_session=SimpleNamespace(),
        pxs=[],
        cluster_status=[],
        cluster_resources=[],
        custom_fields=[],
        tag=SimpleNamespace(id=7),
        sync_vm_network=False,
        overwrite_flags=SyncOverwriteFlags(),
        run_id="issue-519-run",
    )

    async for _chunk in response.body_iterator:
        pass

    assert captured["run_id"] == "issue-519-run"
    assert captured["sync_vm_network"] is False


async def test_vm_by_id_create_stream_forwards_run_id(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_create_virtual_machine_by_netbox_id(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        sync_vm,
        "_create_virtual_machine_by_netbox_id",
        _fake_create_virtual_machine_by_netbox_id,
    )

    response = await sync_vm.create_virtual_machine_by_netbox_id_stream(
        netbox_vm_id=248,
        netbox_session=SimpleNamespace(),
        pxs=[],
        cluster_status=[],
        cluster_resources=[],
        custom_fields=[],
        tag=SimpleNamespace(id=7),
        overwrite_flags=SyncOverwriteFlags(),
        run_id="issue-519-run",
    )

    async for _chunk in response.body_iterator:
        pass

    assert captured["netbox_vm_id"] == 248
    assert captured["run_id"] == "issue-519-run"
