"""VM and node interface + IP synchronization helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxInterfaceSyncState,
    NetBoxIpAddressSyncState,
    NetBoxVirtualMachineInterfaceSyncState,
    NetBoxVlanSyncState,
)


async def sync_node_interface_and_ip(
    nb,
    device: dict,
    interface_name: str,
    interface_config: dict,
    tag_refs: list[dict],
) -> dict:
    interface_type_mapping = {
        "lo": "loopback",
        "bridge": "bridge",
        "bond": "lag",
        "vlan": "virtual",
    }

    node_cidr = interface_config.get("cidr") or interface_config.get("address")
    vlan_nb_id: int | None = None

    iface_type = interface_config.get("type", "other")
    vlan_id_raw = interface_config.get("vlan_id")
    if iface_type == "vlan" and vlan_id_raw is not None:
        try:
            vlan_vid = int(vlan_id_raw)
            vlan_record = await rest_reconcile_async(
                nb,
                "/api/ipam/vlans/",
                lookup={"vid": vlan_vid},
                payload={
                    "vid": vlan_vid,
                    "name": f"VLAN {vlan_vid}",
                    "status": "active",
                    "tags": tag_refs,
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
            vlan_nb_id = (
                vlan_record.get("id")
                if isinstance(vlan_record, dict)
                else getattr(vlan_record, "id", None)
            )
        except Exception as vlan_exc:
            logger.warning(
                "Failed to create/sync VLAN vid=%s for node interface %s: %s",
                vlan_id_raw,
                interface_name,
                vlan_exc,
            )

    interface = await rest_reconcile_async(
        nb,
        "/api/dcim/interfaces/",
        lookup={
            "device_id": device.get("id", 0),
            "name": interface_name,
        },
        payload={
            "device": device.get("id", 0),
            "name": interface_name,
            "status": "active",
            "type": interface_type_mapping.get(iface_type, "other"),
            "untagged_vlan": vlan_nb_id,
            "mode": "access" if vlan_nb_id is not None else None,
            "tags": tag_refs,
        },
        schema=NetBoxInterfaceSyncState,
        current_normalizer=lambda record: {
            "device": record.get("device"),
            "name": record.get("name"),
            "status": record.get("status"),
            "type": record.get("type"),
            "untagged_vlan": record.get("untagged_vlan"),
            "mode": record.get("mode"),
            "tags": record.get("tags"),
        },
    )
    interface_id = getattr(interface, "id", None) or (
        interface.get("id") if isinstance(interface, dict) else None
    )
    result: dict = {"id": interface_id, "name": interface_name}

    if node_cidr and interface_id is not None:
        try:
            ip_record = await rest_reconcile_async(
                nb,
                "/api/ipam/ip-addresses/",
                lookup={"address": node_cidr},
                payload={
                    "address": node_cidr,
                    "assigned_object_type": "dcim.interface",
                    "assigned_object_id": int(interface_id),
                    "status": "active",
                    "tags": tag_refs,
                },
                schema=NetBoxIpAddressSyncState,
                current_normalizer=lambda record: {
                    "address": record.get("address"),
                    "assigned_object_type": record.get("assigned_object_type"),
                    "assigned_object_id": record.get("assigned_object_id"),
                    "status": record.get("status"),
                    "tags": record.get("tags"),
                },
            )
            ip_id = getattr(ip_record, "id", None) or (
                ip_record.get("id") if isinstance(ip_record, dict) else None
            )
            result["ip_id"] = ip_id
            result["ip_address"] = node_cidr
        except Exception as ip_exc:
            logger.warning(
                "Failed to create IP %s for node interface %s: %s",
                node_cidr,
                interface_name,
                ip_exc,
            )

    return result


def _normalized_mac(value: str | None) -> str:
    return str(value or "").strip().lower()


async def sync_vm_interface_and_ip(
    nb,
    virtual_machine: dict,
    interface_name: str,
    interface_config: dict,
    guest_iface: dict | None,
    tag_refs: list[dict],
    use_guest_agent_interface_name: bool = True,
    now: datetime | None = None,
) -> dict:
    if now is None:
        now = datetime.now(timezone.utc)

    vm_id = virtual_machine.get("id")

    bridge: dict = {}
    bridge_name = interface_config.get("bridge")
    if bridge_name and vm_id:
        bridge = await rest_reconcile_async(
            nb,
            "/api/virtualization/interfaces/",
            lookup={
                "virtual_machine_id": vm_id,
                "name": bridge_name,
            },
            payload={
                "name": bridge_name,
                "virtual_machine": vm_id,
                "type": "bridge",
                "tags": tag_refs,
                "custom_fields": {"proxmox_last_updated": now.isoformat()},
            },
            schema=NetBoxVirtualMachineInterfaceSyncState,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "virtual_machine": record.get("virtual_machine"),
                "type": record.get("type"),
                "tags": record.get("tags"),
                "custom_fields": record.get("custom_fields"),
            },
        )
        if not isinstance(bridge, dict):
            bridge = getattr(bridge, "dict", lambda: {})()

    vlan_nb_id: int | None = None
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
            vlan_nb_id = (
                vlan_record.get("id")
                if isinstance(vlan_record, dict)
                else getattr(vlan_record, "id", None)
            )
        except Exception as vlan_exc:
            logger.warning(
                "Failed to create/sync VLAN tag=%s for VM interface %s: %s",
                vlan_tag_raw,
                interface_name,
                vlan_exc,
            )

    mac_address = interface_config.get("virtio") or interface_config.get("hwaddr")

    resolved_name = interface_name
    if use_guest_agent_interface_name and guest_iface:
        guest_name = str(guest_iface.get("name") or "").strip()
        if guest_name:
            resolved_name = guest_name
            guest_mac = guest_iface.get("mac_address")
            if guest_mac and not mac_address:
                mac_address = _normalized_mac(guest_mac)

    payload: dict = {
        "name": resolved_name,
        "enabled": True,
        "bridge": bridge.get("id") if bridge else None,
        "mac_address": mac_address,
        "untagged_vlan": vlan_nb_id,
        "mode": "access" if vlan_nb_id is not None else None,
        "tags": tag_refs,
        "custom_fields": {"proxmox_last_updated": now.isoformat()},
    }
    if vm_id:
        payload["virtual_machine"] = vm_id

    lookup: dict = {"name": resolved_name}
    if vm_id:
        lookup["virtual_machine_id"] = vm_id

    vm_interface = await rest_reconcile_async(
        nb,
        "/api/virtualization/interfaces/",
        lookup=lookup,
        payload=payload,
        schema=NetBoxVirtualMachineInterfaceSyncState,
        current_normalizer=lambda record: {
            "name": record.get("name"),
            "virtual_machine": record.get("virtual_machine"),
            "enabled": record.get("enabled"),
            "bridge": record.get("bridge"),
            "mac_address": record.get("mac_address"),
            "type": record.get("type"),
            "description": record.get("description"),
            "untagged_vlan": record.get("untagged_vlan"),
            "mode": record.get("mode"),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
    )
    if not isinstance(vm_interface, dict):
        vm_interface = getattr(vm_interface, "dict", lambda: {})()

    interface_id = (
        vm_interface.get("id")
        if isinstance(vm_interface, dict)
        else getattr(vm_interface, "id", None)
    )
    result: dict = {
        "id": interface_id,
        "mac_address": mac_address,
    }

    interface_ip: str | None = None
    if guest_iface:
        for addr in guest_iface.get("ip_addresses") or []:
            if not isinstance(addr, dict):
                continue
            ip_text = str(addr.get("ip_address") or "").strip()
            if not ip_text:
                continue
            try:
                from ipaddress import ip_address as ipaddr
                parsed = ipaddr(ip_text)
            except ValueError:
                continue
            if parsed.is_loopback or parsed.is_link_local:
                continue
            prefix = addr.get("prefix")
            if isinstance(prefix, int) and 0 <= prefix <= 128:
                interface_ip = f"{parsed.compressed}/{prefix}"
            else:
                interface_ip = parsed.compressed
            break

    if not interface_ip:
        interface_ip = interface_config.get("ip")

    if interface_ip and interface_ip != "dhcp" and interface_id is not None:
        try:
            ip_record = await rest_reconcile_async(
                nb,
                "/api/ipam/ip-addresses/",
                lookup={"address": interface_ip},
                payload={
                    "address": interface_ip,
                    "assigned_object_type": "virtualization.vminterface",
                    "assigned_object_id": interface_id,
                    "status": "active",
                    "tags": tag_refs,
                    "custom_fields": {"proxmox_last_updated": now.isoformat()},
                },
                schema=NetBoxIpAddressSyncState,
                current_normalizer=lambda record: {
                    "address": record.get("address"),
                    "assigned_object_type": record.get("assigned_object_type"),
                    "assigned_object_id": record.get("assigned_object_id"),
                    "status": record.get("status"),
                    "tags": record.get("tags"),
                },
            )
            ip_id = (
                ip_record.get("id")
                if isinstance(ip_record, dict)
                else getattr(ip_record, "id", None)
            )
            result["ip_id"] = ip_id
            result["ip_address"] = interface_ip
        except Exception as ip_exc:
            logger.warning(
                "Failed to create IP %s for VM interface %s: %s",
                interface_ip,
                interface_name,
                ip_exc,
            )

    return result
