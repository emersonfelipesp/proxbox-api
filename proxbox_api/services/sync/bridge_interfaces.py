"""Helpers for creating bridge interfaces on Proxmox node devices.

Bridges (vmbr0, vmbr1, etc.) are node-level Linux bridges. They are modeled in
NetBox as dcim.Interface objects on the Proxmox node device. Each VM NIC that uses
a bridge stores the node's dcim.Interface ID in the ``proxbox_bridge`` custom field
instead of using the VMInterface ``bridge`` FK (which only allows same-VM references).
"""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    clear_rest_get_cache_for_path,
    rest_create_async,
    rest_first_async,
    rest_reconcile_async,
)
from proxbox_api.proxmox_to_netbox.models import NetBoxInterfaceSyncState


def _normalize_node_interface_record(record: dict[str, object]) -> dict[str, object]:
    return {
        "device": record.get("device"),
        "name": record.get("name"),
        "type": record.get("type"),
        "status": record.get("status"),
        "tags": record.get("tags"),
        "custom_fields": record.get("custom_fields"),
    }


def _record_dict(record: object) -> dict[str, object]:
    if isinstance(record, dict):
        return record
    serializer = getattr(record, "serialize", None)
    if callable(serializer):
        serialized = serializer()
        if isinstance(serialized, dict):
            return serialized
    as_dict = getattr(record, "dict", None)
    if callable(as_dict):
        dumped = as_dict()
        if isinstance(dumped, dict):
            return dumped
    return {}


async def _reconcile_existing_node_bridge(record: object, payload: dict[str, object]) -> dict:
    desired_model = NetBoxInterfaceSyncState.model_validate(payload)
    desired_payload = desired_model.model_dump(exclude_none=True, by_alias=True)

    current_payload = NetBoxInterfaceSyncState.model_validate(
        _normalize_node_interface_record(_record_dict(record))
    ).model_dump(exclude_none=True, by_alias=True)

    # NetBox forbids moving interface components between devices; keep reconcile local.
    patch_payload = {
        key: value
        for key, value in desired_payload.items()
        if key != "device" and current_payload.get(key) != value
    }
    if patch_payload and hasattr(record, "save"):
        for field, value in patch_payload.items():
            setattr(record, field, value)
        await record.save()
    return _record_dict(record)


async def ensure_node_bridge_interface(
    nb,
    device_id: int,
    bridge_name: str,
    tag_refs: list[dict],
    now: datetime | None = None,
) -> dict:
    """Find-or-create a dcim.Interface (type=bridge) on the Proxmox node device.

    Args:
        nb: NetBox session.
        device_id: NetBox ID of the Proxmox node device.
        bridge_name: Bridge name (e.g. "vmbr0", "vmbr1").
        tag_refs: Tag references to attach.
        now: Timestamp for custom_fields (defaults to UTC now).

    Returns:
        The dcim.Interface record dict, or {} on failure.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        payload = {
            "device": device_id,
            "name": bridge_name,
            "type": "bridge",
            "status": "active",
            "tags": tag_refs,
            "custom_fields": {"proxmox_last_updated": now.isoformat()},
        }
        strict_query = {"device_id": device_id, "name": bridge_name, "limit": 2}
        existing = await rest_first_async(nb, "/api/dcim/interfaces/", query=strict_query)

        if existing is None:
            try:
                record = await rest_create_async(nb, "/api/dcim/interfaces/", payload)
            except ProxboxException:
                # Another worker may have created it; invalidate the GET cache so the
                # retry actually hits NetBox instead of returning the stale miss.
                clear_rest_get_cache_for_path(nb, "/api/dcim/interfaces/")
                existing = await rest_first_async(nb, "/api/dcim/interfaces/", query=strict_query)
                if existing is None:
                    raise
                record = await _reconcile_existing_node_bridge(existing, payload)
            else:
                record = _record_dict(record)
        else:
            record = await _reconcile_existing_node_bridge(existing, payload)

        if not isinstance(record, dict):
            record = _record_dict(record)
        if not record:
            # Fallback: keep previous behavior and try generic reconcile if record payload was malformed.
            record = await rest_reconcile_async(
                nb,
                "/api/dcim/interfaces/",
                lookup={"device_id": device_id, "name": bridge_name},
                payload=payload,
                schema=NetBoxInterfaceSyncState,
                current_normalizer=_normalize_node_interface_record,
                patchable_fields={"name", "type", "status", "tags", "custom_fields"},
            )
            record = _record_dict(record)
        return record or {}
    except Exception as exc:
        logger.warning(
            "Failed to ensure node bridge interface %s on device %s: %s",
            bridge_name,
            device_id,
            exc,
        )
        return {}


async def ensure_bridge_interfaces(
    nb,
    device_id: int | None,
    vm_id: int,
    bridge_name: str,
    tag_refs: list[dict],
    now: datetime | None = None,
) -> int | None:
    """Ensure the node-level dcim bridge exists and return its ID.

    This is the main entry point for all sync code paths. It creates/updates a
    dcim.Interface (type=bridge) on the Proxmox node device. The returned ID
    should be stored in the VM NIC's ``proxbox_bridge`` custom field so that all
    VMs referencing the same bridge share one authoritative interface record.

    The VMInterface ``bridge`` FK is NOT used because NetBox enforces a same-VM
    constraint on that field, making it impossible to share a bridge across VMs.

    Args:
        nb: NetBox session.
        device_id: NetBox ID of the Proxmox node device, or None if unknown.
        vm_id: Unused — kept for call-site compatibility during transition.
        bridge_name: Bridge name (e.g. "vmbr0", "vmbr1").
        tag_refs: Tag references to attach.
        now: Timestamp for custom_fields (defaults to UTC now).

    Returns:
        NetBox ID of the node-level dcim.Interface, or None on failure.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if device_id is None:
        return None

    node_bridge = await ensure_node_bridge_interface(nb, device_id, bridge_name, tag_refs, now)
    return node_bridge.get("id") if node_bridge else None
