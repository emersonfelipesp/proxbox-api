"""Guest-OS VM interface reconciliation for the netbox-proxbox plugin."""

from __future__ import annotations

from typing import Literal, cast

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_create_async, rest_first_async, rest_patch_async
from proxbox_api.services.sync.vm_helpers import _is_skippable_ip, normalized_mac, record_id

VMInterfaceSyncStrategy = Literal["guest_os_model", "legacy_rename"]
DEFAULT_VM_INTERFACE_SYNC_STRATEGY: VMInterfaceSyncStrategy = "guest_os_model"

GUEST_VM_INTERFACE_PATH = "/api/plugins/proxbox/guest-vm-interfaces/"
GUEST_VM_INTERFACE_ADDRESS_PATH = "/api/plugins/proxbox/guest-vm-interface-addresses/"


def normalize_vm_interface_sync_strategy(strategy: object) -> VMInterfaceSyncStrategy:
    """Normalize the VM interface sync strategy query value."""
    value = str(strategy or DEFAULT_VM_INTERFACE_SYNC_STRATEGY).strip().lower()
    if value in {"guest_os_model", "legacy_rename"}:
        return cast(VMInterfaceSyncStrategy, value)
    logger.warning(
        "Unknown vm_interface_sync_strategy=%r; falling back to %s",
        strategy,
        DEFAULT_VM_INTERFACE_SYNC_STRATEGY,
    )
    return DEFAULT_VM_INTERFACE_SYNC_STRATEGY


def should_use_guest_agent_core_interface_name(
    use_guest_agent_interface_name: bool,
    strategy: object,
) -> bool:
    """Return True only for the deprecated legacy core-interface rename mode."""
    return bool(use_guest_agent_interface_name) and (
        normalize_vm_interface_sync_strategy(strategy) == "legacy_rename"
    )


def warn_legacy_vm_interface_strategy() -> None:
    """Emit the one-line deprecation warning for legacy rename mode."""
    logger.warning(
        "vm_interface_sync_strategy=legacy_rename is deprecated; use guest_os_model "
        "to keep Proxmox netX VMInterfaces and sync guest OS interfaces separately."
    )


def _is_plugin_endpoint_unavailable(error: Exception) -> bool:
    detail = getattr(error, "detail", None)
    message = getattr(error, "message", None)
    text = " ".join(str(part) for part in (detail, message, error) if part).lower()
    return any(
        marker in text
        for marker in (
            "404",
            "not found",
            "not_found",
            "unavailable",
            "unknown endpoint",
            "invalid endpoint",
        )
    )


def _relation_id(value: object) -> int | None:
    return record_id(value)


def _current_guest_interface(record: dict[str, object]) -> dict[str, object]:
    return {
        "virtual_machine": _relation_id(record.get("virtual_machine")),
        "vm_interface": _relation_id(record.get("vm_interface")),
        "name": record.get("name"),
        "mac_address": normalized_mac(record.get("mac_address")),
        "enabled": record.get("enabled"),
        "mtu": record.get("mtu"),
        "tags": record.get("tags"),
        "custom_fields": record.get("custom_fields"),
    }


def _current_guest_interface_address(record: dict[str, object]) -> dict[str, object]:
    return {
        "guest_interface": _relation_id(record.get("guest_interface")),
        "ip_address": _relation_id(record.get("ip_address")),
    }


