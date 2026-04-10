"""Helper utilities for interacting with netbox-sdk facade objects."""

from __future__ import annotations

from typing import TypeVar

from proxbox_api.logger import logger
from proxbox_api.types import NetBoxRecord, TagLike

T = TypeVar("T", bound=NetBoxRecord)


def to_dict(value: object) -> dict[str, object]:
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
        except Exception as error:
            logger.debug("Failed to serialize NetBox SDK value to dict: %s", error)
    if hasattr(value, "dict"):
        try:
            data = value.dict()
            if isinstance(data, dict):
                return data
        except Exception as error:
            logger.debug("Failed to call dict() on NetBox SDK value: %s", error)
    return {}


def _is_duplicate_error(detail: object) -> bool:
    if isinstance(detail, dict):
        return any(_is_duplicate_error(value) for value in detail.values())
    if isinstance(detail, list):
        return any(_is_duplicate_error(value) for value in detail)
    text = str(detail).lower()
    return "already exists" in text or "must be unique" in text


def _candidate_reuse_lookups(
    lookup: dict[str, object],
    payload: dict[str, object],
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    seen: set[tuple[tuple[str, object], ...]] = set()

    def _add(candidate: dict[str, object]) -> None:
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

    if payload.get("virtual_machine") not in (None, "") and payload.get("volume_id") not in (
        None,
        "",
    ):
        _add(
            {
                "virtual_machine_id": payload["virtual_machine"],
                "volume_id": payload["volume_id"],
            }
        )

    return candidates


async def ensure_record(
    endpoint: object, lookup: dict[str, object], payload: dict[str, object]
) -> NetBoxRecord:
    """Get a record by lookup fields or create it when missing.

    Args:
        endpoint: NetBox API endpoint object for the resource type.
        lookup: Dictionary of lookup fields for finding existing records.
        payload: Dictionary of fields for creating new records if not found.

    Returns:
        The NetBox record (existing or newly created).

    Raises:
        Exception: If the record cannot be found or created.
    """
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


async def ensure_tag(nb: object, *, name: str, slug: str, color: str, description: str) -> TagLike:
    """Get or create a NetBox tag.

    Args:
        nb: NetBox API facade object.
        name: Tag name.
        slug: Tag slug (URL-safe identifier).
        color: Tag color (hex code).
        description: Tag description.

    Returns:
        The NetBox tag (existing or newly created).
    """
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
