"""Helper utilities for interacting with netbox-sdk facade objects."""

from __future__ import annotations

from proxbox_api.logger import logger


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
) -> object:
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


async def ensure_tag(nb: object, *, name: str, slug: str, color: str, description: str) -> object:
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
