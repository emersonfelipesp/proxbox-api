"""Tests for Proxmox cluster ``tag-style`` color-map parsing."""

from __future__ import annotations

import pytest

from proxbox_api.services.proxmox.tag_styles import (
    fallback_color,
    fetch_tag_color_map,
    parse_tag_color_map,
)


@pytest.mark.parametrize(
    ("tag_style", "expected"),
    [
        (None, {}),
        ("", {}),
        ("   ", {}),
        ("case-sensitive=1", {}),
        # Bare color-map form
        (
            "color-map=critical:ff5722:ffffff:1;production:00aa00",
            {"critical": "ff5722", "production": "00aa00"},
        ),
        # Embedded color-map with surrounding kv pairs
        (
            "case-sensitive=1,color-map=critical:ff5722:ffffff:1",
            {"critical": "ff5722"},
        ),
        # Short hex expands to long hex
        ("color-map=foo:f00", {"foo": "ff0000"}),
        # Leading ``#`` tolerated
        ("color-map=foo:#abcdef", {"foo": "abcdef"}),
        # Case-insensitive tag names
        ("color-map=CRITICAL:ff5722", {"critical": "ff5722"}),
        # Malformed entries are dropped, valid ones survive
        ("color-map=bad;good:00ff00;:nocolor;name:zzzzzz", {"good": "00ff00"}),
        # First win on duplicate tag
        (
            "color-map=foo:111111;foo:222222",
            {"foo": "111111"},
        ),
        # Empty color-map value
        ("color-map=", {}),
    ],
)
def test_parse_tag_color_map(tag_style: object, expected: dict[str, str]) -> None:
    assert parse_tag_color_map(tag_style) == expected


def test_parse_tag_color_map_rejects_non_string() -> None:
    assert parse_tag_color_map(123) == {}  # type: ignore[arg-type]
    assert parse_tag_color_map(["color-map=foo:f00"]) == {}  # type: ignore[arg-type]


def test_fallback_color_is_deterministic_six_char_hex() -> None:
    color = fallback_color("critical")
    assert color == fallback_color("critical")
    assert len(color) == 6
    assert all(c in "0123456789abcdef" for c in color)


def test_fallback_color_differs_per_tag() -> None:
    assert fallback_color("critical") != fallback_color("production")


class _StubCluster:
    def __init__(self, options: object) -> None:
        self._options = options

    @property
    def options(self) -> "_StubCluster":
        return self

    def get(self) -> object:
        return self._options


class _StubProxmox:
    def __init__(self, options: object) -> None:
        self.cluster = _StubCluster(options)


class _StubSession:
    def __init__(self, options: object) -> None:
        self.session = _StubProxmox(options)


@pytest.mark.asyncio
async def test_fetch_tag_color_map_returns_parsed_dict() -> None:
    sess = _StubSession({"tag-style": "color-map=critical:ff5722;production:00aa00"})
    assert await fetch_tag_color_map(sess) == {
        "critical": "ff5722",
        "production": "00aa00",
    }


@pytest.mark.asyncio
async def test_fetch_tag_color_map_handles_missing_tag_style() -> None:
    sess = _StubSession({})
    assert await fetch_tag_color_map(sess) == {}


@pytest.mark.asyncio
async def test_fetch_tag_color_map_handles_non_dict_options() -> None:
    sess = _StubSession("not a dict")
    assert await fetch_tag_color_map(sess) == {}


@pytest.mark.asyncio
async def test_fetch_tag_color_map_handles_session_error() -> None:
    class _BrokenSession:
        @property
        def session(self) -> object:
            raise RuntimeError("upstream offline")

    assert await fetch_tag_color_map(_BrokenSession()) == {}
