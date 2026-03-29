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


def _is_duplicate_error(detail: Any) -> bool:
    if isinstance(detail, dict):
        return any(_is_duplicate_error(value) for value in detail.values())
    if isinstance(detail, list):
        return any(_is_duplicate_error(value) for value in detail)
    text = str(detail).lower()
    return "already exists" in text or "must be unique" in text


def _candidate_reuse_lookups(
    lookup: dict[str, Any],
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, Any], ...]] = set()

    def _add(candidate: dict[str, Any]) -> None:
        normalized = {key: value for key, value in candidate.items() if value not in (None, "")}
        if not normalized:
            return
        key = tuple(sorted(normalized.items()))
        if key in seen:
            return
        seen.add(key)
        candidates.append(normalized)

    _add(lookup)

    for field in ("slug", "name", "model"):
        value = payload.get(field)
        if value not in (None, ""):
            _add({field: value})

    if payload.get("name") not in (None, "") and payload.get("site") not in (None, ""):
        _add({"name": payload["name"], "site_id": payload["site"]})

    if payload.get("manufacturer") not in (None, "") and payload.get("model") not in (None, ""):
        _add({"manufacturer_id": payload["manufacturer"], "model": payload["model"]})

    if payload.get("virtual_machine") not in (None, "") and payload.get("volume_id") not in (None, ""):
        _add(
            {
                "virtual_machine_id": payload["virtual_machine"],
                "volume_id": payload["volume_id"],
            }
        )

    return candidates


async def ensure_record(endpoint: Any, lookup: dict[str, Any], payload: dict[str, Any]) -> Any:
    """Get a record by lookup fields or create it when missing."""
    for candidate in _candidate_reuse_lookups(lookup, payload):
        record = await endpoint.get(**candidate)
        if record:
            return record
    try:
        return await endpoint.create(payload)
    except Exception as error:
        if _is_duplicate_error(str(error)):
            for candidate in _candidate_reuse_lookups(lookup, payload):
                record = await endpoint.get(**candidate)
                if record:
                    return record
        # Retry read in case another concurrent request created the same record.
        record = await endpoint.get(**lookup)
        if record:
            return record
        raise error


async def ensure_tag(nb: Any, *, name: str, slug: str, color: str, description: str) -> Any:
    """Get or create a NetBox tag."""
    return await ensure_record(
        nb.extras.tags,
        {"slug": slug},
        {
            "name": name,
            "slug": slug,
            "color": color,
            "description": description,
        },
    )
