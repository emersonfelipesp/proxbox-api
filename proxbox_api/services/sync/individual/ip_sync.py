"""Individual IP Address sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxIpAddressSyncState
from proxbox_api.services.proxmox_helpers import get_qemu_guest_agent_network_interfaces
from proxbox_api.services.sync.individual.base import BaseIndividualSyncService
from proxbox_api.services.sync.individual.helpers import (
    build_ip_lookup_key,
    build_sync_response,
    ensure_vm_record,
    get_serialized_first_record,
    resolve_guest_interface_by_ip,
)


async def _resolve_interface_id(
    nb: object,
    px: object,
    tag: object,
    *,
    node: str,
    vm_type: str,
    vmid: int,
    resolved_interface: str | None,
    auto_create_interface: bool,
) -> int | None:
    if not resolved_interface:
        return None

    existing_ifaces = await rest_list_async(
        nb,
        "/api/virtualization/interfaces/",
        query={"virtual_machine_id": vmid, "name": resolved_interface},
    )
    if existing_ifaces:
        return getattr(existing_ifaces[0], "id", None)

    if not auto_create_interface:
        return None

    from proxbox_api.services.sync.individual.interface_sync import sync_interface_individual

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
    netbox_object = iface_result.get("netbox_object")
    if isinstance(netbox_object, dict):
        return netbox_object.get("id")
    return None


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

    resolved_interface = interface_name or (
        resolve_guest_interface_by_ip(guest_interfaces, ip_address) if guest_interfaces else None
    )

    proxmox_resource: dict[str, object] = {
        "vmid": vmid,
        "node": node,
        "type": vm_type,
        "ip_address": ip_address,
        "interface_name": resolved_interface,
        "proxmox_last_updated": now.isoformat(),
    }

    if dry_run:
        netbox_object = await get_serialized_first_record(
            nb,
            "/api/ipam/ip-addresses/",
            query={"address": ip_address.split("/")[0]},
        )
        dependencies = [
            {
                "object_type": "vm",
                "vmid": vmid,
                "cluster_name": getattr(px, "name", None),
                "node": node,
                "type": vm_type,
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
                }
            )

        return build_sync_response(
            object_type="ip_address",
            action="dry_run",
            proxmox_resource=proxmox_resource,
            netbox_object=netbox_object,
            dry_run=True,
            dependencies_synced=dependencies,
            error=None,
        )

    try:
        vm_record, vm_error = await ensure_vm_record(
            nb,
            px,
            tag,
            vmid=vmid,
            node=node,
            vm_type=vm_type,
            auto_create_vm=auto_create_vm,
        )
        if vm_error:
            return build_sync_response(
                object_type="ip_address",
                action="error",
                proxmox_resource=proxmox_resource,
                netbox_object=None,
                dry_run=False,
                dependencies_synced=[],
                error=vm_error,
            )

        vm_id = getattr(vm_record, "id", None)
        if vm_id is None:
            return build_sync_response(
                object_type="ip_address",
                action="error",
                proxmox_resource=proxmox_resource,
                netbox_object=None,
                dry_run=False,
                dependencies_synced=[],
                error=f"Could not resolve VM ID for vmid={vmid}",
            )

        interface_id = await _resolve_interface_id(
            nb,
            px,
            tag,
            node=node,
            vm_type=vm_type,
            vmid=vmid,
            resolved_interface=resolved_interface,
            auto_create_interface=auto_create_interface,
        )

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

        return build_sync_response(
            object_type="ip_address",
            action=action,
            proxmox_resource=proxmox_resource,
            netbox_object=netbox_object,
            dry_run=False,
            dependencies_synced=dependencies,
            error=None,
        )

    except Exception as error:
        return build_sync_response(
            object_type="ip_address",
            action="error",
            proxmox_resource=proxmox_resource,
            netbox_object=None,
            dry_run=False,
            dependencies_synced=[],
            error=str(error),
        )
