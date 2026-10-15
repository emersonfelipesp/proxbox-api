from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from netbox_sdk.client import ApiResponse

from proxbox_api import netbox_rest
from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import RestRecord
from proxbox_api.proxmox_to_netbox.models import ProxmoxVmResourceInput
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
        created = {"id": 900, **payload}
        self.existing = created
        return created

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
        current = writer._record_to_dict(self.existing) or {}
        patched = {**current, "id": record_id, **payload}
        self.existing = patched
        return patched


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
@pytest.mark.parametrize("custom_fields", [{"proxmox_vm_id": 101}, None])
async def test_vm_sidecar_writes_proxmox_name_when_custom_field_gate_is_closed(
    recorder: _Recorder,
    custom_fields: dict[str, object] | None,
) -> None:
    await writer.write_virtual_machine_sync_state(
        object(),
        virtual_machine_id=123,
        custom_fields=custom_fields,
        overwrite_custom_fields=False,
        proxmox_vm_name="web-renamed",
    )

    assert recorder.calls[0] == {
        "method": "GET",
        "path": writer.VM_SYNC_STATE_PATH,
        "query": {"virtual_machine_id": 123, "limit": 2},
    }
    post = recorder.calls[1]
    assert post["method"] == "POST"
    assert post["path"] == writer.VM_SYNC_STATE_PATH
    assert post["payload"] == {
        "virtual_machine": {"id": 123},
        "proxmox_vm_name": "web-renamed",
    }


@pytest.mark.asyncio
async def test_vm_sidecar_writes_role_snapshot_when_custom_field_gate_is_closed(
    recorder: _Recorder,
) -> None:
    await writer.write_virtual_machine_sync_state(
        object(),
        virtual_machine_id=123,
        custom_fields=None,
        overwrite_custom_fields=False,
        proxmox_last_synced_role_id=31,
    )

    assert recorder.calls[-1] == {
        "method": "POST",
        "path": writer.VM_SYNC_STATE_PATH,
        "payload": {
            "virtual_machine": {"id": 123},
            "proxmox_last_synced_role_id": 31,
        },
        "lookup": {"virtual_machine_id": 123},
    }


