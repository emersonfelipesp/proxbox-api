"""Tests for Proxbox-managed VM orphan sweeping."""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api.constants import DISCOVERY_TAG_VM_LXC, DISCOVERY_TAG_VM_QEMU
from proxbox_api.exception import ProxboxException
from proxbox_api.schemas.stream_messages import ItemOperation
from proxbox_api.services.sync import orphan_sweep, sync_state_reader
from proxbox_api.services.sync.orphan_sweep import (
    delete_orphan_vms,
    extract_touched_vm_ids,
    find_orphan_vms,
    run_orphan_vm_sweep,
)
from proxbox_api.services.sync.sync_state_reader import SidecarVMOrphanScan


def _vm(
    record_id: int,
    name: str,
    *,
    run_id: str | None = "old-run",
    tag_slug: str = DISCOVERY_TAG_VM_QEMU,
) -> dict[str, object]:
    return {
        "id": record_id,
        "name": name,
        "display_url": f"/virtualization/virtual-machines/{record_id}/",
        "_proxbox_last_run_id": run_id,
        "_proxmox_vm_id": record_id + 1000,
        "tags": [{"slug": tag_slug}],
    }


class _Bridge:
    def __init__(self) -> None:
        self.item_progress: list[dict[str, Any]] = []
        self.phase_summary: list[dict[str, Any]] = []
        self.error_detail: list[dict[str, Any]] = []

    async def emit_item_progress(self, **kwargs: Any) -> None:
        self.item_progress.append(kwargs)

    async def emit_phase_summary(self, **kwargs: Any) -> None:
        self.phase_summary.append(kwargs)

    async def emit_error_detail(self, **kwargs: Any) -> None:
        self.error_detail.append(kwargs)


@pytest.mark.asyncio
async def test_find_orphan_vms_returns_typed_sidecar_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_vm = _vm(8, "sidecar-stale")

    async def _fake_sidecar_scan(*_args: Any, **_kwargs: Any) -> SidecarVMOrphanScan:
        return SidecarVMOrphanScan(stale_candidates=[stale_vm], current_vm_ids=set())

    monkeypatch.setattr(orphan_sweep, "scan_vm_sidecar_orphan_candidates", _fake_sidecar_scan)

    assert await find_orphan_vms(object(), "current-run") == [stale_vm]


@pytest.mark.asyncio
async def test_find_orphan_vms_treats_sidecar_503_scan_as_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _failed_sidecar_scan(*_args: Any, **_kwargs: Any):
        raise ProxboxException(
            message="NetBox REST request failed",
            detail="HTTP 503 Service Unavailable",
            http_status_code=503,
        )

    sync_state_reader.reset_sidecar_reader_availability_cache()
    monkeypatch.setattr(sync_state_reader, "rest_list_paginated_async", _failed_sidecar_scan)

    candidates = await find_orphan_vms(object(), "current-run")

    assert candidates == []


