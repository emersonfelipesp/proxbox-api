from __future__ import annotations

import pytest

from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import RestRecord
from proxbox_api.services.sync import sync_state_writer as writer
from proxbox_api.services.sync.vm_create import create_or_update_virtual_machine


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.existing: object | None = None
        self.first_results: list[object | None] | None = None
        self.first_error: Exception | None = None
        self.create_error: Exception | None = None
        self.patch_error: Exception | None = None

    async def first(self, _nb: object, path: str, *, query: dict[str, object] | None = None):
        self.calls.append({"method": "GET", "path": path, "query": query})
        if self.first_error is not None:
            raise self.first_error
        if self.first_results is not None:
            return self.first_results.pop(0) if self.first_results else None
        return self.existing

    async def create(
        self,
        _nb: object,
        path: str,
        payload: dict[str, object],
        *,
        lookup: dict[str, object] | None = None,
    ):
        self.calls.append({"method": "POST", "path": path, "payload": payload, "lookup": lookup})
        if self.create_error is not None:
            raise self.create_error
        return {"id": 900, **payload}

    async def patch(
        self,
        _nb: object,
        path: str,
        record_id: int,
        payload: dict[str, object],
    ):
        self.calls.append(
            {"method": "PATCH", "path": path, "record_id": record_id, "payload": payload}
        )
        if self.patch_error is not None:
            raise self.patch_error
        return {"id": record_id, **payload}


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    writer.reset_sidecar_availability_cache()
    rec = _Recorder()
    monkeypatch.setattr(writer, "rest_first_async", rec.first)
    monkeypatch.setattr(writer, "rest_create_async", rec.create)
    monkeypatch.setattr(writer, "rest_patch_async", rec.patch)
    return rec


@pytest.mark.asyncio
async def test_vm_sidecar_create_uses_live_payload_when_parent_has_no_custom_fields(
    recorder: _Recorder,
) -> None:
    await writer.write_virtual_machine_sync_state(
        object(),
        virtual_machine_id=123,
        custom_fields={
            "proxmox_vm_id": 101,
            "proxmox_vm_type": "qemu",
            "proxmox_start_at_boot": True,
            "proxmox_unprivileged_container": False,
            "proxmox_qemu_agent": True,
            "proxmox_search_domain": "example.test",
            "proxmox_node": "pve-a",
            "proxmox_cluster": "cluster-a",
            "proxmox_status": "running",
            "proxmox_last_updated": "2026-07-20T00:00:00+00:00",
            "proxmox_endpoint_id": 44,
        },
        overwrite_custom_fields=True,
    )

    assert recorder.calls[0] == {
        "method": "GET",
        "path": writer.VM_SYNC_STATE_PATH,
        "query": {"virtual_machine_id": 123, "limit": 2},
    }
    post = recorder.calls[1]
    assert post["method"] == "POST"
    assert post["path"] == writer.VM_SYNC_STATE_PATH
    payload = post["payload"]
    assert payload["virtual_machine"] == {"id": 123}
    assert payload["proxmox_vm_id"] == 101
    assert payload["proxmox_node_name"] == "pve-a"
    assert payload["proxmox_cluster_name"] == "cluster-a"
    assert payload["proxmox_endpoint_raw_id"] == 44


@pytest.mark.asyncio
async def test_vm_sidecar_patches_existing_retrecord_parent_row(recorder: _Recorder) -> None:
    recorder.existing = RestRecord(
        object(),
        writer.VM_SYNC_STATE_PATH,
        {"id": 55, "virtual_machine": {"id": 123}},
    )

    await writer.write_virtual_machine_sync_state(
        object(),
        virtual_machine_id=123,
        custom_fields={"proxmox_vm_id": 102, "proxmox_vm_type": "lxc"},
        overwrite_custom_fields=True,
    )

    assert recorder.calls[-1] == {
        "method": "PATCH",
        "path": writer.VM_SYNC_STATE_PATH,
        "record_id": 55,
        "payload": {"proxmox_vm_id": 102, "proxmox_vm_type": "lxc"},
    }


