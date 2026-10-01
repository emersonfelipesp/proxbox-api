"""Tests for Proxbox-managed VM orphan sweeping."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from proxbox_api import netbox_rest
from proxbox_api.constants import DISCOVERY_TAG_VM_LXC, DISCOVERY_TAG_VM_QEMU
from proxbox_api.exception import ProxboxException
from proxbox_api.schemas.stream_messages import ItemOperation
from proxbox_api.services.sync import orphan_sweep, sync_state_reader
from proxbox_api.services.sync.orphan_sweep import (
    clear_soft_delete_marker,
    extract_touched_vm_ids,
    find_orphan_vms,
    run_orphan_vm_sweep,
    soft_delete_orphan_vms,
)
from proxbox_api.services.sync.sync_state_reader import SidecarVMOrphanScan

_FRESH_TAGS: dict[int, list[object]] = {}
_FRESH_OVERRIDE: dict[int, object] = {}


def _vm(
    record_id: int,
    name: str,
    *,
    run_id: str | None = "old-run",
    tag_slug: str = DISCOVERY_TAG_VM_QEMU,
) -> dict[str, object]:
    _FRESH_TAGS[record_id] = [{"slug": tag_slug}]
    return {
        "id": record_id,
        "name": name,
        "display_url": f"/virtualization/virtual-machines/{record_id}/",
        "_proxbox_last_run_id": run_id,
        "_proxmox_vm_id": record_id + 1000,
        "tags": [{"slug": tag_slug}],
    }


@pytest.fixture(autouse=True)
def _stale_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    """By default the pre-PATCH sidecar re-read still shows the discovery-time stale run."""

    async def _resolve(_nb: object, _vm_id: int) -> dict[str, object] | None:
        return {"last_run_id": "old-run"}

    async def _fresh(_nb: object, record_id: int) -> dict[str, object] | None:
        if record_id in _FRESH_OVERRIDE:
            override = _FRESH_OVERRIDE[record_id]
            return override if isinstance(override, dict) else None
        return {"id": record_id, "tags": list(_FRESH_TAGS.get(record_id, []))}

    _FRESH_OVERRIDE.clear()
    monkeypatch.setattr(orphan_sweep, "resolve_vm_sidecar_by_parent_id", _resolve)
    monkeypatch.setattr(orphan_sweep, "_fresh_vm_record", _fresh)


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

    async def _unexpected_delete(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        raise AssertionError("transient sidecar scan failure must not patch")

    monkeypatch.setattr(orphan_sweep, "scan_vm_sidecar_orphan_candidates", _fake_sidecar_scan)
    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _unexpected_delete)

    result = await run_orphan_vm_sweep(object(), run_id="current-run", enabled=True)

    assert result["candidates"] == 0
    assert result["deleted"] == 0


@pytest.mark.asyncio
async def test_soft_delete_orphan_vms_patches_candidates_and_emits_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched: list[tuple[int, dict[str, object]]] = []

    async def _fake_patch(
        _nb: object, path: str, record_id: int, payload: dict[str, object]
    ) -> dict[str, object]:
        assert path == orphan_sweep.VIRTUAL_MACHINES_PATH
        patched.append((record_id, payload))
        return payload

    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _fake_patch)
    bridge = _Bridge()

    result = await soft_delete_orphan_vms(
        object(),
        [_vm(1, "stale-a"), _vm(2, "stale-b", tag_slug=DISCOVERY_TAG_VM_LXC)],
        run_id="current-run",
        stream=bridge,
        soft_delete_tag_id=77,
    )

    assert patched == [
        (1, {"status": "decommissioning", "tags": [{"slug": DISCOVERY_TAG_VM_QEMU}, {"id": 77}]}),
        (2, {"status": "decommissioning", "tags": [{"slug": DISCOVERY_TAG_VM_LXC}, {"id": 77}]}),
    ]
    assert result == {
        "run_id": "current-run",
        "dry_run": False,
        "candidates": 2,
        "deleted": 0,
        "soft_deleted": 2,
        "failed": 0,
        "skipped": 0,
    }
    assert [event["operation"] for event in bridge.item_progress] == [
        ItemOperation.UPDATED,
        ItemOperation.UPDATED,
    ]
    assert bridge.phase_summary[-1]["updated"] == 2


@pytest.mark.asyncio
async def test_clear_soft_delete_marker_preserves_other_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patches: list[tuple[int, dict[str, object]]] = []

    async def _fake_patch(
        _nb: object, path: str, record_id: int, payload: dict[str, object]
    ) -> dict[str, object]:
        assert path == orphan_sweep.VIRTUAL_MACHINES_PATH
        patches.append((record_id, payload))
        return payload

    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _fake_patch)

    record = _vm(5, "re-adopted", tag_slug=DISCOVERY_TAG_VM_QEMU)
    record["tags"] = [
        {"id": 10, "slug": DISCOVERY_TAG_VM_QEMU},
        {"id": 11, "slug": "customer-owned"},
        {"id": 12, "slug": "proxbox-soft-deleted"},
    ]

    await clear_soft_delete_marker(object(), record)

    assert patches == [
        (
            5,
            {
                "tags": [
                    {"id": 10},
                    {"id": 11},
                ]
            },
        )
    ]


@pytest.mark.asyncio
async def test_run_orphan_vm_sweep_ensures_marker_before_patching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched: list[tuple[int, dict[str, object]]] = []

    async def _fake_find(_nb: object, _run_id: str, **_kwargs: Any) -> list[dict[str, object]]:
        return [_vm(7, "stale")]

    async def _fake_tag(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        return {"id": 77}

    async def _fake_patch(
        _nb: object, _path: str, record_id: int, payload: dict[str, object]
    ) -> dict[str, object]:
        patched.append((record_id, payload))
        return payload

    monkeypatch.setattr(orphan_sweep, "find_orphan_vms", _fake_find)
    monkeypatch.setattr(netbox_rest, "ensure_tag_async", _fake_tag)
    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _fake_patch)

    result = await run_orphan_vm_sweep(object(), run_id="current-run", enabled=True)

    assert result["soft_deleted"] == 1
    assert patched == [
        (
            7,
            {
                "status": "decommissioning",
                "tags": [{"slug": DISCOVERY_TAG_VM_QEMU}, {"id": 77}],
            },
        )
    ]


@pytest.mark.asyncio
async def test_soft_delete_orphan_vms_dry_run_emits_would_delete_without_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected_patch(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        raise AssertionError("dry-run must not patch")

    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _unexpected_patch)
    bridge = _Bridge()

    result = await soft_delete_orphan_vms(
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
async def test_soft_delete_orphan_vms_skips_not_found_patch_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_patch(
        _nb: object, _path: str, _record_id: int, _payload: dict[str, object]
    ) -> dict[str, object]:
        raise ProxboxException(message="NetBox REST request failed", detail="404 not found")

    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _fake_patch)
    bridge = _Bridge()

    result = await soft_delete_orphan_vms(
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
async def test_soft_delete_orphan_vms_raises_on_patch_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_patch(
        _nb: object, _path: str, _record_id: int, _payload: dict[str, object]
    ) -> dict[str, object]:
        raise RuntimeError("permission denied")

    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _fake_patch)
    bridge = _Bridge()

    with pytest.raises(ProxboxException, match="Error while sweeping orphan"):
        await soft_delete_orphan_vms(
            object(),
            [_vm(1, "blocked")],
            run_id="current-run",
            stream=bridge,
        )

    assert bridge.item_progress[0]["operation"] == ItemOperation.FAILED
    assert bridge.phase_summary[-1]["failed"] == 1


@pytest.mark.asyncio
async def test_soft_delete_orphan_vms_aborts_when_candidate_was_touched_this_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected_patch(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        raise AssertionError("stamp invariant failure must abort before patching")

    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _unexpected_patch)
    bridge = _Bridge()

    with pytest.raises(ProxboxException, match="invariant failed"):
        await soft_delete_orphan_vms(
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
    async def _fake_find(_nb: object, _run_id: str, **_kwargs: Any):
        return [_vm(1, "preview")]

    async def _unexpected_delete(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("dry-run must not delete")

    monkeypatch.setattr(orphan_sweep, "find_orphan_vms", _fake_find)
    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _unexpected_delete)

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


def _install_scan(
    monkeypatch: pytest.MonkeyPatch,
    scan: SidecarVMOrphanScan,
    scan_calls: list[dict[str, Any]] | None = None,
) -> None:
    async def _fake_scan(*_args: Any, **kwargs: Any) -> SidecarVMOrphanScan:
        if scan_calls is not None:
            scan_calls.append(kwargs)
        return scan

    monkeypatch.setattr(orphan_sweep, "scan_vm_sidecar_orphan_candidates", _fake_scan)


def _forbid_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _unexpected_patch(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        raise AssertionError("a skipped sweep must not PATCH")

    async def _unexpected_tag(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        raise AssertionError("a skipped sweep must not create the marker tag")

    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _unexpected_patch)
    monkeypatch.setattr(netbox_rest, "ensure_tag_async", _unexpected_tag)


@pytest.mark.asyncio
async def test_find_orphan_vms_forwards_endpoint_scope_to_sidecar_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan_calls: list[dict[str, Any]] = []
    _install_scan(
        monkeypatch,
        SidecarVMOrphanScan(stale_candidates=[_vm(3, "scoped")], current_vm_ids=set()),
        scan_calls,
    )

    assert await find_orphan_vms(object(), "current-run", endpoint_ids={2, 5}) == [_vm(3, "scoped")]
    await find_orphan_vms(object(), "current-run")

    assert scan_calls[0]["endpoint_ids"] == {2, 5}
    assert scan_calls[1]["endpoint_ids"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scan", "vm_stage_failed", "expected_reason"),
    [
        (
            SidecarVMOrphanScan(stale_candidates=[_vm(1, "stale")], current_vm_ids=set()),
            True,
            "vm_stage_failed",
        ),
        (
            SidecarVMOrphanScan(
                stale_candidates=[], current_vm_ids=set(), sidecar_unavailable=True
            ),
            False,
            "sidecar_unavailable",
        ),
        (
            SidecarVMOrphanScan(
                stale_candidates=[], current_vm_ids=set(), sidecar_read_failed=True
            ),
            False,
            "sidecar_read_failed",
        ),
    ],
)
@pytest.mark.parametrize("dry_run", [False, True])
async def test_run_orphan_vm_sweep_skips_with_reason_and_never_writes(
    monkeypatch: pytest.MonkeyPatch,
    scan: SidecarVMOrphanScan,
    vm_stage_failed: bool,
    expected_reason: str,
    dry_run: bool,
) -> None:
    _install_scan(monkeypatch, scan)
    _forbid_writes(monkeypatch)
    bridge = _Bridge()

    result = await run_orphan_vm_sweep(
        object(),
        run_id="current-run",
        enabled=True,
        dry_run=dry_run,
        stream=bridge,
        vm_stage_failed=vm_stage_failed,
    )

    assert result["skipped_reason"] == expected_reason
    assert result["enabled"] is True
    assert result["dry_run"] is dry_run
    assert (result["candidates"], result["soft_deleted"], result["failed"]) == (0, 0, 0)
    assert bridge.item_progress == []
    assert expected_reason in str(bridge.phase_summary[0]["message"])


@pytest.mark.asyncio
async def test_run_orphan_vm_sweep_vm_stage_failure_does_not_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan_calls: list[dict[str, Any]] = []
    _install_scan(
        monkeypatch,
        SidecarVMOrphanScan(stale_candidates=[], current_vm_ids=set(), run_seen_in_scope=True),
        scan_calls,
    )

    result = await run_orphan_vm_sweep(
        object(), run_id="current-run", enabled=True, vm_stage_failed=True
    )

    assert result["skipped_reason"] == "vm_stage_failed"
    assert scan_calls == []


@pytest.mark.asyncio
async def test_run_orphan_vm_sweep_reports_no_skip_reason_when_it_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan_calls: list[dict[str, Any]] = []
    _install_scan(
        monkeypatch,
        SidecarVMOrphanScan(stale_candidates=[], current_vm_ids=set(), run_seen_in_scope=True),
        scan_calls,
    )

    result = await run_orphan_vm_sweep(
        object(), run_id="current-run", enabled=True, endpoint_ids=frozenset({4})
    )
    disabled = await run_orphan_vm_sweep(object(), run_id="current-run", enabled=False)

    assert result["skipped_reason"] is None
    assert result["candidates"] == 0
    assert scan_calls[0]["endpoint_ids"] == frozenset({4})
    assert disabled["enabled"] is False
    assert disabled["skipped_reason"] == "disabled"


def test_is_delete_orphans_enabled_honours_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "true")
    assert orphan_sweep.is_delete_orphans_enabled() is True
    monkeypatch.setenv("PROXBOX_DELETE_ORPHANS", "false")
    assert orphan_sweep.is_delete_orphans_enabled() is False


@pytest.mark.parametrize(
    ("plugin_settings", "expected"),
    [
        ({"delete_orphans": True}, True),
        ({"delete_orphans": False}, False),
        ({}, False),
        (None, False),
    ],
)
def test_is_delete_orphans_enabled_falls_back_to_plugin_setting_then_off(
    monkeypatch: pytest.MonkeyPatch,
    plugin_settings: dict[str, object] | None,
    expected: bool,
) -> None:
    from proxbox_api import runtime_settings

    monkeypatch.delenv("PROXBOX_DELETE_ORPHANS", raising=False)
    monkeypatch.setattr(runtime_settings, "_load_settings", lambda: plugin_settings)

    assert orphan_sweep.is_delete_orphans_enabled() is expected


class _AttrTag:
    slug = orphan_sweep.SOFT_DELETE_TAG_SLUG


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({"status": {"value": "decommissioning", "label": "Decommissioning"}}, True),
        ({"status": {"value": "Decommissioning "}}, True),
        ({"status": "decommissioning"}, True),
        ({"status": {"value": "active"}, "tags": []}, False),
        ({"status": "active"}, False),
        ({"status": None}, False),
        ({"status": {"value": None}}, False),
        ({"tags": [{"slug": orphan_sweep.SOFT_DELETE_TAG_SLUG}]}, True),
        ({"tags": [{"name": orphan_sweep.SOFT_DELETE_TAG_SLUG}]}, True),
        ({"tags": [orphan_sweep.SOFT_DELETE_TAG_SLUG]}, True),
        ({"tags": [_AttrTag()]}, True),
        ({"status": {"value": "active"}, "tags": [{"slug": DISCOVERY_TAG_VM_QEMU}]}, False),
        ({"tags": [DISCOVERY_TAG_VM_QEMU]}, False),
        ({"tags": None}, False),
        ({"tags": "proxbox-soft-deleted"}, False),
        ({}, False),
        (None, False),
        (42, False),
    ],
)
def test_is_soft_deleted_vm_shape_matrix(record: object, expected: bool) -> None:
    assert orphan_sweep.is_soft_deleted_vm(record) is expected


def test_is_soft_deleted_vm_accepts_sdk_style_records() -> None:
    class _Record:
        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def serialize(self) -> dict[str, object]:
            return self._payload

    assert orphan_sweep.is_soft_deleted_vm(_Record({"status": {"value": "decommissioning"}}))
    assert not orphan_sweep.is_soft_deleted_vm(_Record({"status": {"value": "active"}}))


def test_exclude_soft_deleted_vms_keeps_live_records_and_logs_count(
    proxbox_log_capture: Any,
) -> None:
    live = {"id": 1, "status": {"value": "active"}}
    tagged = {"id": 2, "tags": [{"slug": orphan_sweep.SOFT_DELETE_TAG_SLUG}]}
    decommissioned = {"id": 3, "status": "decommissioning"}

    kept = orphan_sweep.exclude_soft_deleted_vms([live, tagged, decommissioned], stage="demo")
    untouched = orphan_sweep.exclude_soft_deleted_vms([live], stage="demo")

    assert kept == [live]
    assert untouched == [live]
    assert proxbox_log_capture.messages(logging.INFO) == [
        "Skipping 2 decommissioned or soft-deleted VM(s) during demo sync"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_run_orphan_vm_sweep_skips_when_run_was_never_stamped_in_scope(
    monkeypatch: pytest.MonkeyPatch, dry_run: bool
) -> None:
    _install_scan(
        monkeypatch,
        SidecarVMOrphanScan(stale_candidates=[_vm(1, "stale")], current_vm_ids=set()),
    )
    _forbid_writes(monkeypatch)

    result = await run_orphan_vm_sweep(
        object(), run_id="bogus-run", enabled=True, dry_run=dry_run, endpoint_ids=frozenset({4})
    )

    assert result["skipped_reason"] == "run_not_found"
    assert result["soft_deleted"] == 0


@pytest.mark.asyncio
async def test_scan_requires_run_id_stamp_inside_endpoint_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _sidecar(vm_id: int, endpoint: int, run: str) -> dict[str, object]:
        return {
            "virtual_machine": vm_id,
            "proxmox_endpoint_raw_id": endpoint,
            "last_run_id": run,
            "proxmox_vm_id": vm_id,
        }

    async def _scan(*_args: Any, **_kwargs: Any) -> tuple[list[object], bool]:
        return [_sidecar(1, 9, "run-x"), _sidecar(2, 4, "old")], False

    async def _vm_fetch(_nb: object, vm_id: int) -> object:
        return _vm(vm_id, f"vm-{vm_id}")

    monkeypatch.setattr(sync_state_reader, "_scan_sidecars", _scan)
    monkeypatch.setattr(sync_state_reader, "_fetch_vm_by_id", _vm_fetch)

    other_endpoint = await sync_state_reader.scan_vm_sidecar_orphan_candidates(
        object(), run_id="run-x", vm_slugs=[DISCOVERY_TAG_VM_QEMU], endpoint_ids={4}
    )
    unscoped = await sync_state_reader.scan_vm_sidecar_orphan_candidates(
        object(), run_id="run-x", vm_slugs=[DISCOVERY_TAG_VM_QEMU]
    )

    assert other_endpoint is not None and other_endpoint.run_seen_in_scope is False
    assert unscoped is not None and unscoped.run_seen_in_scope is True


@pytest.mark.asyncio
async def test_soft_delete_skips_vm_restamped_between_discovery_and_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _restamped(_nb: object, vm_id: int) -> dict[str, object] | None:
        return {"last_run_id": "current-run" if vm_id == 1 else "old-run"}

    patched: list[int] = []

    async def _fake_patch(
        _nb: object, _path: str, record_id: int, _payload: dict[str, object]
    ) -> dict[str, object]:
        patched.append(record_id)
        return {}

    monkeypatch.setattr(orphan_sweep, "resolve_vm_sidecar_by_parent_id", _restamped)
    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _fake_patch)
    bridge = _Bridge()

    result = await soft_delete_orphan_vms(
        object(),
        [_vm(1, "raced"), _vm(2, "still-stale")],
        run_id="current-run",
        stream=bridge,
        soft_delete_tag_id=77,
    )

    assert patched == [2]
    assert result["soft_deleted"] == 1
    assert result["skipped"] == 1
    assert bridge.item_progress[0]["operation"] == ItemOperation.SKIPPED
    assert "restamped" in bridge.item_progress[0]["warning"]


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved", [None, {"last_run_id": "another-run"}])
async def test_soft_delete_skips_when_sidecar_unverifiable_or_changed(
    monkeypatch: pytest.MonkeyPatch, resolved: dict[str, object] | None
) -> None:
    async def _resolve(_nb: object, _vm_id: int) -> dict[str, object] | None:
        return resolved

    async def _unexpected_patch(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        raise AssertionError("an unverifiable candidate must not be PATCHed")

    monkeypatch.setattr(orphan_sweep, "resolve_vm_sidecar_by_parent_id", _resolve)
    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _unexpected_patch)

    result = await soft_delete_orphan_vms(
        object(), [_vm(1, "x")], run_id="current-run", soft_delete_tag_id=77
    )

    assert result["skipped"] == 1 and result["soft_deleted"] == 0


def _identified_vm(record_id: int, *, cluster: str | None = "Lab", vm_type: str | None = "qemu"):
    vm = _vm(record_id, f"vm-{record_id}")
    vm["_proxmox_cluster_name"] = cluster
    vm["_proxmox_vm_type"] = vm_type
    return vm


def test_build_live_vm_keys_ignores_non_guests_and_malformed_entries() -> None:
    keys = orphan_sweep.build_live_vm_keys(
        [
            {
                "Lab": [
                    {"vmid": "101", "type": "qemu"},
                    {"type": "node"},
                    {"vmid": 5, "type": "sdn"},
                ]
            },
            {"Other": [{"vmid": 7, "type": "lxc"}, "junk"]},
            "junk",
        ]
    )

    assert keys == frozenset({("lab", 101, "qemu"), ("other", 7, "lxc")})
    assert orphan_sweep.build_live_vm_keys(None) == frozenset()


@pytest.mark.asyncio
async def test_soft_delete_skips_candidates_still_present_in_live_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched: list[int] = []
    warnings: list[str] = []
    monkeypatch.setattr(
        orphan_sweep.logger, "warning", lambda message, *args: warnings.append(message % args)
    )

    async def _fake_patch(_nb: object, _path: str, record_id: int, payload: object) -> object:
        patched.append(record_id)
        return payload

    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _fake_patch)
    bridge = _Bridge()
    present, absent = _identified_vm(1), _identified_vm(2)
    # vmid is record_id + 1000; only VM 1 is still live in Proxmox.
    live = frozenset({("lab", 1001, "qemu")})

    result = await soft_delete_orphan_vms(
        object(),
        [present, absent],
        run_id="current-run",
        stream=bridge,
        soft_delete_tag_id=77,
        live_vm_keys=live,
    )

    assert patched == [2]
    assert result["soft_deleted"] == 1
    assert result["skipped"] == 1
    assert any("vm-1" in text and "still_present_in_proxmox" in text for text in warnings)
    assert bridge.item_progress[0]["warning"] == "still_present_in_proxmox"


@pytest.mark.asyncio
async def test_soft_delete_skips_candidates_without_enough_identity_when_live_keys_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _unexpected(*_a: Any, **_k: Any) -> object:
        raise AssertionError("an unidentifiable candidate must not be patched")

    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _unexpected)
    bridge = _Bridge()

    result = await soft_delete_orphan_vms(
        object(),
        [_identified_vm(1, cluster=None), _identified_vm(2, vm_type="unknown")],
        run_id="current-run",
        stream=bridge,
        live_vm_keys=frozenset(),
    )

    assert result["soft_deleted"] == 0
    assert result["skipped"] == 2
    assert {event["warning"] for event in bridge.item_progress} == {"identity_incomplete"}


@pytest.mark.asyncio
async def test_run_orphan_vm_sweep_skips_when_live_inventory_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan_calls: list[dict[str, Any]] = []
    _install_scan(
        monkeypatch,
        SidecarVMOrphanScan(stale_candidates=[], current_vm_ids=set(), run_seen_in_scope=True),
        scan_calls,
    )
    _forbid_writes(monkeypatch)

    result = await run_orphan_vm_sweep(
        object(), run_id="current-run", enabled=True, live_inventory_unavailable=True
    )

    assert result["skipped_reason"] == "live_inventory_unavailable"
    assert scan_calls == []


@pytest.mark.asyncio
async def test_scan_exposes_guest_identity_on_stale_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _scan(*_args: Any, **_kwargs: Any) -> tuple[list[object], bool]:
        return [
            {
                "virtual_machine": 2,
                "proxmox_endpoint_raw_id": 4,
                "last_run_id": "old",
                "proxmox_vm_id": 1002,
                "proxmox_vm_type": "lxc",
                "proxmox_cluster_name": "Lab",
            }
        ], False

    async def _vm_fetch(_nb: object, vm_id: int) -> object:
        return _vm(vm_id, f"vm-{vm_id}")

    monkeypatch.setattr(sync_state_reader, "_scan_sidecars", _scan)
    monkeypatch.setattr(sync_state_reader, "_fetch_vm_by_id", _vm_fetch)

    scan = await sync_state_reader.scan_vm_sidecar_orphan_candidates(
        object(), run_id="run-x", vm_slugs=[DISCOVERY_TAG_VM_QEMU]
    )

    assert scan is not None
    candidate = scan.stale_candidates[0]
    assert candidate["_proxmox_vm_id"] == 1002
    assert candidate["_proxmox_vm_type"] == "lxc"
    assert candidate["_proxmox_cluster_name"] == "Lab"


@pytest.mark.parametrize(
    "rows",
    [
        [{"vmid": 5}],
        [{"type": "qemu"}],
        [{"id": "qemu/abc", "type": "qemu"}],
        [{"id": "lxc/", "vmid": "x"}],
    ],
)
def test_build_live_vm_keys_rejects_unidentifiable_guest_rows(rows: list[dict]) -> None:
    if rows == [{"vmid": 5}]:
        # No type and no guest-shaped id: not a guest row, ignored like storage/node.
        assert orphan_sweep.build_live_vm_keys([{"Lab": rows}]) == frozenset()
        return
    with pytest.raises(orphan_sweep.LiveInventoryError):
        orphan_sweep.build_live_vm_keys([{"Lab": rows}])


def test_build_live_vm_keys_derives_missing_fields_from_id() -> None:
    keys = orphan_sweep.build_live_vm_keys(
        [
            {
                "Lab": [
                    {"id": "qemu/123", "type": "qemu"},
                    {"id": "lxc/7"},
                    {"id": "qemu/9", "type": "qemu", "vmid": 9},
                    {"id": "storage/local", "type": "storage"},
                ]
            }
        ]
    )

    assert keys == frozenset({("lab", 123, "qemu"), ("lab", 7, "lxc"), ("lab", 9, "qemu")})


@pytest.mark.asyncio
async def test_soft_delete_preserves_tags_added_after_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patches: list[dict[str, object]] = []

    async def _fake_patch(_nb: object, _path: str, _id: int, payload: dict[str, object]) -> object:
        patches.append(payload)
        return payload

    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _fake_patch)
    candidate = _vm(1, "tagged")
    _FRESH_OVERRIDE[1] = {
        "id": 1,
        "tags": [{"id": 5, "slug": DISCOVERY_TAG_VM_QEMU}, {"id": 6, "slug": "added-later"}],
    }

    await soft_delete_orphan_vms(object(), [candidate], run_id="current-run", soft_delete_tag_id=77)

    assert patches[0]["tags"] == [{"id": 5}, {"id": 6}, {"id": 77}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fresh", "reason"),
    [
        (None, "vm_unreadable"),
        ({"id": 1, "tags": [{"slug": "proxbox-soft-deleted"}]}, "already_swept"),
    ],
)
async def test_soft_delete_skips_when_fresh_vm_unreadable_or_already_swept(
    monkeypatch: pytest.MonkeyPatch, fresh: object, reason: str
) -> None:
    async def _unexpected(*_a: Any, **_k: Any) -> object:
        raise AssertionError("must not PATCH")

    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _unexpected)
    candidate = _vm(1, "x")
    _FRESH_OVERRIDE[1] = fresh
    bridge = _Bridge()

    result = await soft_delete_orphan_vms(
        object(), [candidate], run_id="current-run", stream=bridge, soft_delete_tag_id=77
    )

    assert result["skipped"] == 1 and result["soft_deleted"] == 0
    assert bridge.item_progress[0]["warning"] == reason
