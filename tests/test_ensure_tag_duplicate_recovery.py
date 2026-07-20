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
from proxbox_api.netbox_rest import (
    _is_duplicate_error,
    _normalize_tag_color,
    ensure_tag_async,
)

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


@pytest.mark.parametrize(
    "detail",
    [
        {"name": ["tag with this name already exists."]},
        {"slug": ["tag with this slug already exists."]},
        "duplicate key value violates unique constraint",
        "Key (slug)=(proxbox) already exists.",
        {"detail": "The fields slug must make a unique set."},
    ],
)
def test_is_duplicate_error_matches_netbox_46_variants(detail: object) -> None:
    """NetBox 4.6 / Postgres uniqueness phrasings are still recognized."""
    assert _is_duplicate_error(detail) is True


@pytest.mark.parametrize(
    "detail",
    [
        "Request timeout",
        {"error": "Bad Gateway"},
        "permission denied",
        # "already in use" is deliberately NOT treated as a duplicate: it is a
        # genuine conflict (e.g. an IP/assigned object in use) that must surface,
        # not be swallowed as a duplicate-success by the shared reconcile helper.
        {"address": ["Duplicate IP address found; this address is already in use."]},
        "assigned object already in use",
    ],
)
def test_is_duplicate_error_ignores_non_duplicate(detail: object) -> None:
    assert _is_duplicate_error(detail) is False


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("ff5722", "ff5722"),
        ("#FF5722", "ff5722"),
        ("  #4CAF50 ", "4caf50"),
        ("f52", "ff5522"),  # 3-digit shorthand expands to 6
    ],
)
def test_normalize_tag_color(raw: str, expected: str) -> None:
    assert _normalize_tag_color(raw) == expected


@pytest.mark.asyncio
async def test_ensure_tag_normalizes_color_before_post() -> None:
    """A '#'-prefixed / uppercase color is normalized before it reaches NetBox."""
    nb = object()
    with patch(
        "proxbox_api.netbox_rest.rest_reconcile_async",
        new_callable=AsyncMock,
        return_value=_FAKE_TAG,
    ) as mock_reconcile:
        await ensure_tag_async(nb, name=_NAME, slug=_SLUG, color="#4CAF50", description=_DESC)

    payload = mock_reconcile.await_args.kwargs["payload"]
    assert payload["color"] == "4caf50"