@pytest.mark.asyncio
async def test_run_orphan_vm_sweep_does_not_delete_when_sidecar_scan_transiently_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_sidecar_scan(*_args: Any, **_kwargs: Any) -> SidecarVMOrphanScan:
        return SidecarVMOrphanScan(
            stale_candidates=[],
            current_vm_ids=set(),
            sidecar_read_failed=True,
        )

    async def _unexpected_delete(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("transient sidecar scan failure must not delete")

    monkeypatch.setattr(orphan_sweep, "scan_vm_sidecar_orphan_candidates", _fake_sidecar_scan)
    monkeypatch.setattr(orphan_sweep, "rest_bulk_delete_async", _unexpected_delete)

    result = await run_orphan_vm_sweep(object(), run_id="current-run", enabled=True)

    assert result["candidates"] == 0
    assert result["deleted"] == 0


@pytest.mark.asyncio
async def test_delete_orphan_vms_deletes_candidates_and_emits_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted_ids: list[int] = []

    async def _fake_delete(_nb: object, path: str, ids: list[int]) -> int:
        assert path == orphan_sweep.VIRTUAL_MACHINES_PATH
        deleted_ids.extend(ids)
        return len(ids)

    monkeypatch.setattr(orphan_sweep, "rest_bulk_delete_async", _fake_delete)
    bridge = _Bridge()

    result = await delete_orphan_vms(
        object(),
        [_vm(1, "stale-a"), _vm(2, "stale-b", tag_slug=DISCOVERY_TAG_VM_LXC)],
        run_id="current-run",
        stream=bridge,
    )

    assert deleted_ids == [1, 2]
    assert result == {
        "run_id": "current-run",
        "dry_run": False,
        "candidates": 2,
        "deleted": 2,
        "failed": 0,
        "skipped": 0,
    }
    assert [event["operation"] for event in bridge.item_progress] == [
        ItemOperation.DELETED,
        ItemOperation.DELETED,
    ]
    assert bridge.phase_summary[-1]["deleted"] == 2


@pytest.mark.asyncio
async def test_delete_orphan_vms_dry_run_emits_would_delete_without_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected_delete(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("dry-run must not delete")

    monkeypatch.setattr(orphan_sweep, "rest_bulk_delete_async", _unexpected_delete)
    bridge = _Bridge()

    result = await delete_orphan_vms(
        object(),
        [_vm(1, "preview-a"), _vm(2, "preview-b")],
        run_id="current-run",
        dry_run=True,
        stream=bridge,
    )

    assert result["deleted"] == 0
    assert result["skipped"] == 2
    assert [event["operation"] for event in bridge.item_progress] == [
        ItemOperation.WOULD_DELETE,
        ItemOperation.WOULD_DELETE,
    ]
    assert bridge.phase_summary[-1]["skipped"] == 2


@pytest.mark.asyncio
async def test_delete_orphan_vms_skips_not_found_delete_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_delete(_nb: object, _path: str, _ids: list[int]) -> int:
        raise ProxboxException(message="NetBox REST request failed", detail="404 not found")

    monkeypatch.setattr(orphan_sweep, "rest_bulk_delete_async", _fake_delete)
    bridge = _Bridge()

    result = await delete_orphan_vms(
        object(),
        [_vm(1, "already-gone")],
        run_id="current-run",
        stream=bridge,
    )

    assert result["deleted"] == 0
    assert result["failed"] == 0
    assert result["skipped"] == 1
    assert bridge.item_progress[0]["operation"] == ItemOperation.SKIPPED


@pytest.mark.asyncio
async def test_delete_orphan_vms_raises_on_hard_delete_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_delete(_nb: object, _path: str, _ids: list[int]) -> int:
        raise RuntimeError("permission denied")

    monkeypatch.setattr(orphan_sweep, "rest_bulk_delete_async", _fake_delete)
    bridge = _Bridge()

    with pytest.raises(ProxboxException, match="Error while sweeping orphan"):
        await delete_orphan_vms(
            object(),
            [_vm(1, "blocked")],
            run_id="current-run",
            stream=bridge,
        )

    assert bridge.item_progress[0]["operation"] == ItemOperation.FAILED
    assert bridge.phase_summary[-1]["failed"] == 1


@pytest.mark.asyncio
async def test_delete_orphan_vms_aborts_when_candidate_was_touched_this_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected_delete(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("stamp invariant failure must abort before deleting")

    monkeypatch.setattr(orphan_sweep, "rest_bulk_delete_async", _unexpected_delete)
    bridge = _Bridge()

    with pytest.raises(ProxboxException, match="invariant failed"):
        await delete_orphan_vms(
            object(),
            [_vm(42, "bad-candidate", run_id=None)],
            run_id="current-run",
            stream=bridge,
            touched_vm_ids={42},
        )

    assert bridge.error_detail
    assert bridge.error_detail[0]["phase"] == orphan_sweep.ORPHAN_SWEEP_PHASE


@pytest.mark.asyncio
async def test_run_orphan_vm_sweep_disabled_does_not_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected_find(*_args: Any, **_kwargs: Any) -> list[dict[str, object]]:
        raise AssertionError("disabled sweep must not query")

    monkeypatch.setattr(orphan_sweep, "find_orphan_vms", _unexpected_find)

    result = await run_orphan_vm_sweep(object(), run_id="current-run", enabled=False)

    assert result["enabled"] is False
    assert result["candidates"] == 0
    assert result["deleted"] == 0


@pytest.mark.asyncio
async def test_run_orphan_vm_sweep_dry_run_previews_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_find(_nb: object, _run_id: str):
        return [_vm(1, "preview")]

    async def _unexpected_delete(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("dry-run must not delete")

    monkeypatch.setattr(orphan_sweep, "find_orphan_vms", _fake_find)
    monkeypatch.setattr(orphan_sweep, "rest_bulk_delete_async", _unexpected_delete)

    result = await run_orphan_vm_sweep(
        object(),
        run_id="current-run",
        enabled=False,
        dry_run=True,
    )

    assert result["enabled"] is False
    assert result["dry_run"] is True
    assert result["candidates"] == 1
    assert result["deleted"] == 0


def test_extract_touched_vm_ids_handles_nested_sync_results() -> None:
    payload = [
        {"id": "10", "name": "vm-a"},
        {"virtual_machine": {"id": 11}},
        [{"netbox_object": {"id": 12}}],
    ]

    assert extract_touched_vm_ids(payload) == {10, 11, 12}
