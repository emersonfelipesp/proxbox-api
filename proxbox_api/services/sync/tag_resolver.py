"""Resolve Proxmox VM/LXC ``tags`` strings to NetBox tag IDs.

Parses the ``;``-separated Proxmox ``tags`` field, ensures each tag exists in
NetBox (creating it via ``ensure_tag_async`` if not), and returns the resulting
NetBox tag IDs preserving input order.
"""

from __future__ import annotations

import asyncio
import re

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import ensure_tag_async
from proxbox_api.proxmox_to_netbox import parse_proxmox_tags
from proxbox_api.services.proxmox.tag_styles import fallback_color

DEFAULT_TAG_DESCRIPTION = "Synced by Proxbox"


def _slugify_tag(value: str) -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "tag"


async def resolve_proxmox_tag_ids(
    nb: object,
    raw_tags: object,
    *,
    color_map: dict[str, str] | None = None,
    description: str = DEFAULT_TAG_DESCRIPTION,
) -> list[int]:
    """Ensure NetBox tags exist for each Proxmox tag and return their IDs.

    Color preference: explicit Proxmox ``color-map`` entry > deterministic
    md5 fallback so the same tag name always resolves to the same color.
    """
    tag_names = parse_proxmox_tags(raw_tags)
    if not tag_names:
        return []

    color_lookup = color_map or {}
    out: list[int] = []
    for name in tag_names:
        color = color_lookup.get(name) or fallback_color(name)
        slug = _slugify_tag(name)
        try:
            record = await ensure_tag_async(
                nb,
                name=name,
                slug=slug,
                color=color,
                description=description,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Failed to ensure NetBox tag for Proxmox tag '%s'", name, exc_info=True)
            continue
        tag_id = getattr(record, "id", None)
        if tag_id is None and isinstance(record, dict):
            tag_id = record.get("id")
        try:
            tag_id_int = int(tag_id) if tag_id is not None else None
        except (TypeError, ValueError):
            tag_id_int = None
        if tag_id_int and tag_id_int > 0:
            out.append(tag_id_int)
    return out