async def _best_effort_reconcile(
    nb: object,
    path: str,
    *,
    lookup: dict[str, object],
    payload: dict[str, object],
    current_normalizer,
    nullable_fields: set[str] | None = None,
    lookup_query_field_map: dict[str, str] | None = None,
) -> dict[str, object] | None:
    try:
        query = {
            (lookup_query_field_map or {}).get(key, key): value
            for key, value in lookup.items()
            if value not in (None, "")
        }
        existing = await rest_first_async(
            nb,
            path,
            query={**query, "limit": 2},
        )
        if existing is not None:
            existing_dict = _record_to_dict(existing)
            current_payload = current_normalizer(existing_dict)
            if not _lookup_matches(current_payload, lookup):
                logger.warning(
                    "Skipping guest VM interface plugin sync for %s because the "
                    "endpoint returned a non-matching record for query %s: %s",
                    path,
                    query,
                    current_payload,
                )
                return None

            patch_payload = _build_patch_payload(
                current_payload,
                payload,
                current_normalizer=current_normalizer,
                nullable_fields=nullable_fields,
            )
            if patch_payload:
                record_id_value = record_id(existing_dict)
                if record_id_value is None:
                    logger.warning(
                        "Skipping guest VM interface plugin patch for %s because "
                        "the matched record has no id",
                        path,
                    )
                    return None
                patched = await rest_patch_async(nb, path, record_id_value, patch_payload)
                return _record_to_dict(patched)
            return existing_dict

        created = await rest_create_async(nb, path, _payload_for_create(payload))
        return _record_to_dict(created)
    except Exception as exc:  # noqa: BLE001 - plugin writes are intentionally best-effort
        if _is_plugin_endpoint_unavailable(exc):
            logger.warning(
                "Skipping guest VM interface plugin sync because %s is unavailable: %s",
                path,
                getattr(exc, "detail", str(exc)),
            )
        else:
            logger.warning(
                "Guest VM interface plugin sync failed at %s; core VM interface/IP sync "
                "will continue: %s",
                path,
                getattr(exc, "detail", str(exc)),
            )
        return None


def _payload_for_create(payload: dict[str, object]) -> dict[str, object]:
    return dict(payload)


def _record_to_dict(record: object) -> dict[str, object]:
    if isinstance(record, dict):
        return record
    serialize = getattr(record, "serialize", None)
    if callable(serialize):
        serialized = serialize()
        if isinstance(serialized, dict):
            return serialized
    dumped = getattr(record, "dict", lambda: {})()
    return dumped if isinstance(dumped, dict) else {}


def _lookup_matches(current_payload: dict[str, object], lookup: dict[str, object]) -> bool:
    for key, expected in lookup.items():
        current = current_payload.get(key)
        if key.endswith("_id"):
            current = _relation_id(current)
        if _relation_id(current) is not None or _relation_id(expected) is not None:
            if _relation_id(current) != _relation_id(expected):
                return False
            continue
        if current != expected:
            return False
    return True


def _build_patch_payload(
    current_payload: dict[str, object],
    desired_payload: dict[str, object],
    *,
    current_normalizer,
    nullable_fields: set[str] | None,
) -> dict[str, object]:
    desired_normalized = current_normalizer(desired_payload)
    patch_payload = {
        key: value
        for key, value in desired_payload.items()
        if desired_normalized.get(key) != current_payload.get(key)
    }
    if nullable_fields:
        for field in nullable_fields:
            if field in desired_payload and desired_payload[field] is None:
                if current_payload.get(field) is not None:
                    patch_payload[field] = None
    return patch_payload


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_ip_key(value: object) -> str | None:
    host, _, prefix_part = str(value or "").strip().partition("/")
    skip, cleaned_host = _is_skippable_ip(host, ignore_ipv6_link_local=False)
    if skip or cleaned_host is None:
        return None
    return f"{cleaned_host}/{prefix_part}" if prefix_part else cleaned_host


