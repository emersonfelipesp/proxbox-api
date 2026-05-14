"""Tests for the Proxmox-tag → NetBox-tag resolver service."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from proxbox_api.services.proxmox.tag_styles import fallback_color
from proxbox_api.services.sync import tag_resolver as tag_resolver_module
from proxbox_api.services.sync.tag_resolver import (
    DEFAULT_TAG_DESCRIPTION,
    resolve_proxmox_tag_ids,
)


@dataclass
class _FakeTag:
    id: int
    name: str
    slug: str
    color: str
    description: str


class _TagRecorder:
    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._next_id = 100
        self._fail_for = fail_for or set()

    async def ensure(
        self,
        nb: object,
        *,
        name: str,
        slug: str,
        color: str,
        description: str,
    ) -> _FakeTag:
        self.calls.append(
            {
                "nb": nb,
                "name": name,
                "slug": slug,
                "color": color,
                "description": description,
            }
        )
        if name in self._fail_for:
            raise RuntimeError(f"simulated NetBox error for {name}")
        self._next_id += 1
        return _FakeTag(
            id=self._next_id,
            name=name,
            slug=slug,
            color=color,
            description=description,
        )


@pytest.fixture
def patch_ensure_tag(monkeypatch: pytest.MonkeyPatch) -> _TagRecorder:
    recorder = _TagRecorder()
    monkeypatch.setattr(tag_resolver_module, "ensure_tag_async", recorder.ensure)
    return recorder


@pytest.mark.asyncio
async def test_resolve_empty_returns_empty_list(patch_ensure_tag: _TagRecorder) -> None:
    assert await resolve_proxmox_tag_ids(object(), None) == []
    assert await resolve_proxmox_tag_ids(object(), "") == []
    assert await resolve_proxmox_tag_ids(object(), "   ") == []
    assert await resolve_proxmox_tag_ids(object(), 123) == []
    assert await resolve_proxmox_tag_ids(object(), []) == []
    assert patch_ensure_tag.calls == []


@pytest.mark.asyncio
async def test_resolve_preserves_order_and_dedupes(patch_ensure_tag: _TagRecorder) -> None:
    nb = object()
    ids = await resolve_proxmox_tag_ids(
        nb,
        "Critical;production;critical;End-User Impact",
    )
    # Three distinct tags, in input order
    names = [call["name"] for call in patch_ensure_tag.calls]
    assert names == ["critical", "production", "end-user impact"]
    assert len(ids) == 3
    assert ids == sorted(ids)  # _next_id increments — order preserved


@pytest.mark.asyncio
async def test_resolve_slugifies_tag_names(patch_ensure_tag: _TagRecorder) -> None:
    await resolve_proxmox_tag_ids(object(), "End-User Impact;Foo_Bar;tag!!!")
    slugs = [call["slug"] for call in patch_ensure_tag.calls]
    assert slugs == ["end-user-impact", "foo-bar", "tag"]


@pytest.mark.asyncio
async def test_resolve_color_map_hit_wins_over_fallback(
    patch_ensure_tag: _TagRecorder,
) -> None:
    color_map = {"critical": "ff5722"}
    await resolve_proxmox_tag_ids(
        object(),
        "critical;production",
        color_map=color_map,
    )
    colors = {call["name"]: call["color"] for call in patch_ensure_tag.calls}
    assert colors["critical"] == "ff5722"
    # No color-map entry → deterministic md5 fallback
    assert colors["production"] == fallback_color("production")


@pytest.mark.asyncio
async def test_resolve_uses_default_description(patch_ensure_tag: _TagRecorder) -> None:
    await resolve_proxmox_tag_ids(object(), "critical")
    assert patch_ensure_tag.calls[0]["description"] == DEFAULT_TAG_DESCRIPTION
    assert DEFAULT_TAG_DESCRIPTION == "Synced by Proxbox"


@pytest.mark.asyncio
async def test_resolve_passes_custom_description(patch_ensure_tag: _TagRecorder) -> None:
    await resolve_proxmox_tag_ids(object(), "critical", description="from-test")
    assert patch_ensure_tag.calls[0]["description"] == "from-test"


@pytest.mark.asyncio
async def test_resolve_skips_failed_tags_but_keeps_going(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _TagRecorder(fail_for={"production"})
    monkeypatch.setattr(tag_resolver_module, "ensure_tag_async", recorder.ensure)
    ids = await resolve_proxmox_tag_ids(object(), "critical;production;maintenance")
    # All three were attempted, but only two IDs returned (production failed)
    assert [c["name"] for c in recorder.calls] == [
        "critical",
        "production",
        "maintenance",
    ]
    assert len(ids) == 2


@pytest.mark.asyncio
async def test_resolve_propagates_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _cancelled_ensure(*args: object, **kwargs: object) -> object:
        raise asyncio.CancelledError()

    monkeypatch.setattr(tag_resolver_module, "ensure_tag_async", _cancelled_ensure)

    with pytest.raises(asyncio.CancelledError):
        await resolve_proxmox_tag_ids(object(), "critical")
