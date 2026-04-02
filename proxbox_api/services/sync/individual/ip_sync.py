"""Individual IP Address sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxIpAddressSyncState
from proxbox_api.services.proxmox_helpers import get_qemu_guest_agent_network_interfaces
from proxbox_api.services.sync.individual.base import BaseIndividualSyncService
from proxbox_api.services.sync.individual.helpers import build_ip_lookup_key


async def sync_ip_individual(
    nb: object,
    px: object,
    tag: object,
    node: str,
    vm_type: str,
    vmid: int,
    ip_address: str,
    interface_name: str | None = None,
    auto_create_vm: bool = True,
    auto_create_interface: bool = True,
    dry_run: bool = False,
) -> dict:
    """Sync a single IP Address from Proxmox to NetBox.

    Args:
        nb: NetBox async session.
        px: Single Proxmox session.
        tag: ProxboxTagDep object.
        node: Proxmox node name.
        vm_type: 'qemu' or 'lxc'.
        vmid: Proxmox VM ID.
        ip_address: IP address to sync (e.g., '192.168.1.1/24').
        interface_name: Optional interface name to resolve interface.
        auto_create_vm: Whether to auto-create the VM if it doesn't exist.
        auto_create_interface: Whether to auto-create the interface if it doesn't exist.
        dry_run: If True, return what would be synced without making changes.

    Returns:
        IndividualSyncResponse dict.
    """
    service = BaseIndividualSyncService(nb, px, tag)
    tag_refs = service.tag_refs
    now = datetime.now(timezone.utc)

    guest_interfaces: list[dict] = []
    if vm_type == "qemu":
        try:
            guest_interfaces = get_qemu_guest_agent_network_interfaces(px, node, vmid)
        except Exception:
            pass

    resolved_interface = interface_name
    if guest_interfaces and not interface_name:
        for iface in guest_interfaces:
            for addr in iface.get("ip_addresses") or []:
                addr_ip = str(addr.get("ip_address") or "").strip()
                addr_ip_clean = addr_ip.split("/")[0] if "/" in addr_ip else addr_ip
                ip_address_clean = ip_address.split("/")[0] if "/" in ip_address else ip_address
                if addr_ip_clean == ip_address_clean:
                    resolved_interface = str(iface.get("name") or "").strip()
                    break

    proxmox_resource: dict[str, object] = {
        "vmid": vmid,
        "node": node,
        "type": vm_type,
        "ip_address": ip_address,
        "interface_name": resolved_interface,
        "proxmox_last_updated": now.isoformat(),
    }

    if dry_run:
        existing_ips = await rest_list_async(
            nb,
            "/api/ipam/ip-addresses/",
            query={"address": ip_address.split("/")[0]},
        )
        netbox_object = None
        if existing_ips:
            netbox_object = (
                existing_ips[0].serialize() if hasattr(existing_ips[0], "serialize") else None
            )

        vm_dep: dict[str, object] = {
            "object_type": "vm",
            "vmid": vmid,
            "cluster_name": getattr(px, "name", None),
            "node": node,
            "type": vm_type,
        }
        iface_dep: dict[str, object] | None = (
            {
                "object_type": "interface",
                "name": resolved_interface,
                "cluster_name": getattr(px, "name", None),
                "node": node,
                "type": vm_type,
                "vmid": vmid,
            }
            if resolved_interface
            else None
        )
        deps = [vm_dep]
        if iface_dep:
            deps.append(iface_dep)

        return {
            "object_type": "ip_address",
            "action": "dry_run",
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": True,
            "dependencies_synced": deps,
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
                    "object_type": "ip_address",
                    "action": "error",
                    "proxmox_resource": proxmox_resource,
                    "netbox_object": None,
                    "dry_run": False,
                    "dependencies_synced": [],
                    "error": f"VM with vmid={vmid} not found in NetBox",
                }

        vm_record = existing_vms[0]
        vm_id = getattr(vm_record, "id", None)

        interface_id: int | None = None
        if resolved_interface:
            existing_ifaces = await rest_list_async(
                nb,
                "/api/virtualization/interfaces/",
                query={"virtual_machine_id": vm_id, "name": resolved_interface},
            )
            if existing_ifaces:
                interface_id = getattr(existing_ifaces[0], "id", None)

            if not interface_id and auto_create_interface:
                from proxbox_api.services.sync.individual.interface_sync import (
                    sync_interface_individual,
                )

                iface_result = await sync_interface_individual(
                    nb,
                    px,
                    tag,
                    node,
                    vm_type,
                    vmid,
                    resolved_interface,
                    auto_create_vm=False,
                    dry_run=False,
                )
                if iface_result.get("netbox_object"):
                    interface_id = iface_result["netbox_object"].get("id")

        ip_payload: dict[str, object] = {
            "address": ip_address,
            "status": "active",
            "tags": tag_refs,
            "custom_fields": {"proxmox_last_updated": now.isoformat()},
        }

        if interface_id:
            ip_payload["assigned_object_type"] = "virtualization.vminterface"
            ip_payload["assigned_object_id"] = interface_id

        existing_ips = await rest_list_async(
            nb,
            "/api/ipam/ip-addresses/",
            query={"address": ip_address.split("/")[0]},
        )
        ip_record = await rest_reconcile_async(
            nb,
            "/api/ipam/ip-addresses/",
            lookup=build_ip_lookup_key(ip_address),
            payload=ip_payload,
            schema=NetBoxIpAddressSyncState,
            current_normalizer=lambda record: {
                "address": record.get("address"),
                "assigned_object_type": record.get("assigned_object_type"),
                "assigned_object_id": record.get("assigned_object_id"),
                "status": record.get("status"),
                "tags": record.get("tags"),
            },
        )

        netbox_object = ip_record.serialize() if hasattr(ip_record, "serialize") else None
        action = "updated" if existing_ips else "created"

        dependencies: list[dict] = [
            {
                "object_type": "vm",
                "vmid": vmid,
                "cluster_name": getattr(px, "name", None),
                "node": node,
                "type": vm_type,
                "action": action,
            }
        ]
        if resolved_interface:
            dependencies.append(
                {
                    "object_type": "interface",
                    "name": resolved_interface,
                    "cluster_name": getattr(px, "name", None),
                    "node": node,
                    "type": vm_type,
                    "vmid": vmid,
                    "action": action,
                }
            )

        return {
            "object_type": "ip_address",
            "action": action,
            "proxmox_resource": proxmox_resource,
            "netbox_object": netbox_object,
            "dry_run": False,
            "dependencies_synced": dependencies,
            "error": None,
        }

    except Exception as error:
        return {
            "object_type": "ip_address",
            "action": "error",
            "proxmox_resource": proxmox_resource,
            "netbox_object": None,
            "dry_run": False,
            "dependencies_synced": [],
            "error": str(error),
        }
