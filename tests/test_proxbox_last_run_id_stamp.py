"""Tests for persisting successful VM runs in the typed sync-state sidecar."""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api.services.sync import vm_helpers


@pytest.mark.asyncio
async def test_stamp_writes_run_id_to_typed_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def _write(_nb: object, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        "proxbox_api.services.sync.sync_state_writer.write_vm_last_run_sync_state",
        _write,
    )

    await vm_helpers.stamp_vm_last_run_id(
        nb=object(),
        vm_record={"id": "42", "name": "vm-42"},
        run_id="run-uuid-1",
    )

    assert calls == [{"virtual_machine_id": 42, "run_id": "run-uuid-1"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("vm_record", "run_id"),
    [
        ({"id": 1}, None),
        ({"id": 1}, ""),
        ({"name": "missing-id"}, "run-uuid"),
        ({"id": "invalid"}, "run-uuid"),
        (None, "run-uuid"),
    ],
)
async def test_stamp_skips_incomplete_values(
    monkeypatch: pytest.MonkeyPatch,
    vm_record: object,
    run_id: str | None,
) -> None:
    async def _unexpected_write(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("incomplete stamp must not write a sync-state sidecar")

    monkeypatch.setattr(
        "proxbox_api.services.sync.sync_state_writer.write_vm_last_run_sync_state",
        _unexpected_write,
    )

    await vm_helpers.stamp_vm_last_run_id(object(), vm_record, run_id)


@pytest.mark.asyncio
async def test_stamp_coerces_record_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class _Record:
        def dict(self) -> dict[str, object]:
            return {"id": 7}

    async def _write(_nb: object, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(
        "proxbox_api.services.sync.sync_state_writer.write_vm_last_run_sync_state",
        _write,
    )

    await vm_helpers.stamp_vm_last_run_id(object(), _Record(), "run-uuid-2")

    assert calls == [{"virtual_machine_id": 7, "run_id": "run-uuid-2"}]
