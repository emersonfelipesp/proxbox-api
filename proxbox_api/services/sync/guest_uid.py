"""Proxmox VE 9.3 cluster and guest UID identity helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from proxbox_api.logger import logger

GUEST_UID_PATTERN = re.compile(r"[a-z2-7]{20}")
_VM_UID_SUPPORT_BY_SESSION: dict[int, bool] = {}


def _serialized_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    for method_name in ("serialize", "model_dump", "dict"):
        method = getattr(value, method_name, None)
        if not callable(method):
            continue
        try:
            serialized = method()
        except Exception:  # noqa: BLE001 - tolerant SDK/REST compatibility
            continue
        if isinstance(serialized, Mapping):
            return serialized
    return {}


def extract_proxmox_field(value: object, *names: str) -> object | None:
    """Read typed fields, aliases, or Pydantic extras without SDK-version coupling."""

    for name in names:
        attribute_name = name.replace("-", "_")
        try:
            candidate = getattr(value, attribute_name)
        except Exception:  # noqa: BLE001 - generated models may proxy attributes
            candidate = None
        if candidate not in (None, ""):
            return candidate

    mappings: list[Mapping[str, object]] = []
    model_extra = getattr(value, "model_extra", None)
    if isinstance(model_extra, Mapping):
        mappings.append(model_extra)
    mappings.append(_serialized_mapping(value))
    for mapping in mappings:
        for name in names:
            for key in (name, name.replace("-", "_")):
                candidate = mapping.get(key)
                if candidate not in (None, ""):
                    return candidate
    return None


def normalize_guest_uid(value: object, *, context: str = "guest") -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if GUEST_UID_PATTERN.fullmatch(text):
        return text
    logger.debug("Ignoring malformed Proxmox %s uid=%r", context, text)
    return None


def extract_guest_uid(value: object) -> str | None:
    return normalize_guest_uid(extract_proxmox_field(value, "uid"))


def normalize_cluster_uid(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) <= 64:
        return text
    logger.debug("Ignoring Proxmox cluster-id longer than 64 characters")
    return None


def extract_cluster_uid(value: object) -> str | None:
    return normalize_cluster_uid(
        extract_proxmox_field(value, "cluster_uid", "cluster-id", "cluster_id")
    )


def cluster_uid_from_status_items(items: Iterable[object]) -> str | None:
    """Read cluster-id from the cluster row or a standalone local node row."""

    local_node_uid: str | None = None
    for item in items:
        item_type = str(extract_proxmox_field(item, "type") or "").strip().lower()
        uid = extract_cluster_uid(item)
        if item_type == "cluster" and uid:
            return uid
        local = extract_proxmox_field(item, "local")
        if item_type == "node" and str(local or "").strip().lower() in {"1", "true"}:
            local_node_uid = uid or local_node_uid
    return local_node_uid


async def fetch_cluster_uid(session: object) -> str | None:
    """Best-effort cluster-id read for individual sync paths."""

    from proxbox_api.services.proxmox_helpers import get_cluster_status

    try:
        return cluster_uid_from_status_items(await get_cluster_status(session))
    except Exception as error:  # noqa: BLE001 - missing PVE 9.3 identity keeps legacy behavior
        logger.debug("Could not read Proxmox cluster-id for individual sync: %s", error)
        return None


def uid_identity_key(cluster_uid: object, vm_uid: object) -> tuple[str, str] | None:
    normalized_cluster_uid = normalize_cluster_uid(cluster_uid)
    normalized_vm_uid = normalize_guest_uid(vm_uid, context="sidecar guest")
    if normalized_cluster_uid and normalized_vm_uid:
        return (normalized_cluster_uid, normalized_vm_uid)
    return None


def record_uid_identity(record: Mapping[str, object]) -> tuple[str, str] | None:
    return uid_identity_key(
        record.get("proxmox_cluster_uid"),
        record.get("proxmox_vm_uid"),
    )


def legacy_candidate_has_uid_conflict(
    record: Mapping[str, object],
    *,
    cluster_uid: object,
    vm_uid: object,
) -> bool:
    """Refuse VMID reuse only when both sides identify the same Proxmox cluster."""

    live = uid_identity_key(cluster_uid, vm_uid)
    stored = record_uid_identity(record)
    return live is not None and stored is not None and live[0] == stored[0] and live[1] != stored[1]


def uid_candidate_endpoint_conflict(stored_endpoint_id: object, live_endpoint_id: object) -> bool:
    """Return True when a stored endpoint id names a different endpoint than the live one.

    A guest UID match never crosses endpoints on its own. When the stored row
    belongs to another endpoint, the caller falls back to the legacy identity
    chain, whose stale-endpoint adoption only re-binds rows whose endpoint id is
    not configured any more. A blank stored endpoint id is not a conflict.
    """

    def _positive(value: object) -> int | None:
        if isinstance(value, Mapping):
            value = value.get("id")
        if isinstance(value, bool):
            return None
        try:
            number = int(str(value).strip()) if value is not None else None
        except (TypeError, ValueError):
            return None
        return number if number is not None and number > 0 else None

    live = _positive(live_endpoint_id)
    stored = _positive(stored_endpoint_id)
    return live is not None and stored is not None and stored != live


def reset_vm_uid_feature_cache() -> None:
    _VM_UID_SUPPORT_BY_SESSION.clear()


def note_vm_uid_feature_support(nb: object, rows: Iterable[object]) -> bool:
    """Cache whether this run's plugin serializer exposes guest UID fields."""

    mappings = [_serialized_mapping(row) for row in rows]
    cached = _VM_UID_SUPPORT_BY_SESSION.get(id(nb))
    if not mappings:
        return cached is True
    supported = any("proxmox_vm_uid" in row for row in mappings)
    if supported or cached is None:
        _VM_UID_SUPPORT_BY_SESSION[id(nb)] = supported
    return supported or cached is True


def cache_vm_uid_feature_support(nb: object, *, supported: bool) -> bool:
    """Cache explicit capability evidence without downgrading proven support."""

    cache_key = id(nb)
    if supported or cache_key not in _VM_UID_SUPPORT_BY_SESSION:
        _VM_UID_SUPPORT_BY_SESSION[cache_key] = supported
    return _VM_UID_SUPPORT_BY_SESSION[cache_key]


def cached_vm_uid_feature_support(nb: object) -> bool | None:
    return _VM_UID_SUPPORT_BY_SESSION.get(id(nb))
