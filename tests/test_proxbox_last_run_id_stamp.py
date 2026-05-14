"""Tests for the post-reconcile `proxbox_last_run_id` stamp helper.

The stamp is written by `proxbox_api.services.sync.vm_helpers.stamp_vm_last_run_id`
as a narrow PATCH that merges the new run id on top of the VM's existing
custom_fields dict. It runs irrespective of the operator's
`overwrite_vm_custom_fields` flag, because that flag only gates the main
reconciler's drift detection on `custom_fields`.
"""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api.services.sync import vm_helpers
from proxbox_api.services.sync.vm_helpers import (
    LAST_RUN_ID_CUSTOM_FIELD,
    stamp_vm_last_run_id,
)


class _PatchRecorder:
    """Async recorder that mimics `rest_patch_async`."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_on_call: Exception | None = None

    async def __call__(
        self,
        nb: object,
        path: str,
        record_id: int,
        payload: dict[str, object],
    ) -> dict[str, object]:
        self.calls.append({"nb": nb, "path": path, "record_id": record_id, "payload": payload})
        if self.raise_on_call is not None:
            raise self.raise_on_call
        merged_payload = {"id": record_id, **payload}
        return merged_payload


@pytest.fixture
def patch_recorder(monkeypatch: pytest.MonkeyPatch) -> _PatchRecorder:
    recorder = _PatchRecorder()
    monkeypatch.setattr(
        "proxbox_api.netbox_rest.rest_patch_async",
        recorder,
    )
    return recorder


@pytest.mark.asyncio
async def test_stamp_writes_run_id_on_fresh_vm(patch_recorder: _PatchRecorder) -> None:
    """A VM without `proxbox_last_run_id` receives the stamp via PATCH."""
    vm_record = {
        "id": 42,
        "name": "vm-42",
        "custom_fields": {"team": "platform"},
    }

    await stamp_vm_last_run_id(nb=object(), vm_record=vm_record, run_id="run-uuid-1")

    assert len(patch_recorder.calls) == 1
    call = patch_recorder.calls[0]
    assert call["path"] == "/api/virtualization/virtual-machines/"
    assert call["record_id"] == 42
    assert call["payload"] == {
        "custom_fields": {
            "team": "platform",
            LAST_RUN_ID_CUSTOM_FIELD: "run-uuid-1",
        }
    }


@pytest.mark.asyncio
async def test_stamp_preserves_operator_set_custom_field_keys(
    patch_recorder: _PatchRecorder,
) -> None:
    """Non-managed CF keys set by the operator survive the merge."""
    vm_record = {
        "id": 7,
        "custom_fields": {
            "team": "platform",
            "cost_center": "infra-42",
            LAST_RUN_ID_CUSTOM_FIELD: "old-uuid",
        },
    }

    await stamp_vm_last_run_id(nb=object(), vm_record=vm_record, run_id="run-uuid-2")

    assert len(patch_recorder.calls) == 1
    merged_cf = patch_recorder.calls[0]["payload"]["custom_fields"]
    assert merged_cf["team"] == "platform"
    assert merged_cf["cost_center"] == "infra-42"
    assert merged_cf[LAST_RUN_ID_CUSTOM_FIELD] == "run-uuid-2"


@pytest.mark.asyncio
async def test_stamp_is_idempotent_when_run_id_already_matches(
    patch_recorder: _PatchRecorder,
) -> None:
    """If the record already carries the same run id, no PATCH is issued."""
    vm_record = {
        "id": 5,
        "custom_fields": {LAST_RUN_ID_CUSTOM_FIELD: "run-uuid-3"},
    }

    await stamp_vm_last_run_id(nb=object(), vm_record=vm_record, run_id="run-uuid-3")

    assert patch_recorder.calls == []


@pytest.mark.asyncio
async def test_stamp_replaces_different_existing_run_id(
    patch_recorder: _PatchRecorder,
) -> None:
    """An existing but different run id is overwritten."""
    vm_record = {
        "id": 9,
        "custom_fields": {LAST_RUN_ID_CUSTOM_FIELD: "old-uuid"},
    }

    await stamp_vm_last_run_id(nb=object(), vm_record=vm_record, run_id="new-uuid")

    assert len(patch_recorder.calls) == 1
    assert (
        patch_recorder.calls[0]["payload"]["custom_fields"][LAST_RUN_ID_CUSTOM_FIELD] == "new-uuid"
    )


@pytest.mark.asyncio
async def test_stamp_works_when_record_has_no_custom_fields_key(
    patch_recorder: _PatchRecorder,
) -> None:
    """A VM record without a `custom_fields` key still gets stamped."""
    vm_record = {"id": 1, "name": "vm-1"}

    await stamp_vm_last_run_id(nb=object(), vm_record=vm_record, run_id="run-uuid-4")

    assert len(patch_recorder.calls) == 1
    assert patch_recorder.calls[0]["payload"] == {
        "custom_fields": {LAST_RUN_ID_CUSTOM_FIELD: "run-uuid-4"}
    }


@pytest.mark.asyncio
async def test_stamp_treats_non_dict_custom_fields_as_empty(
    patch_recorder: _PatchRecorder,
) -> None:
    """If `custom_fields` arrives as something other than a dict, we start fresh."""
    vm_record = {"id": 2, "custom_fields": None}

    await stamp_vm_last_run_id(nb=object(), vm_record=vm_record, run_id="run-uuid-5")

    assert len(patch_recorder.calls) == 1
    assert patch_recorder.calls[0]["payload"] == {
        "custom_fields": {LAST_RUN_ID_CUSTOM_FIELD: "run-uuid-5"}
    }


@pytest.mark.asyncio
async def test_stamp_coerces_pynetbox_style_record(
    patch_recorder: _PatchRecorder,
) -> None:
    """Records exposing `.dict()` (pynetbox-style) are coerced before stamping."""

    class _PynetboxLikeVM:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def dict(self) -> dict[str, Any]:
            return self._payload

    record = _PynetboxLikeVM({"id": 11, "custom_fields": {"team": "ops"}})

    await stamp_vm_last_run_id(nb=object(), vm_record=record, run_id="run-uuid-6")

    assert len(patch_recorder.calls) == 1
    assert patch_recorder.calls[0]["record_id"] == 11
    assert (
        patch_recorder.calls[0]["payload"]["custom_fields"][LAST_RUN_ID_CUSTOM_FIELD]
        == "run-uuid-6"
    )


@pytest.mark.asyncio
async def test_stamp_handles_string_id_via_int_coercion(
    patch_recorder: _PatchRecorder,
) -> None:
    """Numeric record ids that arrive as strings are coerced to int."""
    vm_record = {"id": "13", "custom_fields": {}}

    await stamp_vm_last_run_id(nb=object(), vm_record=vm_record, run_id="run-uuid-7")

    assert len(patch_recorder.calls) == 1
    assert patch_recorder.calls[0]["record_id"] == 13


@pytest.mark.asyncio
async def test_stamp_skips_when_run_id_is_none(patch_recorder: _PatchRecorder) -> None:
    """A missing run id is a no-op."""
    await stamp_vm_last_run_id(nb=object(), vm_record={"id": 1, "custom_fields": {}}, run_id=None)
    assert patch_recorder.calls == []


@pytest.mark.asyncio
async def test_stamp_skips_when_run_id_is_empty_string(
    patch_recorder: _PatchRecorder,
) -> None:
    """An empty run id is a no-op."""
    await stamp_vm_last_run_id(nb=object(), vm_record={"id": 1, "custom_fields": {}}, run_id="")
    assert patch_recorder.calls == []


@pytest.mark.asyncio
async def test_stamp_skips_when_record_is_none(patch_recorder: _PatchRecorder) -> None:
    """A missing record is a no-op."""
    await stamp_vm_last_run_id(nb=object(), vm_record=None, run_id="run-uuid-8")
    assert patch_recorder.calls == []


@pytest.mark.asyncio
async def test_stamp_skips_when_record_is_not_coercible(
    patch_recorder: _PatchRecorder,
) -> None:
    """A record that is neither a dict nor `.dict()`-bearing is a no-op."""
    await stamp_vm_last_run_id(nb=object(), vm_record="not-a-record", run_id="run-uuid-9")
    assert patch_recorder.calls == []


@pytest.mark.asyncio
async def test_stamp_skips_when_record_has_no_id(
    patch_recorder: _PatchRecorder,
) -> None:
    """A record with no id (or unparseable id) is a no-op."""
    await stamp_vm_last_run_id(
        nb=object(),
        vm_record={"name": "no-id-vm", "custom_fields": {}},
        run_id="run-uuid-10",
    )
    assert patch_recorder.calls == []

    await stamp_vm_last_run_id(
        nb=object(),
        vm_record={"id": "not-a-number", "custom_fields": {}},
        run_id="run-uuid-11",
    )
    assert patch_recorder.calls == []


@pytest.mark.asyncio
async def test_stamp_swallows_patch_errors(
    patch_recorder: _PatchRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing PATCH is logged but does not raise."""
    patch_recorder.raise_on_call = RuntimeError("netbox unreachable")

    warnings: list[tuple[str, tuple[Any, ...]]] = []

    def _record_warning(msg: str, *args: Any, **kwargs: Any) -> None:
        warnings.append((msg, args))

    monkeypatch.setattr(vm_helpers.logger, "warning", _record_warning)

    vm_record = {"id": 99, "name": "doomed", "custom_fields": {}}

    await stamp_vm_last_run_id(nb=object(), vm_record=vm_record, run_id="run-uuid-12")

    assert len(patch_recorder.calls) == 1
    assert any("Failed to stamp proxbox_last_run_id" in msg for msg, _ in warnings)


@pytest.mark.asyncio
async def test_stamp_skips_when_dict_call_raises(
    monkeypatch: pytest.MonkeyPatch, patch_recorder: _PatchRecorder
) -> None:
    """A `.dict()` call that throws is treated as a non-coercible record."""

    class _BrokenPynetboxVM:
        def dict(self) -> dict[str, Any]:
            raise RuntimeError("kaboom")

    await stamp_vm_last_run_id(nb=object(), vm_record=_BrokenPynetboxVM(), run_id="run-uuid-13")

    assert patch_recorder.calls == []


def test_module_exports_helpers() -> None:
    """The helper is importable from the module's public surface."""
    assert hasattr(vm_helpers, "stamp_vm_last_run_id")
    assert vm_helpers.LAST_RUN_ID_CUSTOM_FIELD == "proxbox_last_run_id"
