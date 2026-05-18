"""Tests for ensure_tag_async duplicate-error recovery (issue #121).

When multiple concurrent VM syncs share a Proxmox tag, one worker creates the
tag and a second worker's POST receives "tag with this name already exists".
The reconciler may still fail to locate the record (timing / slug mismatch).
ensure_tag_async must catch that case, do two direct lookups, and return the
existing record rather than propagating a noisy traceback.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from proxbox_api.exception import ProxboxException
from proxbox_api.netbox_rest import ensure_tag_async

_SLUG = "production"
_NAME = "production"
_COLOR = "4caf50"
_DESC = "Synced by Proxbox"

_FAKE_TAG: dict[str, Any] = {
    "id": 42,
    "name": _NAME,
    "slug": _SLUG,
    "color": _COLOR,
    "description": _DESC,
}

_DUPLICATE_EXC = ProxboxException(
    "NetBox returned an error",
    detail={"name": ["tag with this name already exists."]},
)


@pytest.mark.asyncio
async def test_ensure_tag_recovers_via_slug_lookup() -> None:
    """Reconciler raises duplicate error; slug lookup finds the record."""
    nb = object()
    with (
        patch(
            "proxbox_api.netbox_rest.rest_reconcile_async",
            new_callable=AsyncMock,
            side_effect=_DUPLICATE_EXC,
        ),
        patch(
            "proxbox_api.netbox_rest.rest_first_async",
            new_callable=AsyncMock,
            return_value=_FAKE_TAG,
        ) as mock_first,
    ):
        result = await ensure_tag_async(nb, name=_NAME, slug=_SLUG, color=_COLOR, description=_DESC)

    assert result == _FAKE_TAG
    mock_first.assert_awaited_once_with(nb, "/api/extras/tags/", query={"slug": _SLUG})


@pytest.mark.asyncio
async def test_ensure_tag_recovers_via_name_lookup_when_slug_misses() -> None:
    """Slug lookup misses; name lookup finds the record."""
    nb = object()
    call_count = 0

    async def _first(nb_: object, path: str, *, query: dict[str, object]) -> dict[str, Any] | None:
        nonlocal call_count
        call_count += 1
        if query.get("slug"):
            return None
        return _FAKE_TAG

    with (
        patch(
            "proxbox_api.netbox_rest.rest_reconcile_async",
            new_callable=AsyncMock,
            side_effect=_DUPLICATE_EXC,
        ),
        patch("proxbox_api.netbox_rest.rest_first_async", side_effect=_first),
    ):
        result = await ensure_tag_async(nb, name=_NAME, slug=_SLUG, color=_COLOR, description=_DESC)

    assert result == _FAKE_TAG
    assert call_count == 2


@pytest.mark.asyncio
async def test_ensure_tag_reraises_when_fallback_lookups_both_miss() -> None:
    """Both fallback lookups return None → original exception re-raised."""
    nb = object()
    with (
        patch(
            "proxbox_api.netbox_rest.rest_reconcile_async",
            new_callable=AsyncMock,
            side_effect=_DUPLICATE_EXC,
        ),
        patch(
            "proxbox_api.netbox_rest.rest_first_async",
            new_callable=AsyncMock,
            return_value=None,
        ),
    ):
        with pytest.raises(ProxboxException) as exc_info:
            await ensure_tag_async(nb, name=_NAME, slug=_SLUG, color=_COLOR, description=_DESC)

    assert exc_info.value is _DUPLICATE_EXC


@pytest.mark.asyncio
async def test_ensure_tag_does_not_swallow_non_duplicate_exception() -> None:
    """A ProxboxException that is NOT a duplicate error propagates immediately."""
    nb = object()
    non_dup_exc = ProxboxException("timeout talking to NetBox", detail="Request timeout")

    with (
        patch(
            "proxbox_api.netbox_rest.rest_reconcile_async",
            new_callable=AsyncMock,
            side_effect=non_dup_exc,
        ),
        patch(
            "proxbox_api.netbox_rest.rest_first_async",
            new_callable=AsyncMock,
        ) as mock_first,
    ):
        with pytest.raises(ProxboxException) as exc_info:
            await ensure_tag_async(nb, name=_NAME, slug=_SLUG, color=_COLOR, description=_DESC)

    assert exc_info.value is non_dup_exc
    mock_first.assert_not_awaited()