@pytest.mark.asyncio
async def test_required_role_snapshot_retries_and_surfaces_failure(
    recorder: _Recorder,
) -> None:
    recorder.create_error = ProxboxException(
        message="NetBox REST request failed",
        detail="temporary sidecar failure",
        http_status_code=503,
    )

    with pytest.raises(ProxboxException, match="NetBox REST request failed"):
        await writer.write_virtual_machine_sync_state(
            object(),
            virtual_machine_id=123,
            custom_fields=None,
            overwrite_custom_fields=False,
            proxmox_last_synced_role_id=31,
        )

    assert [call["method"] for call in recorder.calls] == [
        "GET",
        "POST",
        "GET",
        "POST",
        "GET",
        "POST",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_snapshot_id", [11, None])
async def test_exact_role_snapshot_restore_accepts_commit_before_response_loss(
    monkeypatch: pytest.MonkeyPatch,
    previous_snapshot_id: int | None,
) -> None:
    from proxbox_api.services.sync import sync_state_reader

    current_snapshot_id = 20
    write_calls = 0

    async def _commit_then_error(*_args: object, **kwargs: object) -> None:
        nonlocal current_snapshot_id, write_calls
        write_calls += 1
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        value = payload["proxmox_last_synced_role_id"]
        current_snapshot_id = value if isinstance(value, int) else None
        raise RuntimeError("response lost after exact snapshot commit")

    async def _read(*_args: object, **_kwargs: object):
        return sync_state_reader.VMRoleSnapshotRead(
            snapshot_id=current_snapshot_id,
            verified=True,
        )

    monkeypatch.setattr(writer, "_upsert_parent_sidecar", _commit_then_error)
    monkeypatch.setattr(sync_state_reader, "read_vm_last_synced_role", _read)

    await writer.write_vm_role_snapshot_exact(
        object(),
        virtual_machine_id=123,
        snapshot_id=previous_snapshot_id,
        attempts=1,
    )

    assert write_calls == 1
    assert current_snapshot_id == previous_snapshot_id


@pytest.mark.asyncio
@pytest.mark.parametrize("proxmox_vm_name", [None, "", "   "])
async def test_vm_sidecar_custom_field_gate_still_skips_blank_proxmox_name(
    recorder: _Recorder,
    proxmox_vm_name: object,
) -> None:
    await writer.write_virtual_machine_sync_state(
        object(),
        virtual_machine_id=123,
        custom_fields={"proxmox_vm_id": 101},
        overwrite_custom_fields=False,
        proxmox_vm_name=proxmox_vm_name,
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
        ProxboxException(message="Not found", detail="Not found.", http_status_code=404),
        ProxboxException(
            message="Not implemented", detail="Not implemented.", http_status_code=501
        ),
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
    recorder.first_error = ProxboxException(
        message="Not found", detail="Not found.", http_status_code=404
    )

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
async def test_device_sidecar_writer_persists_endpoint_identity(
    recorder: _Recorder,
) -> None:
    result = await writer.write_device_sync_state(
        object(),
        device_id=10,
        proxmox_last_updated="2026-07-20T00:00:00+00:00",
        proxmox_node_name="pve01",
        proxmox_cluster_name="cluster-a",
        proxmox_endpoint_raw_id=501,
        overwrite_custom_fields=True,
    )

    assert result is not None
    write_call = next(
        call for call in reversed(recorder.calls) if call["method"] in {"POST", "PATCH"}
    )
    assert write_call["payload"]["proxmox_endpoint_raw_id"] == 501


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_endpoint_id", [None, 501], ids=["unclaimed", "same-owner"])
async def test_device_sidecar_writer_allows_unclaimed_and_same_endpoint_rows(
    recorder: _Recorder,
    existing_endpoint_id: int | None,
) -> None:
    recorder.existing = RestRecord(
        object(),
        writer.DEVICE_SYNC_STATE_PATH,
        {
            "id": 55,
            "device": {"id": 10},
            "proxmox_endpoint_raw_id": existing_endpoint_id,
        },
    )

    result = await writer.write_device_sync_state(
        object(),
        device_id=10,
        proxmox_last_updated="2026-07-20T00:00:00+00:00",
        proxmox_endpoint_raw_id=501,
        overwrite_custom_fields=True,
    )

    assert result is not None
    patch_call = next(call for call in reversed(recorder.calls) if call["method"] == "PATCH")
    assert patch_call["payload"]["proxmox_endpoint_raw_id"] == 501


@pytest.mark.asyncio
async def test_device_sidecar_writer_rejects_another_endpoint_owner(
    recorder: _Recorder,
) -> None:
    recorder.existing = RestRecord(
        object(),
        writer.DEVICE_SYNC_STATE_PATH,
        {"id": 55, "device": {"id": 10}, "proxmox_endpoint_raw_id": 502},
    )

    with pytest.raises(writer.DeviceSyncStateOwnershipConflict, match="endpoint 502 owns"):
        await writer.write_device_sync_state(
            object(),
            device_id=10,
            proxmox_last_updated="2026-07-20T00:00:00+00:00",
            proxmox_endpoint_raw_id=501,
            overwrite_custom_fields=True,
        )

    assert [call["method"] for call in recorder.calls] == ["GET"]


@pytest.mark.asyncio
async def test_device_sidecar_writer_rechecks_owner_after_duplicate_create_race(
    recorder: _Recorder,
) -> None:
    recorder.first_results = [
        None,
        RestRecord(
            object(),
            writer.DEVICE_SYNC_STATE_PATH,
            {"id": 55, "device": {"id": 10}, "proxmox_endpoint_raw_id": 502},
        ),
    ]
    recorder.create_error = ProxboxException(
        message="NetBox REST request failed",
        detail='{"device":["This object already has a Proxbox sync-state row."]}',
    )

    with pytest.raises(writer.DeviceSyncStateOwnershipConflict, match="endpoint 502 owns"):
        await writer.write_device_sync_state(
            object(),
            device_id=10,
            proxmox_last_updated="2026-07-20T00:00:00+00:00",
            proxmox_endpoint_raw_id=501,
            overwrite_custom_fields=True,
        )

    assert [call["method"] for call in recorder.calls] == ["GET", "POST", "GET"]


@pytest.mark.asyncio
async def test_upsert_validates_real_create_recovery_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Api:
        def __init__(self) -> None:
            self.client = self
            self.sidecar: dict[str, object] | None = None

        async def request(self, method: str, _path: str, **_kwargs: object) -> ApiResponse:
            if method == "GET":
                rows = [] if self.sidecar is None else [self.sidecar]
                return ApiResponse(
                    status=200,
                    text=json.dumps({"count": len(rows), "next": None, "results": rows}),
                )
            if method == "POST":
                self.sidecar = {
                    "id": 55,
                    "device": {"id": 10},
                    "proxmox_endpoint_raw_id": 502,
                }
                raise RuntimeError("Server disconnected")
            raise AssertionError(method)

    def _validate(existing: object) -> None:
        writer.validate_device_sync_state_owner(
            existing,
            expected_endpoint_id=501,
            device_id=10,
        )

    netbox_rest._reset_netbox_globals()
    writer.reset_sidecar_availability_cache()
    monkeypatch.setattr(netbox_rest, "_resolve_netbox_max_retries", lambda: 1)
    monkeypatch.setattr(netbox_rest, "_resolve_netbox_retry_delay", lambda: 0.0)

    with pytest.raises(writer.DeviceSyncStateOwnershipConflict, match="endpoint 502 owns"):
        await writer._upsert_parent_sidecar(
            _Api(),
            path=writer.DEVICE_SYNC_STATE_PATH,
            parent_field="device",
            parent_id=10,
            payload={"proxmox_endpoint_raw_id": 501},
            existing_validator=_validate,
        )


@pytest.mark.asyncio
async def test_device_sidecar_writer_maps_atomic_plugin_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner: int | None = None
    initial_reads = 0
    both_read = asyncio.Event()

    async def _first(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal initial_reads
        snapshot = owner
        if initial_reads < 2:
            initial_reads += 1
            if initial_reads == 2:
                both_read.set()
            await both_read.wait()
        return {"id": 55, "device": {"id": 10}, "proxmox_endpoint_raw_id": snapshot}

    async def _patch(
        _nb: object,
        _path: str,
        _record_id: int,
        payload: dict[str, object],
    ) -> dict[str, object]:
        nonlocal owner
        requested = int(payload["proxmox_endpoint_raw_id"])
        if owner is not None and owner != requested:
            raise ProxboxException(
                message="NetBox REST request failed",
                detail={"code": "endpoint_ownership_conflict"},
                http_status_code=409,
            )
        owner = requested
        return {"id": 55, "device": {"id": 10}, **payload}

    monkeypatch.setattr(writer, "rest_first_async", _first)
    monkeypatch.setattr(writer, "rest_patch_async", _patch)
    monkeypatch.setattr(writer, "clear_rest_get_cache_for_path", lambda *_args: None)

    outcomes = await asyncio.gather(
        *(
            writer.write_device_sync_state(
                object(),
                device_id=10,
                proxmox_last_updated=None,
                proxmox_endpoint_raw_id=endpoint_id,
                overwrite_custom_fields=False,
            )
            for endpoint_id in (501, 502)
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert (
        sum(isinstance(outcome, writer.DeviceSyncStateOwnershipConflict) for outcome in outcomes)
        == 1
    )
    assert owner in {501, 502}


@pytest.mark.asyncio
async def test_device_sidecar_writer_maps_real_rest_409_conflict() -> None:
    class _Api:
        def __init__(self) -> None:
            self.client = SimpleNamespace(request=self.request)

        async def request(
            self,
            method: str,
            _path: str,
            **_kwargs: object,
        ) -> ApiResponse:
            if method == "GET":
                return ApiResponse(
                    status=200,
                    text=json.dumps(
                        {
                            "count": 1,
                            "next": None,
                            "results": [
                                {
                                    "id": 55,
                                    "device": {"id": 10},
                                    "proxmox_endpoint_raw_id": None,
                                }
                            ],
                        }
                    ),
                )
            if method == "PATCH":
                return ApiResponse(
                    status=409,
                    text=json.dumps(
                        {
                            "detail": "Device ownership belongs to another endpoint.",
                            "code": "endpoint_ownership_conflict",
                        }
                    ),
                )
            raise AssertionError(method)

    netbox_rest._reset_netbox_globals()
    writer.reset_sidecar_availability_cache()

    with pytest.raises(writer.DeviceSyncStateOwnershipConflict, match="another endpoint"):
        await writer.write_device_sync_state(
            _Api(),
            device_id=10,
            proxmox_last_updated=None,
            proxmox_endpoint_raw_id=501,
            overwrite_custom_fields=False,
        )


@pytest.mark.asyncio
async def test_device_sidecar_writer_rejects_exposed_null_readback() -> None:
    class _Api:
        def __init__(self) -> None:
            self.client = self

        async def request(self, method: str, _path: str, **_kwargs: object) -> ApiResponse:
            row = {
                "id": 55,
                "device": {"id": 10},
                "proxmox_endpoint_raw_id": None,
            }
            if method == "GET":
                return ApiResponse(
                    status=200,
                    text=json.dumps({"count": 1, "next": None, "results": [row]}),
                )
            if method == "PATCH":
                return ApiResponse(status=200, text=json.dumps(row))
            raise AssertionError(method)

    netbox_rest._reset_netbox_globals()
    writer.reset_sidecar_availability_cache()

    with pytest.raises(writer.DeviceSyncStateOwnershipConflict, match="endpoint None owns"):
        await writer.write_device_sync_state(
            _Api(),
            device_id=10,
            proxmox_last_updated=None,
            proxmox_endpoint_raw_id=501,
            overwrite_custom_fields=False,
        )


@pytest.mark.asyncio
async def test_device_sidecar_writer_staggered_cas_claim_authorizes_one_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner: int | None = None
    initial_reads = 0
    both_read = asyncio.Event()
    first_authorized = asyncio.Event()

    async def _first(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal initial_reads
        snapshot = owner
        if initial_reads < 2:
            initial_reads += 1
            if initial_reads == 2:
                both_read.set()
            await both_read.wait()
            return {"id": 55, "device": {"id": 10}, "proxmox_endpoint_raw_id": snapshot}
        return {"id": 55, "device": {"id": 10}, "proxmox_endpoint_raw_id": owner}

    async def _patch(
        _nb: object,
        _path: str,
        _record_id: int,
        payload: dict[str, object],
    ) -> dict[str, object]:
        nonlocal owner
        requested = int(payload["proxmox_endpoint_raw_id"])
        if requested == 502:
            await first_authorized.wait()
        if owner is not None and owner != requested:
            raise ProxboxException(
                message="NetBox REST request failed",
                detail={"code": "endpoint_ownership_conflict"},
                http_status_code=409,
            )
        owner = requested
        return {"id": 55, "device": {"id": 10}, **payload}

    async def _claim(endpoint_id: int) -> dict[str, object] | None:
        result = await writer.write_device_sync_state(
            object(),
            device_id=10,
            proxmox_last_updated=None,
            proxmox_endpoint_raw_id=endpoint_id,
            overwrite_custom_fields=False,
        )
        if endpoint_id == 501:
            first_authorized.set()
        return result

    monkeypatch.setattr(writer, "rest_first_async", _first)
    monkeypatch.setattr(writer, "rest_patch_async", _patch)
    monkeypatch.setattr(writer, "clear_rest_get_cache_for_path", lambda *_args: None)

    outcomes = await asyncio.gather(_claim(501), _claim(502), return_exceptions=True)

    assert isinstance(outcomes[0], dict)
    assert isinstance(outcomes[1], writer.DeviceSyncStateOwnershipConflict)
    assert owner == 501


@pytest.mark.asyncio
async def test_device_sidecar_writer_verifies_claim_against_legacy_plugin_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner: int | None = None
    initial_reads = 0
    patch_count = 0
    both_read = asyncio.Event()
    both_patched = asyncio.Event()

    async def _first(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal initial_reads
        snapshot = owner
        if initial_reads < 2:
            initial_reads += 1
            if initial_reads == 2:
                both_read.set()
            await both_read.wait()
            return {"id": 55, "device": {"id": 10}, "proxmox_endpoint_raw_id": snapshot}
        return {"id": 55, "device": {"id": 10}, "proxmox_endpoint_raw_id": owner}

    async def _patch(
        _nb: object,
        _path: str,
        _record_id: int,
        payload: dict[str, object],
    ) -> dict[str, object]:
        nonlocal owner, patch_count
        owner = int(payload["proxmox_endpoint_raw_id"])
        patch_count += 1
        if patch_count == 2:
            both_patched.set()
        await both_patched.wait()
        return {"id": 55, "device": {"id": 10}, **payload}

    monkeypatch.setattr(writer, "rest_first_async", _first)
    monkeypatch.setattr(writer, "rest_patch_async", _patch)
    monkeypatch.setattr(writer, "clear_rest_get_cache_for_path", lambda *_args: None)

    outcomes = await asyncio.gather(
        *(
            writer.write_device_sync_state(
                object(),
                device_id=10,
                proxmox_last_updated=None,
                proxmox_endpoint_raw_id=endpoint_id,
                overwrite_custom_fields=False,
            )
            for endpoint_id in (501, 502)
        ),
        return_exceptions=True,
    )

    assert patch_count == 2
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert (
        sum(isinstance(outcome, writer.DeviceSyncStateOwnershipConflict) for outcome in outcomes)
        == 1
    )
    assert owner == (501 if isinstance(outcomes[0], dict) else 502)


@pytest.mark.asyncio
async def test_device_sidecar_writer_stamps_owner_when_reflection_overwrite_is_disabled(
    recorder: _Recorder,
) -> None:
    result = await writer.write_device_sync_state(
        object(),
        device_id=10,
        proxmox_last_updated="2026-07-20T00:00:00+00:00",
        proxmox_node_name="pve01",
        proxmox_cluster_name="cluster-a",
        proxmox_endpoint_raw_id=501,
        overwrite_custom_fields=False,
    )

    assert result is not None
    post_call = next(call for call in reversed(recorder.calls) if call["method"] == "POST")
    assert post_call["payload"] == {
        "device": {"id": 10},
        "proxmox_endpoint_raw_id": 501,
    }


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
async def test_create_or_update_vm_accepts_typed_resource_and_writes_live_sidecar_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def _fake_detect_version(_nb: object) -> tuple[int, int, int]:
        return (4, 5, 0)

    async def _fake_reconcile(*_args, **_kwargs):
        return {"id": 123, "name": "vm-101"}

    async def _fake_cloudinit(*_args, **_kwargs) -> None:
        return None

    async def _fake_sync_state_resolver(*_args, **_kwargs):
        return None

    async def _fake_sidecar(_nb: object, **kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_create.detect_netbox_version", _fake_detect_version
    )
    monkeypatch.setattr("proxbox_api.services.sync.vm_create.rest_reconcile_async", _fake_reconcile)
    monkeypatch.setattr("proxbox_api.services.sync.vm_create.sync_vm_cloudinit", _fake_cloudinit)
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_create.resolve_virtual_machine_by_sync_state",
        _fake_sync_state_resolver,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_create.write_virtual_machine_sync_state",
        _fake_sidecar,
    )

    await create_or_update_virtual_machine(
        netbox_session=object(),
        proxmox_resource=ProxmoxVmResourceInput.model_validate(
            {
                "vmid": 101,
                "name": "vm-101",
                "node": "pve-a",
                "type": "qemu",
                "status": "running",
                "maxcpu": 2,
                "maxmem": 2_147_483_648,
                "maxdisk": 10_737_418_240,
            }
        ),
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


@pytest.fixture
def proxbox_caplog(caplog: pytest.LogCaptureFixture):
    """Capture the non-propagating application logger."""

    import logging

    proxbox_logger = logging.getLogger("proxbox")
    proxbox_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="proxbox"):
            yield caplog
    finally:
        proxbox_logger.removeHandler(caplog.handler)


_COMPLETE_IDENTITY = {
    "proxmox_vm_id": 101,
    "proxmox_vm_type": "qemu",
    "proxmox_cluster": "cluster-a",
    "proxmox_endpoint_id": 44,
}


def _identity_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if "incomplete Proxmox ownership identity" in record.getMessage()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("custom_fields", "missing"),
    [
        ({**_COMPLETE_IDENTITY, "proxmox_endpoint_id": None}, "proxmox_endpoint_id"),
        ({**_COMPLETE_IDENTITY, "proxmox_cluster": ""}, "proxmox_cluster"),
        ({k: v for k, v in _COMPLETE_IDENTITY.items() if k != "proxmox_vm_id"}, "proxmox_vm_id"),
        ({**_COMPLETE_IDENTITY, "proxmox_vm_type": "unknown"}, "proxmox_vm_type"),
    ],
    ids=("endpoint-dropped", "cluster-blank", "vmid-absent", "type-unknown"),
)
@pytest.mark.parametrize("overwrite", [False, True])
async def test_vm_sidecar_writer_warns_when_identity_is_incomplete(
    recorder: _Recorder,
    proxbox_caplog: pytest.LogCaptureFixture,
    custom_fields: dict[str, object],
    missing: str,
    overwrite: bool,
) -> None:
    await writer.write_virtual_machine_sync_state(
        object(),
        virtual_machine_id=321,
        custom_fields=custom_fields,
        overwrite_custom_fields=overwrite,
        proxmox_vm_name="vm-321",
    )

    warnings = _identity_warnings(proxbox_caplog)
    assert len(warnings) == 1
    assert "virtual_machine_id=321" in warnings[0]
    assert missing in warnings[0]
    assert f"overwrite_custom_fields={overwrite}" in warnings[0]


@pytest.mark.asyncio
async def test_vm_sidecar_writer_incomplete_identity_warning_changes_nothing_persisted(
    recorder: _Recorder,
    proxbox_caplog: pytest.LogCaptureFixture,
) -> None:
    """The warning only reports: overwrite off still writes just name/role evidence."""

    await writer.write_virtual_machine_sync_state(
        object(),
        virtual_machine_id=321,
        custom_fields={**_COMPLETE_IDENTITY, "proxmox_endpoint_id": None},
        overwrite_custom_fields=False,
        proxmox_vm_name="vm-321",
    )

    assert _identity_warnings(proxbox_caplog)
    post = recorder.calls[-1]
    assert post["method"] == "POST"
    assert post["payload"] == {"virtual_machine": {"id": 321}, "proxmox_vm_name": "vm-321"}

    recorder.calls.clear()
    await writer.write_virtual_machine_sync_state(
        object(),
        virtual_machine_id=322,
        custom_fields={**_COMPLETE_IDENTITY, "proxmox_endpoint_id": None},
        overwrite_custom_fields=True,
    )
    payload = recorder.calls[-1]["payload"]
    # A null endpoint id is still dropped rather than written, exactly as before.
    assert "proxmox_endpoint_raw_id" not in payload
    assert payload["proxmox_cluster_name"] == "cluster-a"


@pytest.mark.asyncio
async def test_vm_sidecar_writer_is_quiet_for_complete_identity_or_no_custom_fields(
    recorder: _Recorder,
    proxbox_caplog: pytest.LogCaptureFixture,
) -> None:
    await writer.write_virtual_machine_sync_state(
        object(),
        virtual_machine_id=1,
        custom_fields=dict(_COMPLETE_IDENTITY),
        overwrite_custom_fields=True,
    )
    await writer.write_virtual_machine_sync_state(
        object(),
        virtual_machine_id=2,
        custom_fields=None,
        overwrite_custom_fields=False,
        proxmox_vm_name="vm-2",
    )

    assert _identity_warnings(proxbox_caplog) == []


@pytest.mark.asyncio
async def test_write_vm_endpoint_raw_id_patches_only_the_endpoint_id(
    recorder: _Recorder,
) -> None:
    recorder.existing = RestRecord(
        object(),
        "/api/plugins/proxbox/sync-state/virtual-machines/",
        {"id": 55, "virtual_machine": {"id": 960}, "proxmox_endpoint_raw_id": 1},
    )

    result = await writer.write_vm_endpoint_raw_id(object(), virtual_machine_id=960, endpoint_id=2)

    assert result is not None
    patches = [call for call in recorder.calls if call["method"] == "PATCH"]
    assert [(p["record_id"], p["payload"]) for p in patches] == [
        (55, {"proxmox_endpoint_raw_id": 2})
    ]


@pytest.mark.asyncio
async def test_write_vm_endpoint_raw_id_returns_none_when_the_write_fails(
    recorder: _Recorder,
) -> None:
    recorder.existing = RestRecord(
        object(),
        "/api/plugins/proxbox/sync-state/virtual-machines/",
        {"id": 55, "virtual_machine": {"id": 960}},
    )
    recorder.patch_error = RuntimeError("boom")

    assert (
        await writer.write_vm_endpoint_raw_id(object(), virtual_machine_id=960, endpoint_id=2)
        is None
    )