def _guest_ip_keys(guest_iface: dict[str, object]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for address in guest_iface.get("ip_addresses") or []:
        if not isinstance(address, dict):
            continue
        ip_text = str(address.get("ip_address") or "").strip()
        if not ip_text:
            continue
        prefix = address.get("prefix")
        candidate = ip_text
        if isinstance(prefix, int) and 0 <= prefix <= 128:
            candidate = f"{ip_text}/{prefix}"
        key = _normalize_ip_key(candidate)
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _normalize_core_interface_id_by_mac(
    core_interface_id_by_mac: dict[str, int],
) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for mac, interface_id in core_interface_id_by_mac.items():
        key = normalized_mac(mac)
        if not key:
            continue
        try:
            normalized[key] = int(interface_id)
        except (TypeError, ValueError):
            continue
    return normalized


def _normalize_ip_ids_by_interface_id(
    ip_ids_by_interface_id: dict[int, dict[str, int]],
) -> dict[int, dict[str, int]]:
    normalized: dict[int, dict[str, int]] = {}
    for raw_interface_id, ip_map in ip_ids_by_interface_id.items():
        try:
            interface_id = int(raw_interface_id)
        except (TypeError, ValueError):
            continue
        normalized_map: dict[str, int] = {}
        for raw_address, raw_ip_id in ip_map.items():
            address = _normalize_ip_key(raw_address)
            if address is None:
                continue
            try:
                normalized_map[address] = int(raw_ip_id)
            except (TypeError, ValueError):
                continue
        normalized[interface_id] = normalized_map
    return normalized


async def reconcile_guest_vm_interfaces(
    nb: object,
    vm_id: int,
    guest_interfaces: list[dict[str, object]],
    core_interface_id_by_mac: dict[str, int],
    ip_ids_by_interface_id: dict[int, dict[str, int]],
    tag_refs: list[dict[str, object]],
    strategy: object,
) -> list[dict[str, object]]:
    """Upsert guest-OS interface plugin rows and links to existing core IPs."""
    if normalize_vm_interface_sync_strategy(strategy) != "guest_os_model":
        return []
    if not vm_id or not guest_interfaces:
        return []

    core_ids_by_mac = _normalize_core_interface_id_by_mac(core_interface_id_by_mac)
    ip_ids_by_interface = _normalize_ip_ids_by_interface_id(ip_ids_by_interface_id)

    records: list[dict[str, object]] = []
    for guest_iface in guest_interfaces:
        if not isinstance(guest_iface, dict):
            continue
        guest_name = str(guest_iface.get("name") or "").strip()
        if not guest_name:
            continue
        guest_mac = normalized_mac(guest_iface.get("mac_address"))
        core_interface_id = core_ids_by_mac.get(guest_mac)

        guest_payload = {
            "virtual_machine": int(vm_id),
            "vm_interface": core_interface_id,
            "name": guest_name,
            "mac_address": guest_mac,
            "enabled": True,
            "mtu": _int_or_none(guest_iface.get("mtu")),
            "tags": tag_refs,
            "custom_fields": {},
        }
        guest_record = await _best_effort_reconcile(
            nb,
            GUEST_VM_INTERFACE_PATH,
            lookup={"virtual_machine": int(vm_id), "name": guest_name},
            payload=guest_payload,
            current_normalizer=_current_guest_interface,
            nullable_fields={"vm_interface", "mtu"},
            lookup_query_field_map={"virtual_machine": "virtual_machine_id"},
        )
        if not guest_record:
            continue

        records.append(guest_record)
        guest_id = record_id(guest_record)
        if guest_id is None or core_interface_id is None:
            continue

        ip_id_by_address = ip_ids_by_interface.get(int(core_interface_id), {})
        for ip_key in _guest_ip_keys(guest_iface):
            ip_id = ip_id_by_address.get(ip_key)
            if ip_id is None:
                continue
            await _best_effort_reconcile(
                nb,
                GUEST_VM_INTERFACE_ADDRESS_PATH,
                lookup={"guest_interface": int(guest_id), "ip_address": int(ip_id)},
                payload={"guest_interface": int(guest_id), "ip_address": int(ip_id)},
                current_normalizer=_current_guest_interface_address,
                lookup_query_field_map={
                    "guest_interface": "guest_interface_id",
                    "ip_address": "ip_address_id",
                },
            )

    return records
