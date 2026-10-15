"""Regression tests for full-update fetch-concurrency plumbing.

History: an earlier rev of `create_storages` used `fetch_max_concurrency`. The
parameter was renamed to `fetch_concurrency`, but `app/full_update.py` continued
to pass `fetch_max_concurrency=...` in two call sites — passing a non-default
value would raise `TypeError`. The full-update wrappers now translate
`fetch_max_concurrency` (their own kwarg, kept for plugin/SSE compat) into
`fetch_concurrency` when calling `create_storages`. This test pins that
contract: the public function still names the parameter `fetch_concurrency`,
and both full-update stage modes pass that exact keyword.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from proxbox_api.services.sync.storages import create_storages
from proxbox_api.services.sync.virtual_disks import create_virtual_disks


def test_create_storages_signature_uses_fetch_concurrency() -> None:
    sig = inspect.signature(create_storages)
    assert "fetch_concurrency" in sig.parameters
    assert "fetch_max_concurrency" not in sig.parameters


def test_full_update_calls_create_storages_with_fetch_concurrency() -> None:
    """Both storage stage descriptors pass the renamed keyword."""
    full_update_path = (
        Path(__file__).resolve().parent.parent / "proxbox_api" / "app" / "full_update.py"
    )
    source = full_update_path.read_text(encoding="utf-8")

    storage_descriptor = source.split('"storage": _StageDescriptor(', 1)[1].split(
        '"virtual-machines": _StageDescriptor(', 1
    )[0]
    assert storage_descriptor.count('"fetch_concurrency": fetch_concurrency') == 2
    assert "fetch_max_concurrency" not in storage_descriptor


@pytest.mark.asyncio
async def test_create_storages_accepts_fetch_concurrency_without_typeerror() -> None:
    """Smoke check: passing `fetch_concurrency=4` does not raise TypeError.

    `create_storages` takes the early `pxs is empty` path and returns immediately
    without calling NetBox or Proxmox, so this only validates kwarg compatibility.
    """
    result = await create_storages(
        netbox_session=object(),
        pxs=None,
        tag=object(),
        fetch_concurrency=4,
    )
    assert result == []


def test_create_virtual_disks_signature_uses_fetch_max_concurrency() -> None:
    sig = inspect.signature(create_virtual_disks)
    assert "fetch_max_concurrency" in sig.parameters


def test_full_update_calls_create_virtual_disks_with_fetch_max_concurrency() -> None:
    full_update_path = (
        Path(__file__).resolve().parent.parent / "proxbox_api" / "app" / "full_update.py"
    )
    source = full_update_path.read_text(encoding="utf-8")

    disk_descriptor = source.split('"virtual-disks": _StageDescriptor(', 1)[1].split(
        '"task-history": _StageDescriptor(', 1
    )[0]
    assert disk_descriptor.count('"fetch_max_concurrency": inputs.fetch_max_concurrency') == 2
