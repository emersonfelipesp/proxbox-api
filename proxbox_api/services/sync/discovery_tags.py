"""Helpers for the first-discovery audit tags (issue #362).

These tags are stamped onto Proxbox-discovered objects **only when the
reconciler takes the create branch**. The update branch must never re-stamp
them, so an operator removing a discovery tag from a NetBox object via the
UI is a permanent decision. The companion bootstrap (``netbox_bootstrap``)
guarantees the four slugs exist on a fresh NetBox install.

Slug inventory (see ``proxbox_api.constants``):

- ``proxbox-discovered-qemu`` / ``proxbox-discovered-lxc`` — VMs
- ``proxbox-discovered-cluster`` — Clusters
- ``proxbox-discovered-node`` — Proxmox node Devices
"""

from __future__ import annotations

from proxbox_api.constants import (
    DISCOVERY_TAG_VM_LXC,
    DISCOVERY_TAG_VM_QEMU,
)
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_first_async


def discovery_tag_ref(slug: str) -> dict[str, object]:
    """Build a NetBox tag reference dict identified by slug.

    NetBox's REST API accepts tags as ``[{"slug": "..."}]`` for create and
    update payloads. Passing the slug-only form keeps the diff layer (which
    sorts by slug) stable without needing to resolve a numeric ID first.
    """
    return {"slug": slug}


def vm_discovery_tag_slug(vm_type: str) -> str:
    """Return the discovery tag slug for the given Proxmox VM type."""
    return DISCOVERY_TAG_VM_LXC if str(vm_type).lower() == "lxc" else DISCOVERY_TAG_VM_QEMU


async def resolve_discovery_tag_id(nb: object, slug: str) -> int | None:
    """Look up the NetBox tag ID for a discovery slug. Returns ``None`` if missing.

    A missing tag means bootstrap has not run (or operator deleted the tag).
    Callers must treat ``None`` as "skip stamping" rather than erroring — the
    audit trail is a soft contract, never a sync blocker.
    """
    try:
        record = await rest_first_async(
            nb,
            "/api/extras/tags/",
            query={"slug": slug},
        )
    except Exception as exc:  # noqa: BLE001 — never fail the sync over a tag lookup
        logger.debug("discovery tag lookup failed for slug=%s: %s", slug, exc)
        return None
    if record is None:
        return None
    tag_id = getattr(record, "id", None)
    try:
        return int(tag_id) if tag_id is not None else None
    except (TypeError, ValueError):
        return None


def _ref_key(item: object) -> str | None:
    """Extract the comparable slug from a tag ref-shaped value."""
    if isinstance(item, dict):
        slug = item.get("slug") or item.get("name")
        return str(slug).strip().lower() if slug else None
    if hasattr(item, "serialize"):
        try:
            serialized = item.serialize()
        except Exception:  # noqa: BLE001
            return None
        if isinstance(serialized, dict):
            slug = serialized.get("slug") or serialized.get("name")
            return str(slug).strip().lower() if slug else None
    text = str(item or "").strip().lower()
    return text or None


def merge_tag_refs(
    base: list[dict[str, object]],
    existing: object,
) -> list[dict[str, object]]:
    """Union ``base`` tag refs with the tag refs already on a NetBox record.

    Used on the update branch of every reconciler that owns ``tags`` so that
    operator-added tags AND previously-stamped discovery tags survive a
    sync. Comparison is by slug (case-insensitive); ``base`` takes precedence
    when both sides have the same slug.
    """
    merged: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    for ref in base:
        key = _ref_key(ref)
        if key is None:
            continue
        merged.append(ref)
        seen_keys.add(key)
    if not isinstance(existing, list):
        return merged
    for item in existing:
        key = _ref_key(item)
        if key is None or key in seen_keys:
            continue
        if isinstance(item, dict):
            merged.append(
                {
                    k: item.get(k)
                    for k in ("id", "name", "slug", "color", "description")
                    if item.get(k) is not None
                }
            )
        elif hasattr(item, "serialize"):
            try:
                serialized = item.serialize()
            except Exception:  # noqa: BLE001
                continue
            if isinstance(serialized, dict):
                merged.append(
                    {
                        k: serialized.get(k)
                        for k in ("id", "name", "slug", "color", "description")
                        if serialized.get(k) is not None
                    }
                )
        else:
            merged.append({"slug": str(item).strip(), "name": str(item).strip()})
        seen_keys.add(key)
    return merged
