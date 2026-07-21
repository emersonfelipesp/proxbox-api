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
from proxbox_api.services.sync.vm_helpers import LAST_RUN_ID_CUSTOM_FIELD


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
        "custom_fields": {
            LAST_RUN_ID_CUSTOM_FIELD: run_id,
            "proxmox_vm_id": record_id + 1000,
        },
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
async def test_find_orphan_vms_uses_vm_discovery_slugs_and_stamp_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Candidates are selected by VM discovery tag and stale/missing run ID."""
    calls: list[dict[str, object]] = []

    async def _fake_list(_nb: object, _path: str, *, base_query: dict[str, object], **_: Any):
        calls.append(base_query)
        if base_query.get("tag") == DISCOVERY_TAG_VM_QEMU and base_query.get(
            f"cf_{LAST_RUN_ID_CUSTOM_FIELD}__nie"
        ):
            return [_vm(1, "stale-qemu")]
        if base_query.get("tag") == DISCOVERY_TAG_VM_LXC and base_query.get(
            f"cf_{LAST_RUN_ID_CUSTOM_FIELD}__empty"
        ):
            return [_vm(2, "missing-lxc", run_id=None, tag_slug=DISCOVERY_TAG_VM_LXC)]
        if base_query.get("tag") == DISCOVERY_TAG_VM_LXC and base_query.get(
            f"cf_{LAST_RUN_ID_CUSTOM_FIELD}__nie"
        ):
            return [_vm(1, "duplicate-qemu")]
        return []

    async def _fake_sidecar_scan(*_args: Any, **_kwargs: Any) -> SidecarVMOrphanScan:
        return SidecarVMOrphanScan(
            stale_candidates=[],
            current_vm_ids=set(),
            sidecar_unavailable=True,
        )

    monkeypatch.setattr(orphan_sweep, "scan_vm_sidecar_orphan_candidates", _fake_sidecar_scan)
    monkeypatch.setattr(orphan_sweep, "rest_list_paginated_async", _fake_list)

    candidates = await find_orphan_vms(object(), "current-run")

    assert [candidate["id"] for candidate in candidates] == [1, 2]
    assert {call["tag"] for call in calls} == {
        DISCOVERY_TAG_VM_QEMU,
        DISCOVERY_TAG_VM_LXC,
    }
    assert all("proxbox-discovered-cluster" not in str(call) for call in calls)
    assert any(f"cf_{LAST_RUN_ID_CUSTOM_FIELD}__nie" in call for call in calls)
    assert any(call.get(f"cf_{LAST_RUN_ID_CUSTOM_FIELD}__empty") is True for call in calls)


@pytest.mark.asyncio
async def test_find_orphan_vms_skips_first_pass_current_sidecar_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_vm_with_stale_cf = _vm(7, "current-sidecar-stale-cf")

    async def _fake_sidecar_scan(*_args: Any, **_kwargs: Any) -> SidecarVMOrphanScan:
        return SidecarVMOrphanScan(
            stale_candidates=[],
            current_vm_ids={7},
        )

    async def _fake_legacy_list(
        _nb: object,
        _path: str,
        *,
        base_query: dict[str, object],
        **_: Any,
    ) -> list[dict[str, object]]:
        if base_query.get(f"cf_{LAST_RUN_ID_CUSTOM_FIELD}__nie") == "current-run":
            return [current_vm_with_stale_cf]
        return []

    async def _unexpected_last_run_lookup(*_args: Any, **_kwargs: Any) -> str | None:
        raise AssertionError("first-pass-current sidecar VM must be skipped before recheck")

    monkeypatch.setattr(orphan_sweep, "scan_vm_sidecar_orphan_candidates", _fake_sidecar_scan)
    monkeypatch.setattr(orphan_sweep, "rest_list_paginated_async", _fake_legacy_list)
    monkeypatch.setattr(orphan_sweep, "resolve_vm_last_run_id", _unexpected_last_run_lookup)

    candidates = await find_orphan_vms(object(), "current-run")

    assert candidates == []


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

    async def _unexpected_legacy_list(*_args: Any, **_kwargs: Any):
        raise AssertionError("HTTP 503 sidecar scan failure must not enter legacy sweep")

    sync_state_reader.reset_sidecar_reader_availability_cache()
    monkeypatch.setattr(sync_state_reader, "rest_list_paginated_async", _failed_sidecar_scan)
    monkeypatch.setattr(orphan_sweep, "rest_list_paginated_async", _unexpected_legacy_list)

    candidates = await find_orphan_vms(object(), "current-run")

    assert candidates == []


@pytest.mark.asyncio
async def test_find_orphan_vms_treats_sidecar_404_scan_as_old_plugin_legacy_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_vm = _vm(8, "legacy-stale")

    async def _missing_sidecar_route(*_args: Any, **_kwargs: Any):
        raise ProxboxException(
            message="NetBox REST request failed",
            detail="Not found.",
            http_status_code=404,
        )

    async def _fake_legacy_list(
        _nb: object,
        _path: str,
        *,
        base_query: dict[str, object],
        **_: Any,
    ) -> list[dict[str, object]]:
        if base_query.get(f"cf_{LAST_RUN_ID_CUSTOM_FIELD}__nie") == "current-run":
            return [stale_vm]
        return []

    sync_state_reader.reset_sidecar_reader_availability_cache()
    monkeypatch.setattr(sync_state_reader, "rest_list_paginated_async", _missing_sidecar_route)
    monkeypatch.setattr(orphan_sweep, "rest_list_paginated_async", _fake_legacy_list)

    candidates = await find_orphan_vms(object(), "current-run")

    assert candidates == [stale_vm]


@pytest.mark.asyncio
async def test_find_orphan_vms_skips_legacy_candidates_when_sidecar_scan_transiently_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_sidecar_scan(*_args: Any, **_kwargs: Any) -> SidecarVMOrphanScan:
        return SidecarVMOrphanScan(
            stale_candidates=[],
            current_vm_ids=set(),
            sidecar_read_failed=True,
        )

    async def _unexpected_legacy_list(*_args: Any, **_kwargs: Any):
        raise AssertionError("transient sidecar scan failure must not fall through to legacy sweep")

    monkeypatch.setattr(orphan_sweep, "scan_vm_sidecar_orphan_candidates", _fake_sidecar_scan)
    monkeypatch.setattr(orphan_sweep, "rest_list_paginated_async", _unexpected_legacy_list)

    candidates = await find_orphan_vms(object(), "current-run")

    assert candidates == []


@pytest.mark.asyncio
async def test_find_orphan_vms_uses_legacy_candidates_when_sidecar_route_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_vm = _vm(8, "legacy-stale")

    async def _fake_sidecar_scan(*_args: Any, **_kwargs: Any) -> SidecarVMOrphanScan:
        return SidecarVMOrphanScan(
            stale_candidates=[],
            current_vm_ids=set(),
            sidecar_unavailable=True,
        )

    async def _fake_legacy_list(
        _nb: object,
        _path: str,
        *,
        base_query: dict[str, object],
        **_: Any,
    ) -> list[dict[str, object]]:
        if base_query.get(f"cf_{LAST_RUN_ID_CUSTOM_FIELD}__nie") == "current-run":
            return [stale_vm]
        return []

    monkeypatch.setattr(orphan_sweep, "scan_vm_sidecar_orphan_candidates", _fake_sidecar_scan)
    monkeypatch.setattr(orphan_sweep, "rest_list_paginated_async", _fake_legacy_list)

    candidates = await find_orphan_vms(object(), "current-run")

    assert candidates == [stale_vm]


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
