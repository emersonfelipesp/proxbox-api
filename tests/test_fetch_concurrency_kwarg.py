"""Regression test for the `fetch_concurrency` kwarg name on `create_storages`.

History: an earlier rev of `create_storages` used `fetch_max_concurrency`. The
parameter was renamed to `fetch_concurrency`, but `app/full_update.py` continued
to pass `fetch_max_concurrency=...` in two call sites — passing a non-default
value would raise `TypeError`. The full-update wrappers now translate
`fetch_max_concurrency` (their own kwarg, kept for plugin/SSE compat) into
`fetch_concurrency` when calling `create_storages`. This test pins that
contract: the public function still names the parameter `fetch_concurrency`,
and the full-update call sites pass that exact kwarg.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from proxbox_api.services.sync.storages import create_storages


def test_create_storages_signature_uses_fetch_concurrency() -> None:
    sig = inspect.signature(create_storages)
    assert "fetch_concurrency" in sig.parameters
    assert "fetch_max_concurrency" not in sig.parameters


def test_full_update_calls_create_storages_with_fetch_concurrency() -> None:
    """The two `create_storages(...)` call sites in full_update.py must pass
    the renamed kwarg, not the legacy `fetch_max_concurrency`.
    """
    full_update_path = (
        Path(__file__).resolve().parent.parent / "proxbox_api" / "app" / "full_update.py"
    )
    source = full_update_path.read_text(encoding="utf-8")

    # Find every `create_storages(` call and verify each block includes the
    # renamed kwarg before the next top-level statement.
    matches = list(re.finditer(r"create_storages\s*\(", source))
    assert matches, "expected at least one create_storages(...) call in full_update.py"

    for match in matches:
        # Take the next ~600 characters as the call window. Long enough to
        # cover the kwargs block; short enough not to bleed into the next call.
        window = source[match.start() : match.start() + 600]
        assert "fetch_concurrency" in window, (
            f"create_storages call near offset {match.start()} is missing the "
            "fetch_concurrency kwarg"
        )


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
