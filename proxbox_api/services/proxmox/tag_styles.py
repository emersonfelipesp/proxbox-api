"""Proxmox cluster tag-style helpers.

Reads ``/cluster/options::tag-style`` from a Proxmox session and parses its
``color-map`` into ``{tag_name_lower: "rrggbb"}`` so sync code can mirror
Proxmox tag colors into NetBox tags.

Proxmox color-map format (per ``man pvesh`` / cluster.cfg docs):

    color-map=<tag>:<bg>[:<fg>][:<weight>];<tag>:<bg>[:<fg>][:<weight>];...

Color tokens are 3- or 6-char hex without ``#``. The full ``tag-style`` field
may be just the color-map, or include other style flags before/after it
separated by ``,`` (e.g. ``case-sensitive=1,color-map=...``). Never raises into
the sync flow — returns ``{}`` on any failure.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async

_HEX_RE = re.compile(r"^[0-9a-f]{6}$")
_SHORT_HEX_RE = re.compile(r"^[0-9a-f]{3}$")


def _normalize_hex(value: str) -> str | None:
    token = value.strip().lower().lstrip("#")
    if _HEX_RE.fullmatch(token):
        return token
    if _SHORT_HEX_RE.fullmatch(token):
        return "".join(ch * 2 for ch in token)
    return None


def _extract_color_map_text(tag_style: str) -> str | None:
    """Pull the ``color-map=...`` value out of a Proxmox ``tag-style`` string.

    Accepts both the bare ``color-map=...`` form and the longer
    ``case-sensitive=1,color-map=...,...`` shape.
    """
    text = tag_style.strip()
    if not text:
        return None
    # The color-map sub-field uses ``;`` to separate per-tag entries, but its
    # siblings inside tag-style use ``,``. Split on the outer ``,`` and grab the
    # sub-field whose key is color-map.
    for chunk in text.split(","):
        key, sep, value = chunk.partition("=")
        if not sep:
            continue
        if key.strip().lower() == "color-map":
            return value.strip()
    return None


def parse_tag_color_map(tag_style: str | None) -> dict[str, str]:
    """Parse a Proxmox ``tag-style`` field into ``{tag_lower: "rrggbb"}``.

    Returns an empty dict on any malformed input. Background color is used
    (Proxmox color-map foreground/weight are ignored for NetBox tag color).
    """
    if not tag_style or not isinstance(tag_style, str):
        return {}
    color_map_text = _extract_color_map_text(tag_style)
    if color_map_text is None:
        return {}
    result: dict[str, str] = {}
    for entry in color_map_text.split(";"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) < 2:
            continue
        name = parts[0].strip().lower()
        if not name:
            continue
        color = _normalize_hex(parts[1])
        if color is None:
            continue
        result.setdefault(name, color)
    return result


async def fetch_tag_color_map(proxmox_session: Any) -> dict[str, str]:
    """Fetch and parse the cluster ``tag-style`` color-map.

    Returns ``{}`` on any error so callers can fall through to deterministic
    color fallback without breaking the sync flow.
    """
    try:
        options = await resolve_async(proxmox_session.session.cluster.options.get())
    except Exception:
        logger.debug("Failed to fetch /cluster/options for tag-style", exc_info=True)
        return {}

    if not isinstance(options, dict):
        return {}
    tag_style = options.get("tag-style") or options.get("tag_style")
    if not tag_style:
        return {}
    if not isinstance(tag_style, str):
        return {}
    return parse_tag_color_map(tag_style)


def fallback_color(tag_name: str) -> str:
    """Deterministic 6-char hex color derived from the tag name.

    Stable across bulk and individual sync paths so re-syncs don't flip
    colors for tags that aren't in the Proxmox color-map.
    """
    digest = hashlib.md5(tag_name.encode("utf-8")).hexdigest()
    return digest[:6]
