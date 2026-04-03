"""Helpers for resolving Proxbox storage relations during sync."""

from __future__ import annotations

from collections.abc import Iterable


def _normalize_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("name") or value.get("slug") or value.get("id")
    text = str(value).strip()
    return text or None


def _record_to_dict(record: object) -> dict[str, object]:
    if hasattr(record, "serialize"):
        return record.serialize()
    if isinstance(record, dict):
        return record
    # Handle Pydantic models or other objects with model_dump
    if hasattr(record, "model_dump"):
        return record.model_dump()
    # Handle objects with __dict__
    if hasattr(record, "__dict__"):
        return dict(record.__dict__)
    # Fallback: try direct conversion
    try:
        return dict(record)
    except Exception:
        return {"id": getattr(record, "id", None), "name": getattr(record, "name", None)}


def _cluster_name(value: object) -> str | None:
    if isinstance(value, dict):
        # Cluster is now a nested object with id, name, etc.
        value = value.get("name") or value.get("slug") or value.get("id")
    return _normalize_text(value)


def build_storage_index(records: Iterable[object]) -> dict[tuple[str, str], dict[str, object]]:
    """Create a lookup index keyed by (cluster, storage_name)."""
    index: dict[tuple[str, str], dict[str, object]] = {}
    for record in records:
        data = _record_to_dict(record)
        cluster = _cluster_name(data.get("cluster"))
        name = _normalize_text(data.get("name"))
        if not cluster or not name:
            continue
        index[(cluster, name)] = data
    return index


def find_storage_record(
    storage_index: dict[tuple[str, str], dict[str, object]],
    *,
    cluster_name: str | None,
    storage_name: str | None,
) -> dict[str, object] | None:
    """Return the matching storage record, preferring the cluster-specific match."""
    cluster = _normalize_text(cluster_name)
    name = _normalize_text(storage_name)
    if not name:
        return None

    if cluster:
        record = storage_index.get((cluster, name))
        if record is not None:
            return record

    candidates = [
        record
        for (stored_cluster, stored_name), record in storage_index.items()
        if stored_name == name
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def storage_name_from_volume_id(volume_id: object) -> str | None:
    """Extract the storage prefix from a Proxmox volume id."""
    text = _normalize_text(volume_id)
    if not text:
        return None
    return _normalize_text(text.split(":", 1)[0])
