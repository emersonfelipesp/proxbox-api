"""Shared helpers for intent apply dispatchers."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from proxbox_api.logger import logger
from proxbox_api.services.verb_dispatch import build_journal_comments, utcnow_iso
from proxbox_api.session.netbox import get_netbox_async_session

SENSITIVE_KEYS = {"password", "cipassword"}


@dataclass(frozen=True)
class IntentEndpointContext:
    session: object
    endpoint_id: int | None
    netbox_id: int | None = None


JournalWriter = Callable[..., Awaitable[dict[str, object] | None]]


def coerce_endpoint_context(endpoint: object) -> IntentEndpointContext:
    session = getattr(endpoint, "session", None)
    endpoint_id = getattr(endpoint, "endpoint_id", None)
    netbox_id = getattr(endpoint, "netbox_id", None)
    if session is None:
        raise RuntimeError("intent dispatcher endpoint context is missing database session")
    return IntentEndpointContext(
        session=session,
        endpoint_id=int(endpoint_id) if endpoint_id is not None else None,
        netbox_id=int(netbox_id) if netbox_id is not None else None,
    )


def scrub_value(value: object) -> object:
    if isinstance(value, Mapping):
        scrubbed: dict[object, object] = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS:
                scrubbed[key] = "***" if item else None
            else:
                scrubbed[key] = scrub_value(item)
        return scrubbed
    if isinstance(value, list):
        return [scrub_value(item) for item in value]
    return value


def collect_sensitive_values(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS and isinstance(item, str) and item:
                found.append(item)
            else:
                found.extend(collect_sensitive_values(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(collect_sensitive_values(item))
    return found


def scrub_message(message: str, payload: object | None = None) -> str:
    scrubbed = re.sub(
        r"(?i)(password|cipassword)([\"']?\s*[:=]\s*[\"']?)([^,\"'\s}\]]+)",
        r"\1\2***",
        message,
    )
    if payload is not None:
        for secret in collect_sensitive_values(payload):
            scrubbed = scrubbed.replace(secret, "***")
    return scrubbed


def extract_upid(response: object) -> str | None:
    if isinstance(response, str):
        return response
    if isinstance(response, Mapping):
        data = response.get("data")
        if isinstance(data, str):
            return data
        if isinstance(data, Mapping):
            upid = data.get("upid") or data.get("UPID")
            return str(upid) if upid is not None else None
    return str(response) if response is not None else None


def merge_indexed_items(
    params: dict[str, object],
    items: list[dict],
    *,
    default_prefix: str,
) -> None:
    for index, item in enumerate(items):
        key = item.get("key") or item.get("name") or item.get("slot") or f"{default_prefix}{index}"
        value = item.get("value") or item.get("config")
        if value is None:
            value_parts = [
                f"{field}={field_value}"
                for field, field_value in item.items()
                if field not in {"key", "name", "slot"} and field_value is not None
            ]
            if not value_parts:
                continue
            value = ",".join(value_parts)
        params[str(key)] = value


def mapping_from_response(value: object) -> dict[str, object]:
    """Normalize SDK/Pydantic/dict response objects into a plain mapping."""
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="python", by_alias=True, exclude_none=True)
        if isinstance(dumped, dict):
            return mapping_from_response(dumped)
    if isinstance(value, Mapping):
        data = value.get("data")
        if isinstance(data, Mapping):
            return {str(key): item for key, item in data.items()}
        return {str(key): item for key, item in value.items()}
    return {}


def sequence_from_response(value: object) -> list[dict[str, object]]:
    """Normalize SDK/Pydantic/list response objects into record mappings."""
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="python", by_alias=True, exclude_none=True)
        return sequence_from_response(dumped)
    if isinstance(value, Mapping):
        data = value.get("data")
        if isinstance(data, list):
            return sequence_from_response(data)
        record = mapping_from_response(value)
        return [record] if record else []
    if isinstance(value, list):
        records: list[dict[str, object]] = []
        for item in value:
            record = mapping_from_response(item)
            if record:
                records.append(record)
        return records
    return []


def find_vmid_record(
    records: list[dict[str, object]],
    *,
    vmid: int,
    kind: str,
) -> dict[str, object] | None:
    for record in records:
        try:
            record_vmid = int(str(record.get("vmid")))
        except (TypeError, ValueError):
            continue
        record_kind = str(record.get("type") or kind)
        if record_vmid == vmid and record_kind == kind:
            return record
    return None


def find_current_node(
    records: list[dict[str, object]],
    *,
    vmid: int,
    kind: str,
) -> str:
    record = find_vmid_record(records, vmid=vmid, kind=kind)
    if record is None:
        return "unknown"
    node = record.get("node")
    return str(node) if node else "unknown"


def status_is_running(status_payload: object) -> bool:
    status_map = mapping_from_response(status_payload)
    return status_map.get("status") == "running"


def tags_to_config(tags: list[str]) -> str:
    return ";".join(tags)


def values_match(current: object, desired: object) -> bool:
    if current == desired:
        return True
    if isinstance(desired, int):
        try:
            return int(str(current)) == desired
        except (TypeError, ValueError):
            return False
    if isinstance(desired, str):
        return str(current) == desired
    return False


def set_delta_if_changed(
    delta: dict[str, object],
    current: Mapping[str, object],
    key: str,
    desired: object,
) -> None:
    if not values_match(current.get(key), desired):
        delta[key] = desired


def merge_indexed_delta(
    delta: dict[str, object],
    current: Mapping[str, object],
    items: list[dict],
    *,
    default_prefix: str,
) -> None:
    indexed: dict[str, object] = {}
    merge_indexed_items(indexed, items, default_prefix=default_prefix)
    for key, desired in indexed.items():
        set_delta_if_changed(delta, current, key, desired)


async def write_intent_journal(
    *,
    journal_writer: JournalWriter,
    endpoint_context: IntentEndpointContext,
    endpoint: object | None,
    verb: str,
    result: str,
    vmid: int,
    actor: str | None,
    run_uuid: str,
    kind: str,
    proxmox_upid: str | None = None,
    error_detail: str | None = None,
) -> None:
    try:
        nb = await get_netbox_async_session(database_session=endpoint_context.session)
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "intent.apply: NetBox session unavailable while journaling verb=%s vmid=%s: %s",
            verb,
            vmid,
            scrub_message(str(error)),
        )
        nb = object()

    endpoint_id = getattr(endpoint, "id", None) or endpoint_context.endpoint_id or 0
    endpoint_name = getattr(endpoint, "name", None) or "unknown"
    safe_error = scrub_message(error_detail) if error_detail is not None else None
    comments = build_journal_comments(
        verb=verb,
        actor=actor or "proxbox-api",
        result=result,
        endpoint_name=str(endpoint_name),
        endpoint_id=int(endpoint_id),
        dispatched_at=utcnow_iso(),
        proxmox_task_upid=proxmox_upid,
        error_detail=safe_error,
    )
    comments = f"{comments}\n- target_vmid: {vmid}\n- run_uuid: {run_uuid}"
    await journal_writer(
        nb,
        netbox_vm_id=endpoint_context.netbox_id or vmid,
        kind=kind,
        comments=comments,
    )
