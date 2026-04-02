"""Individual Interface sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxVirtualMachineInterfaceSyncState,
    NetBoxVlanSyncState,
)
from proxbox_api.services.proxmox_helpers import (
    get_qemu_guest_agent_network_interfaces,
    get_vm_config_individual,
)
from proxbox_api.services.sync.individual.base import BaseIndividualSyncService
from proxbox_api.services.sync.individual.helpers import (
    build_interface_lookup_key,
    normalize_mac,
    parse_key_value_string,
)


def _best_guest_agent_ip(
    guest_iface: dict | None,
    ignore_ipv6_link_local: bool = True,
) -> str | None:
    """Select the best IP address from guest agent interface data."""
    from ipaddress import ip_address

    if not isinstance(guest_iface, dict):
        return None
    for addr in guest_iface.get("ip_addresses") or []:
        if not isinstance(addr, dict):
            continue
        if str(addr.get("ip_address_type") or "").lower() == "ipv6":
            continue
        ip_text = str(addr.get("ip_address") or "").strip()
        if not ip_text:
            continue
        try:
            parsed = ip_address(ip_text)
        except ValueError:
            continue
        if parsed.is_loopback:
            continue
        if ignore_ipv6_link_local and parsed.is_link_local:
            continue
        prefix = addr.get("prefix")
        if isinstance(prefix, int) and 0 <= prefix <= 128:
            return f"{parsed.compressed}/{prefix}"
        return parsed.compressed
    return None


async def sync_interface_individual(
    nb: object,
    px: object,
    tag: object,
    node: str,
    vm_type: str,
    vmid: int,
    interface_name: str,
    auto_create_vm: bool = True,
    dry_run: bool = False,
) -> dict:
    """Sync a single Interface from Proxmox to NetBox.

    Args:
        nb: NetBox async session.
        px: Single Proxmox session.
        tag: ProxboxTagDep object.
        node: Proxmox node name.
        vm_type: 'qemu' or 'lxc'.
        vmid: Proxmox VM ID.
        interface_name: Name of the interface (e.g., 'net0', 'eth0').
        auto_create_vm: Whether to auto-create the VM if it doesn't exist.
        dry_run: If True, return what would be synced without making changes.

    Returns:
        IndividualSyncResponse dict.
    """
    service = BaseIndividualSyncService(nb, px, tag)
    tag_refs = service.tag_refs
    now = datetime.now(timezone.utc)

    try:
        vm_config = get_vm_config_individual(px, node, vm_type, vmid)
    except Exception:
        vm_config = {}

    guest_interfaces: list[dict] = []
    if vm_type == "qemu":
        try:
            guest_interfaces = get_qemu_guest_agent_network_interfaces(px, node, vmid)
        except Exception:
            pass

    net_config: dict[str, str] = {}
    for key, value in vm_config.items():
        if key.startswith("net") and not key.startswith("nets"):
            config_entry = parse_key_value_string(value)
            if config_entry:
                net_config[key] = config_entry

    target_config = net_config.get(interface_name, {})
    mac_address = target_config.get("virtio") or target_config.get("hwaddr")
    bridge = target_config.get("bridge")
    vlan_tag_raw = target_config.get("tag")

    resolved_name = interface_name
    guest_iface = None
    if guest_interfaces:
        guest_by_name = {
            str(iface.get("name", "")).strip().lower(): iface for iface in guest_interfaces
        }
        guest_by_mac = {
            normalize_mac(iface.get("mac_address")): iface
            for iface in guest_interfaces
            if normalize_mac(iface.get("mac_address"))
        }
        guest_iface = guest_by_name.get(interface_name.lower())
        if guest_iface is None and mac_address:
            guest_iface = guest_by_mac.get(normalize_mac(mac_address))
        if guest_iface:
            guest_name = str(guest_iface.get("name") or "").strip()
            if guest_name:
                resolved_name = guest_name
                guest_mac = guest_iface.get("mac_address")
                if guest_mac and not mac_address:
                    mac_address = normalize_mac(guest_mac)

    proxmox_resource: dict[str, object] = {
        "vmid": vmid,
        "node": node,
        "type": vm_type,
        "interface_name": resolved_name,
        "mac_address": mac_address,
        "bridge": bridge,
        "vlan_tag": vlan_tag_raw,
        "guest_data": guest_iface,
        "proxmox_last_updated": now.isoformat(),
    }

    if dry_run:
        existing_vms = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"cf_proxmox_vm_id": vmid},
        )
        vm_id = None
        if existing_vms:
            vm_id = getattr(existing_vms[0], "id", None)

        netbox_object = None
        if vm_id:
            existing = await rest_list_async(
                nb,
                "/api/virtualization/interfaces/",
                query={"virtual_machine_id": vm_id, "name": resolved_name},
            )
            if existing:
                netbox_object = (
                    existing[0].serialize() if hasattr(existing[0], "serialize") else None
                )

        vm_dep: dict[str, object] = {"object_type": "vm", "vmid": vmid}
        return {
            "object_type": "interface",
            "action": "dry_run",
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": True,
            "dependencies_synced": [vm_dep],
            "error": None,
        }

    try:
        existing_vms = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"cf_proxmox_vm_id": vmid},
        )
        if not existing_vms:
            if auto_create_vm:
                from proxbox_api.services.sync.individual.vm_sync import sync_vm_individual

                cluster_name = getattr(px, "name", "unknown")
                await sync_vm_individual(
                    nb, px, tag, cluster_name, node, vm_type, vmid, dry_run=False
                )
                existing_vms = await rest_list_async(
                    nb,
                    "/api/virtualization/virtual-machines/",
                    query={"cf_proxmox_vm_id": vmid},
                )
            else:
                return {
                    "object_type": "interface",
                    "action": "error",
                    "proxmox_resource": proxmox_resource,
                    "netbox_object": None,
                    "dry_run": False,
                    "dependencies_synced": [],
                    "error": f"VM with vmid={vmid} not found in NetBox and auto_create_vm=False",
                }

        vm_record = existing_vms[0]
        vm_id = getattr(vm_record, "id", None)
        if vm_id is None:
            return {
                "object_type": "interface",
                "action": "error",
                "proxmox_resource": proxmox_resource,
                "netbox_object": None,
                "dry_run": False,
                "dependencies_synced": [],
                "error": f"Could not resolve VM ID for vmid={vmid}",
            }

        vlan_nb_id: int | None = None
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
                vlan_nb_id = getattr(vlan_record, "id", None) if vlan_record else None
            except Exception:
                pass

        bridge_id: int | None = None

        interface_payload: dict[str, object] = {
            "name": resolved_name,
            "enabled": True,
            "bridge": bridge_id,
            "mac_address": mac_address,
            "untagged_vlan": vlan_nb_id,
            "mode": "access" if vlan_nb_id else None,
            "tags": tag_refs,
            "custom_fields": {"proxmox_last_updated": now.isoformat()},
        }

        if vm_id:
            interface_payload["virtual_machine"] = vm_id

        interface_record = await rest_reconcile_async(
            nb,
            "/api/virtualization/interfaces/",
            lookup=build_interface_lookup_key(resolved_name, vm_id),
            payload=interface_payload,
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

        netbox_object = (
            interface_record.serialize() if hasattr(interface_record, "serialize") else None
        )
        action = "created" if getattr(interface_record, "id", None) else "updated"

        return {
            "object_type": "interface",
            "action": action,
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": False,
            "dependencies_synced": [{"object_type": "vm", "vmid": vmid, "action": action}],
            "error": None,
        }

    except Exception as error:
        return {
            "object_type": "interface",
            "action": "error",
            "proxmox_resource": proxmox_resource,
            "netbox_object": None,
            "dry_run": False,
            "dependencies_synced": [],
            "error": str(error),
        }