@pytest.mark.asyncio
async def test_sidecar_create_duplicate_conflict_relooks_up_and_patches(
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder.first_results = [
        None,
        RestRecord(
            object(),
            writer.VM_INTERFACE_SYNC_STATE_PATH,
            {"id": 77, "vm_interface": {"id": 40}},
        ),
    ]
    cleared_paths: list[str] = []
    recorder.create_error = ProxboxException(
        message="NetBox REST request failed",
        detail='{"vm_interface":["This object already has a Proxbox sync-state row."]}',
    )
    monkeypatch.setattr(
        writer,
        "clear_rest_get_cache_for_path",
        lambda _nb, path: cleared_paths.append(path),
    )

    result = await writer.write_vm_interface_sync_state(
        object(),
        vm_interface_id=40,
        proxbox_bridge_id=400,
        overwrite_custom_fields=True,
    )

    assert result == {
        "id": 77,
        "proxbox_bridge": {"id": 400},
        "proxbox_bridge_raw_id": None,
        "proxbox_bridge_raw_value": "",
    }
    assert [call["method"] for call in recorder.calls] == ["GET", "POST", "GET", "PATCH"]
    assert cleared_paths == [writer.VM_INTERFACE_SYNC_STATE_PATH]
    assert recorder.calls[-1] == {
        "method": "PATCH",
        "path": writer.VM_INTERFACE_SYNC_STATE_PATH,
        "record_id": 77,
        "payload": {
            "proxbox_bridge": {"id": 400},
            "proxbox_bridge_raw_id": None,
            "proxbox_bridge_raw_value": "",
        },
    }


@pytest.mark.asyncio
async def test_sidecar_writer_respects_custom_field_gate(recorder: _Recorder) -> None:
    await writer.write_virtual_machine_sync_state(
        object(),
        virtual_machine_id=123,
        custom_fields={"proxmox_vm_id": 101},
        overwrite_custom_fields=False,
    )

    assert recorder.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("func", "kwargs", "path", "parent_field", "expected"),
    [
        (
            writer.write_device_sync_state,
            {
                "device_id": 10,
                "proxmox_last_updated": "2026-07-20T00:01:00+00:00",
                "proxmox_node_name": "pve-a",
                "proxmox_cluster_name": "cluster-a",
                "overwrite_custom_fields": True,
            },
            writer.DEVICE_SYNC_STATE_PATH,
            "device",
            {
                "device": {"id": 10},
                "proxmox_last_updated": "2026-07-20T00:01:00+00:00",
                "proxmox_node_name": "pve-a",
                "proxmox_cluster_name": "cluster-a",
            },
        ),
        (
            writer.write_cluster_sync_state,
            {
                "cluster_id": 20,
                "proxmox_last_updated": "2026-07-20T00:02:00+00:00",
                "proxmox_cluster_name": "cluster-a",
                "overwrite_custom_fields": True,
            },
            writer.CLUSTER_SYNC_STATE_PATH,
            "cluster",
            {
                "cluster": {"id": 20},
                "proxmox_last_updated": "2026-07-20T00:02:00+00:00",
                "proxmox_cluster_name": "cluster-a",
            },
        ),
        (
            writer.write_virtual_disk_sync_state,
            {
                "virtual_disk_id": 30,
                "proxbox_storage_id": 300,
                "overwrite_custom_fields": True,
            },
            writer.VIRTUAL_DISK_SYNC_STATE_PATH,
            "virtual_disk",
            {
                "virtual_disk": {"id": 30},
                "proxbox_storage": {"id": 300},
                "proxbox_storage_raw_value": "",
            },
        ),
        (
            writer.write_vm_interface_sync_state,
            {
                "vm_interface_id": 40,
                "proxbox_bridge_id": 400,
                "overwrite_custom_fields": True,
            },
            writer.VM_INTERFACE_SYNC_STATE_PATH,
            "vm_interface",
            {
                "vm_interface": {"id": 40},
                "proxbox_bridge": {"id": 400},
                "proxbox_bridge_raw_value": "",
            },
        ),
    ],
)
async def test_each_sidecar_writer_posts_expected_contract(
    recorder: _Recorder,
    func,
    kwargs: dict[str, object],
    path: str,
    parent_field: str,
    expected: dict[str, object],
) -> None:
    await func(object(), **kwargs)

    assert recorder.calls[0]["query"] == {
        f"{parent_field}_id": expected[parent_field]["id"],
        "limit": 2,
    }
    post_payload = recorder.calls[1]["payload"]
    for key, value in expected.items():
        assert post_payload[key] == value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ProxboxException(message="Not found", detail="HTTP 404"),
        ProxboxException(message="Not implemented", detail="HTTP 501"),
    ],
)
async def test_sidecar_writer_skips_older_plugin_without_failing(
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    recorder.first_error = error
    warnings: list[tuple[object, ...]] = []
    monkeypatch.setattr(writer.logger, "warning", lambda *args, **_kwargs: warnings.append(args))

    result = await writer.write_cluster_sync_state(
        object(),
        cluster_id=20,
        proxmox_last_updated="2026-07-20T00:00:00+00:00",
        overwrite_custom_fields=True,
    )

    assert result is None
    assert len(recorder.calls) == 1
    assert warnings
    assert "unavailable" in str(warnings[0])

    await writer.write_cluster_sync_state(
        object(),
        cluster_id=20,
        proxmox_last_updated="2026-07-20T00:00:00+00:00",
        overwrite_custom_fields=True,
    )

    assert len(recorder.calls) == 1
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_sidecar_unavailable_memo_is_cleared_between_sync_runs(
    recorder: _Recorder,
) -> None:
    recorder.first_error = ProxboxException(message="Not found", detail="HTTP 404")

    first_result = await writer.write_cluster_sync_state(
        object(),
        cluster_id=20,
        proxmox_last_updated="2026-07-20T00:00:00+00:00",
        overwrite_custom_fields=True,
    )

    assert first_result is None
    assert len(recorder.calls) == 1

    recorder.first_error = None
    writer.reset_sidecar_availability_cache()

    second_result = await writer.write_cluster_sync_state(
        object(),
        cluster_id=20,
        proxmox_last_updated="2026-07-20T00:00:00+00:00",
        overwrite_custom_fields=True,
    )

    assert second_result is not None
    assert [call["method"] for call in recorder.calls] == ["GET", "GET", "POST"]


@pytest.mark.asyncio
async def test_sidecar_writer_tolerates_transient_failure(
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder.first_error = TimeoutError("temporary timeout")
    warnings: list[tuple[object, ...]] = []
    monkeypatch.setattr(writer.logger, "warning", lambda *args, **_kwargs: warnings.append(args))

    result = await writer.write_device_sync_state(
        object(),
        device_id=10,
        proxmox_last_updated="2026-07-20T00:00:00+00:00",
        overwrite_custom_fields=True,
    )

    assert result is None
    assert len(recorder.calls) == 1
    assert warnings
    assert "sync will continue" in str(warnings[0])


@pytest.mark.asyncio
async def test_sidecar_writer_isolates_create_failure(
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder.create_error = RuntimeError("create failed")
    warnings: list[tuple[object, ...]] = []
    monkeypatch.setattr(writer.logger, "warning", lambda *args, **_kwargs: warnings.append(args))

    result = await writer.write_device_sync_state(
        object(),
        device_id=10,
        proxmox_last_updated="2026-07-20T00:00:00+00:00",
        overwrite_custom_fields=True,
    )

    assert result is None
    assert [call["method"] for call in recorder.calls] == ["GET", "POST"]
    assert warnings
    assert "sync will continue" in str(warnings[0])


@pytest.mark.asyncio
async def test_sidecar_writer_isolates_patch_failure(
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder.existing = RestRecord(
        object(),
        writer.DEVICE_SYNC_STATE_PATH,
        {"id": 55, "device": {"id": 10}},
    )
    recorder.patch_error = RuntimeError("patch failed")
    warnings: list[tuple[object, ...]] = []
    monkeypatch.setattr(writer.logger, "warning", lambda *args, **_kwargs: warnings.append(args))

    result = await writer.write_device_sync_state(
        object(),
        device_id=10,
        proxmox_last_updated="2026-07-20T00:00:00+00:00",
        overwrite_custom_fields=True,
    )

    assert result is None
    assert [call["method"] for call in recorder.calls] == ["GET", "PATCH"]
    assert warnings
    assert "sync will continue" in str(warnings[0])


@pytest.mark.asyncio
async def test_create_or_update_vm_writes_sidecar_from_live_payload_without_record_custom_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _fake_detect_version(_nb: object) -> tuple[int, int, int]:
        return (4, 5, 0)

    async def _fake_reconcile(*_args, **_kwargs):
        return {"id": 123, "name": "vm-101"}

    async def _fake_cloudinit(*_args, **_kwargs) -> None:
        return None

    async def _fake_sidecar(_nb: object, **kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_create.detect_netbox_version", _fake_detect_version
    )
    monkeypatch.setattr("proxbox_api.services.sync.vm_create.rest_reconcile_async", _fake_reconcile)
    monkeypatch.setattr("proxbox_api.services.sync.vm_create.sync_vm_cloudinit", _fake_cloudinit)
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_create.write_virtual_machine_sync_state",
        _fake_sidecar,
    )

    await create_or_update_virtual_machine(
        netbox_session=object(),
        proxmox_resource={
            "vmid": 101,
            "name": "vm-101",
            "node": "pve-a",
            "type": "qemu",
            "status": "running",
            "maxcpu": 2,
            "maxmem": 2_147_483_648,
            "maxdisk": 10_737_418_240,
        },
        proxmox_config={"onboot": 1, "agent": 1},
        cluster_id=11,
        device_id=22,
        role_id=33,
        tag_id=44,
        tag_refs=[{"id": 44}],
        cluster_name="cluster-a",
        endpoint_id=55,
    )

    assert captured["virtual_machine_id"] == 123
    assert captured["overwrite_custom_fields"] is True
    custom_fields = captured["custom_fields"]
    assert custom_fields["proxmox_vm_id"] == 101
    assert custom_fields["proxmox_node"] == "pve-a"
    assert custom_fields["proxmox_cluster"] == "cluster-a"
    assert custom_fields["proxmox_endpoint_id"] == 55
