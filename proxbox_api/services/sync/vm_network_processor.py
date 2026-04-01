"""Flattened network processing for VM sync - extracted from deeply nested blocks."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from proxbox_api.dependencies import NetBoxSessionDep
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxVirtualMachineInterfaceSyncState,
    NetBoxVlanSyncState,
)
from proxbox_api.services.sync.vm_helpers import normalized_mac


async def process_vm_network_interface(  # noqa: C901
    nb: NetBoxSessionDep,
    virtual_machine: dict[str, Any],
    interface_name: str,
    interface_config: dict[str, Any],
    guest_by_mac: dict[str, dict],
    guest_by_name: dict[str, dict],
    use_guest_agent_interface_name: bool,
    tag_refs: list[dict],
    resource_node: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Process a single VM network interface with flattened logic (no deep nesting).

    Args:
        nb: NetBox session
        virtual_machine: NetBox VM record
        interface_name: Interface name (net0, net1, etc.)
        interface_config: Interface configuration dictionary
        guest_by_mac: Guest interfaces indexed by MAC
        guest_by_name: Guest interfaces indexed by name
        use_guest_agent_interface_name: Use guest agent interface names
        tag_refs: Tag references
        resource_node: Proxmox node name
        now: Current timestamp

    Returns:
        Processed interface data dict or None if failed
    """
    if now is None:
        now = datetime.now()

    # Early returns for validation
    if not isinstance(interface_config, dict):
        logger.warning(f"Skipping interface {interface_name}: invalid config type")
        return None

    vm_id = virtual_machine.get("id")
    if not vm_id:
        logger.warning(f"Skipping interface {interface_name}: VM has no ID")
        return None

    # Resolve interface name
    config_interface_name = (
        str(interface_config.get("name", interface_name)).strip() or interface_name
    )
    interface_mac = interface_config.get("virtio", interface_config.get("hwaddr"))
    resolved_interface_name = config_interface_name

    # Try to get guest interface info
    guest_iface = None
    if interface_mac:
        guest_iface = guest_by_mac.get(normalized_mac(interface_mac))
    if guest_iface is None:
        guest_iface = guest_by_name.get(config_interface_name.lower())

    # Use guest agent name if available and enabled
    if use_guest_agent_interface_name and guest_iface:
        guest_name = str(guest_iface.get("name") or "").strip()
        if guest_name:
            resolved_interface_name = guest_name

    # Process bridge interface
    result = {
        "interface_name": resolved_interface_name,
        "mac_address": interface_config.get("virtio", interface_config.get("hwaddr")),
        "vlan_id": None,
        "bridge_id": None,
    }

    # Create/sync bridge if configured
    bridge_name = interface_config.get("bridge")
    if bridge_name:
        try:
            bridge = await rest_reconcile_async(
                nb,
                "/api/virtualization/interfaces/",
                lookup={"virtual_machine_id": vm_id, "name": bridge_name},
                payload={
                    "name": bridge_name,
                    "virtual_machine": vm_id,
                    "type": "bridge",
                    "description": f"Bridge interface of Device {resource_node}.",
                    "tags": tag_refs,
                    "custom_fields": {"proxmox_last_updated": now.isoformat()},
                },
                schema=NetBoxVirtualMachineInterfaceSyncState,
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "virtual_machine": record.get("virtual_machine"),
                    "type": record.get("type"),
                    "description": record.get("description"),
                    "bridge": record.get("bridge"),
                    "enabled": record.get("enabled"),
                    "mac_address": record.get("mac_address"),
                    "untagged_vlan": record.get("untagged_vlan"),
                    "mode": record.get("mode"),
                    "tags": record.get("tags"),
                    "custom_fields": record.get("custom_fields"),
                },
            )
            if bridge:
                result["bridge_id"] = (
                    bridge.get("id") if isinstance(bridge, dict) else getattr(bridge, "id", None)
                )
        except Exception as e:
            logger.warning(f"Failed to create bridge {bridge_name}: {e}")

    # Process VLAN tag if present
    vlan_tag_raw = interface_config.get("tag")
    if vlan_tag_raw is not None:
        try:
            vlan_tag = int(vlan_tag_raw)
            vlan_record = await rest_reconcile_async(
                nb,
                "/api/ipam/vlans/",
                lookup={"vid": vlan_tag},
                payload={
                    "vid": vlan_tag,
                    "name": f"VLAN {vlan_tag}",
                    "status": "active",
                    "tags": tag_refs,
                    "custom_fields": {"proxmox_last_updated": now.isoformat()},
                },
                schema=NetBoxVlanSyncState,
                current_normalizer=lambda record: {
                    "vid": record.get("vid"),
                    "name": record.get("name"),
                    "status": record.get("status"),
                    "tags": record.get("tags"),
                    "custom_fields": record.get("custom_fields"),
                },
            )
            if vlan_record:
                result["vlan_id"] = (
                    vlan_record.get("id")
                    if isinstance(vlan_record, dict)
                    else getattr(vlan_record, "id", None)
                )
        except Exception as e:
            logger.warning(f"Failed to create VLAN {vlan_tag_raw}: {e}")

    return result
