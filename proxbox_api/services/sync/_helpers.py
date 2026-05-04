"""Shared helpers for sync services.

These small coercion utilities were duplicated across backup_routines.py,
snapshots.py, task_history.py, and replications.py. Centralized here so all
sync services share a single source of truth.
"""

from __future__ import annotations


def _extract_fk_id(value: object) -> object:
    """Return the integer ID from a nested FK dict, or the value itself."""
    if isinstance(value, dict):
        return value.get("id")
    return value


def _extract_choice_value(value: object) -> object:
    """Return the raw choice string from a nested choice dict, or the value itself."""
    if isinstance(value, dict):
        return value.get("value")
    return value


def _normalize_text(value: object) -> str | None:
    """Normalize text value, handling None, dict, and string types."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("name") or value.get("slug") or value.get("id")
    text = str(value).strip()
    return text or None
