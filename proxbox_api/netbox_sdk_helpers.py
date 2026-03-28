"""Helper utilities for interacting with netbox-sdk facade objects."""

from __future__ import annotations

from typing import Any


def to_dict(value: Any) -> dict[str, Any]:
    """Convert netbox-sdk records or plain objects into dictionaries."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "serialize"):
        try:
            data = value.serialize()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    if hasattr(value, "dict"):
        try:
            data = value.dict()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


async def ensure_record(endpoint: Any, lookup: dict[str, Any], payload: dict[str, Any]) -> Any:
    """Get a record by lookup fields or create it when missing."""
    record = await endpoint.get(**lookup)
    if record:
        return record
    try:
        return await endpoint.create(payload)
    except Exception:
        # Retry read in case another concurrent request created the same record.
        return await endpoint.get(**lookup)


async def ensure_tag(nb: Any, *, name: str, slug: str, color: str, description: str) -> Any:
    """Get or create a NetBox tag."""
    tag = await nb.extras.tags.get(slug=slug)
    if tag:
        return tag
    return await nb.extras.tags.create(
        {
            "name": name,
            "slug": slug,
            "color": color,
            "description": description,
        }
    )
