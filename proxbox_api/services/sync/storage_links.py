"""Helpers for resolving Proxbox storage relations during sync."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def _normalize_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("name") or value.get("slug") or value.get("id")
    text = str(value).strip()
    return text or None


def _record_to_dict(record: Any) -> dict[str, Any]:
    if hasattr(record, "serialize"):
        return record.serialize()
    if isinstance(record, dict):
        return record
    return dict(record)


def _cluster_name(value: Any) -> str | None:
    if isinstance(value, dict):
        # Cluster is now a nested object with id, name, etc.
        value = value.get("name") or value.get("slug") or value.get("id")
    return _normalize_text(value)


def build_storage_index(records: Iterable[Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Create a lookup index keyed by (cluster, storage_name)."""
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        data = _record_to_dict(record)
        cluster = _cluster_name(data.get("cluster"))
        name = _normalize_text(data.get("name"))
        if not cluster or not name:
            continue
        index[(cluster, name)] = data
    return index


def find_storage_record(
    storage_index: dict[tuple[str, str], dict[str, Any]],
    *,
    cluster_name: str | None,
    storage_name: str | None,
) -> dict[str, Any] | None:
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


def storage_name_from_volume_id(volume_id: Any) -> str | None:
    """Extract the storage prefix from a Proxmox volume id."""
    text = _normalize_text(volume_id)
    if not text:
        return None
    return _normalize_text(text.split(":", 1)[0])
