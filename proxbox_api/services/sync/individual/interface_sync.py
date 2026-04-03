"""Individual Interface sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_reconcile_async
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
    build_sync_response,
    ensure_vm_record,
    extract_net_interface_config,
    get_first_record,
    get_serialized_first_record,
    resolve_guest_interface,
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


async def _resolve_vlan_id(
    nb: object,
    tag_refs: list[dict[str, object]],
    vlan_tag_raw: object,
    *,
    now: datetime,
    interface_name: str,
) -> int | None:
    if vlan_tag_raw is None:
        return None
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
        return (
            vlan_record.get("id")
            if isinstance(vlan_record, dict)
            else getattr(vlan_record, "id", None)
        )
    except Exception as vlan_exc:
        from proxbox_api.logger import logger

        logger.warning(
            "Failed to create/sync VLAN tag=%s for interface %s: %s",
            vlan_tag_raw,
            interface_name,
            vlan_exc,
        )
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

    net_config = extract_net_interface_config(vm_config)

    target_config = net_config.get(interface_name, {})
    mac_address = target_config.get("virtio") or target_config.get("hwaddr")
    bridge = target_config.get("bridge")
    vlan_tag_raw = target_config.get("tag")

    guest_iface, resolved_name, mac_address = resolve_guest_interface(
        guest_interfaces,
        interface_name,
        mac_address,
    )

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
        vm_record, _ = await ensure_vm_record(
            nb,
            px,
            tag,
            vmid=vmid,
            node=node,
            vm_type=vm_type,
            auto_create_vm=False,
        )
        vm_id = getattr(vm_record, "id", None) if vm_record is not None else None
        netbox_object = None
        if vm_id:
            netbox_object = await get_serialized_first_record(
                nb,
                "/api/virtualization/interfaces/",
                query={"virtual_machine_id": vm_id, "name": resolved_name},
            )
        return build_sync_response(
            object_type="interface",
            action="dry_run",
            proxmox_resource=proxmox_resource,
            netbox_object=netbox_object,
            dry_run=True,
            dependencies_synced=[
                {
                    "object_type": "vm",
                    "vmid": vmid,
                    "cluster_name": getattr(px, "name", None),
                    "node": node,
                    "type": vm_type,
                }
            ],
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
                object_type="interface",
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
                object_type="interface",
                action="error",
                proxmox_resource=proxmox_resource,
                netbox_object=None,
                dry_run=False,
                dependencies_synced=[],
                error=f"Could not resolve VM ID for vmid={vmid}",
            )

        vlan_nb_id = await _resolve_vlan_id(
            nb,
            tag_refs,
            vlan_tag_raw,
            now=now,
            interface_name=interface_name,
        )

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

        existing_interface = await get_first_record(
            nb,
            "/api/virtualization/interfaces/",
            query={"virtual_machine_id": vm_id, "name": resolved_name},
        )
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
        action = "updated" if existing_interface else "created"

        return build_sync_response(
            object_type="interface",
            action=action,
            proxmox_resource=proxmox_resource,
            netbox_object=netbox_object,
            dry_run=False,
            dependencies_synced=[
                {
                    "object_type": "vm",
                    "vmid": vmid,
                    "cluster_name": getattr(px, "name", None),
                    "node": node,
                    "type": vm_type,
                    "action": action,
                }
            ],
            error=None,
        )

    except Exception as error:
        return build_sync_response(
            object_type="interface",
            action="error",
            proxmox_resource=proxmox_resource,
            netbox_object=None,
            dry_run=False,
            dependencies_synced=[],
            error=str(error),
        )
