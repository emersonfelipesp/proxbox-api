"""Regression tests for ``_ensure_device`` honoring ``overwrite_*`` flags.

Issue #342 (`emersonfelipesp/netbox-proxbox#342`): when a Proxmox node has been
synced to NetBox and the user later changes its ``device_type`` from the default
``Proxmox Generic Device`` to a custom one, a follow-up VM sync would revert it
back. The bulk DCIM sync path was already wired through ``patchable_fields``,
but ``_ensure_device`` (the per-VM helper that materializes parent devices)
ignored every overwrite flag and applied a raw diff via ``setattr`` + ``save``.

These tests pin the fix: with ``overwrite_device_type=False`` and an existing
device, the FK is not patched. They also lock symmetric behavior for
``overwrite_device_role`` and ``overwrite_device_tags``, plus the first-create
branch propagating ``patchable_fields`` to ``rest_reconcile_async``.
"""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.sync import device_ensure


class _FakeExistingDevice:
    """Stand-in for a NetBox record returned by ``rest_list_async``."""

    def __init__(self, current: dict[str, Any]) -> None:
        self._current = current
        self.saved = False
        self.applied: dict[str, Any] = {}

    # ``_record_has_tag`` reads ``serialize()`` first, then falls back to dict
    # access. Returning a dict-shaped payload without proxbox-tagged entries
    # lets ``_prefer_existing_device`` keep this record as the chosen match.
    def serialize(self) -> dict[str, Any]:
        return {**self._current}

    def get(self, key: str, default: Any = None) -> Any:
        return self._current.get(key, default)

    async def save(self) -> None:
        self.saved = True

    def __setattr__(self, key: str, value: Any) -> None:
        if key in {"_current", "saved", "applied"}:
            object.__setattr__(self, key, value)
            return
        self.applied[key] = value


def _existing_payload(*, device_type_id: int, role_id: int) -> dict[str, Any]:
    """Return a NetBox-shape device payload with the given FKs."""
    return {
        "name": "pve01",
        "status": "active",
        "cluster": 11,
        "device_type": device_type_id,
        "role": role_id,
        "site": 41,
        "description": "Proxmox Node pve01",
        "tags": [{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        "custom_fields": {"proxmox_last_updated": "2026-04-29T00:00:00+00:00"},
    }


@pytest.fixture
def stub_existing_device(monkeypatch: pytest.MonkeyPatch):
    """Patch ``rest_list_async`` to return one fake existing record."""
    holder: dict[str, _FakeExistingDevice] = {}

    def _install(record: _FakeExistingDevice) -> None:
        holder["record"] = record

        async def _fake_rest_list(*_args: Any, **_kwargs: Any) -> list[Any]:
            return [record]

        monkeypatch.setattr(device_ensure, "rest_list_async", _fake_rest_list)

    return _install


# ── existing-device branch ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_existing_device_default_overwrites_device_type(
    stub_existing_device,
) -> None:
    """Default flags (all True) keep historical always-overwrite behavior."""
    existing = _FakeExistingDevice(_existing_payload(device_type_id=999, role_id=10))
    stub_existing_device(existing)

    await device_ensure._ensure_device(
        nb=object(),
        device_name="pve01",
        cluster_id=11,
        device_type_id=42,  # different from existing 999
        role_id=10,
        site_id=41,
        tag_refs=[{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        overwrite_flags=SyncOverwriteFlags(),
    )

    assert existing.saved is True
    assert existing.applied.get("device_type") == 42


@pytest.mark.asyncio
async def test_existing_device_preserves_device_type_when_flag_disabled(
    stub_existing_device,
) -> None:
    """Regression for issue #342: ``overwrite_device_type=False`` blocks the patch."""
    existing = _FakeExistingDevice(_existing_payload(device_type_id=999, role_id=10))
    stub_existing_device(existing)

    await device_ensure._ensure_device(
        nb=object(),
        device_name="pve01",
        cluster_id=11,
        device_type_id=42,
        role_id=10,
        site_id=41,
        tag_refs=[{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        overwrite_device_type=False,
        overwrite_flags=SyncOverwriteFlags(overwrite_device_type=False),
    )

    assert "device_type" not in existing.applied


@pytest.mark.asyncio
async def test_existing_device_preserves_role_when_flag_disabled(
    stub_existing_device,
) -> None:
    existing = _FakeExistingDevice(_existing_payload(device_type_id=42, role_id=999))
    stub_existing_device(existing)

    await device_ensure._ensure_device(
        nb=object(),
        device_name="pve01",
        cluster_id=11,
        device_type_id=42,
        role_id=10,
        site_id=41,
        tag_refs=[{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        overwrite_device_role=False,
        overwrite_flags=SyncOverwriteFlags(overwrite_device_role=False),
    )

    assert "role" not in existing.applied


@pytest.mark.asyncio
async def test_existing_device_preserves_tags_when_flag_disabled(
    stub_existing_device,
) -> None:
    existing = _FakeExistingDevice(_existing_payload(device_type_id=42, role_id=10))
    # Existing record carries a different tag set than what the sync would push.
    existing._current["tags"] = [{"id": 8, "name": "Custom", "slug": "custom", "color": "000000"}]
    stub_existing_device(existing)

    await device_ensure._ensure_device(
        nb=object(),
        device_name="pve01",
        cluster_id=11,
        device_type_id=42,
        role_id=10,
        site_id=41,
        tag_refs=[{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        overwrite_device_tags=False,
        overwrite_flags=SyncOverwriteFlags(overwrite_device_tags=False),
    )

    assert "tags" not in existing.applied


# ── first-create branch ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_create_propagates_patchable_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no device exists, ``rest_reconcile_async`` must receive the allowlist."""
    captured: dict[str, Any] = {}

    async def _fake_rest_list(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def _fake_reconcile(*_args: Any, **kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(device_ensure, "rest_list_async", _fake_rest_list)
    monkeypatch.setattr(device_ensure, "rest_reconcile_async", _fake_reconcile)

    await device_ensure._ensure_device(
        nb=object(),
        device_name="pve02",
        cluster_id=11,
        device_type_id=42,
        role_id=10,
        site_id=41,
        tag_refs=[{"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
        overwrite_device_type=False,
        overwrite_flags=SyncOverwriteFlags(overwrite_device_type=False),
    )

    allowed = captured.get("patchable_fields")
    assert allowed is not None
    assert "device_type" not in allowed
    assert "cluster" in allowed
