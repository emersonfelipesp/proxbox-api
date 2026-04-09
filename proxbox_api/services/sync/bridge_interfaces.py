"""Helpers for creating bridge interfaces on Proxmox node devices and VMs.

Bridges (vmbr0, vmbr1, etc.) are node-level Linux bridges. They are modeled in
NetBox as dcim.Interface objects on the Proxmox node device. Each VM that uses a
bridge additionally gets its own VMInterface of type "bridge" so that the VM's NIC
interface can reference it via the same-VM bridge FK constraint.
"""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxInterfaceSyncState,
    NetBoxVirtualMachineInterfaceSyncState,
)


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
        record = await rest_reconcile_async(
            nb,
            "/api/dcim/interfaces/",
            lookup={"device_id": device_id, "name": bridge_name},
            payload={
                "device": device_id,
                "name": bridge_name,
                "type": "bridge",
                "status": "active",
                "tags": tag_refs,
                "custom_fields": {"proxmox_last_updated": now.isoformat()},
            },
            schema=NetBoxInterfaceSyncState,
            current_normalizer=lambda rec: {
                "device": rec.get("device"),
                "name": rec.get("name"),
                "type": rec.get("type"),
                "status": rec.get("status"),
                "tags": rec.get("tags"),
                "custom_fields": rec.get("custom_fields"),
            },
        )
        if not isinstance(record, dict):
            record = getattr(record, "dict", lambda: {})()
        return record or {}
    except Exception as exc:
        logger.warning(
            "Failed to ensure node bridge interface %s on device %s: %s",
            bridge_name,
            device_id,
            exc,
        )
        return {}


async def ensure_vm_bridge_interface(
    nb,
    vm_id: int,
    bridge_name: str,
    tag_refs: list[dict],
    now: datetime | None = None,
) -> dict:
    """Find-or-create a VMInterface (type=bridge) on the virtual machine.

    Each VM that uses a bridge gets its own bridge VMInterface so that the NIC
    VMInterface can satisfy NetBox's same-VM bridge FK constraint.

    Args:
        nb: NetBox session.
        vm_id: NetBox ID of the virtual machine.
        bridge_name: Bridge name (e.g. "vmbr0", "vmbr1").
        tag_refs: Tag references to attach.
        now: Timestamp for custom_fields (defaults to UTC now).

    Returns:
        The VMInterface record dict, or {} on failure.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        record = await rest_reconcile_async(
            nb,
            "/api/virtualization/interfaces/",
            lookup={"virtual_machine_id": vm_id, "name": bridge_name},
            payload={
                "virtual_machine": vm_id,
                "name": bridge_name,
                "type": "bridge",
                "tags": tag_refs,
                "custom_fields": {"proxmox_last_updated": now.isoformat()},
            },
            schema=NetBoxVirtualMachineInterfaceSyncState,
            current_normalizer=lambda rec: {
                "virtual_machine": rec.get("virtual_machine"),
                "name": rec.get("name"),
                "type": rec.get("type"),
                "tags": rec.get("tags"),
                "custom_fields": rec.get("custom_fields"),
            },
        )
        if not isinstance(record, dict):
            record = getattr(record, "dict", lambda: {})()
        return record or {}
    except Exception as exc:
        logger.warning(
            "Failed to ensure VM bridge interface %s on VM %s: %s",
            bridge_name,
            vm_id,
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
    """Ensure both the node-level dcim bridge and the per-VM bridge VMInterface exist.

    This is the main entry point for all sync code paths.  It:
    1. Creates/updates a dcim.Interface (type=bridge) on the Proxmox node device.
    2. Creates/updates a VMInterface (type=bridge) with the same name on the VM.

    The VM's NIC interface must then set its ``bridge`` field to the returned ID
    so NetBox's same-VM FK constraint is satisfied.

    Args:
        nb: NetBox session.
        device_id: NetBox ID of the Proxmox node device, or None if unknown.
        vm_id: NetBox ID of the virtual machine.
        bridge_name: Bridge name (e.g. "vmbr0", "vmbr1").
        tag_refs: Tag references to attach.
        now: Timestamp for custom_fields (defaults to UTC now).

    Returns:
        NetBox ID of the per-VM bridge VMInterface, or None on failure.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Ensure the bridge exists on the node device (best-effort; non-fatal).
    if device_id is not None:
        await ensure_node_bridge_interface(nb, device_id, bridge_name, tag_refs, now)

    # Ensure the per-VM bridge VMInterface exists.
    vm_bridge = await ensure_vm_bridge_interface(nb, vm_id, bridge_name, tag_refs, now)
    return vm_bridge.get("id") if vm_bridge else None
