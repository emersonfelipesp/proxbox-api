"""VM Interface and Disk synchronization - extracted from sync_vm.py."""

from __future__ import annotations

from datetime import datetime

from proxbox_api.dependencies import NetBoxSessionDep
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    rest_first_async,
    rest_list_async,
    rest_patch_async,
    rest_reconcile_async,
)
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxIpAddressSyncState,
    NetBoxVirtualDiskSyncState,
    NetBoxVirtualMachineInterfaceSyncState,
    NetBoxVlanSyncState,
)
from proxbox_api.services.sync.storage_links import find_storage_record
from proxbox_api.services.sync.vm_helpers import best_guest_agent_ip


async def sync_vm_interfaces(  # noqa: C901
    nb: NetBoxSessionDep,
    virtual_machine: dict[str, object],
    vm_config: dict[str, object],
    guest_agent_interfaces: list[dict[str, object]],
    network_configs: list[dict[str, object]],
    tag_refs: list[dict[str, object]],
    use_guest_agent_interface_name: bool = True,
    now: datetime | None = None,
    device: dict | None = None,
) -> tuple[list[dict[str, object]], int | None]:
    """Synchronize VM interfaces and IP addresses.

    Args:
        nb: NetBox session
        virtual_machine: NetBox VM record dict
        vm_config: Proxmox VM config dict
        guest_agent_interfaces: List of interfaces from guest agent
        network_configs: Parsed network configs
        tag_refs: Tag references for NetBox objects
        use_guest_agent_interface_name: Whether to use guest agent names
        now: Current datetime for timestamps
        device: NetBox node device dict (used to create node-level bridge interfaces)

    Returns:
        Tuple of (created_interfaces, first_ip_id)
    """
    if now is None:
        now = datetime.now()

    vm_id = virtual_machine.get("id")
    if not vm_id:
        raise ProxboxException(message="Virtual machine missing ID")

    netbox_vm_interfaces: list[dict[str, object]] = []
    first_ip_id: int | None = None

    from proxbox_api.services.sync.vm_filter import get_interface_name_from_config_and_agent
    from proxbox_api.services.sync.vm_helpers import normalized_mac

    guest_by_name = {
        str(iface.get("name", "")).strip().lower(): iface for iface in guest_agent_interfaces
    }
    guest_by_mac = {
        normalized_mac(iface.get("mac_address")): iface
        for iface in guest_agent_interfaces
        if normalized_mac(iface.get("mac_address"))
    }

    for network in network_configs:
        for interface_name, config_dict in network.items():
            config_interface_name = (
                str(config_dict.get("name", interface_name)).strip() or interface_name
            )

            # Find matching guest agent interface
            interface_mac = config_dict.get("virtio") or config_dict.get("hwaddr")
            guest_iface = None
            if interface_mac:
                guest_iface = guest_by_mac.get(normalized_mac(interface_mac))
            if guest_iface is None:
                guest_iface = guest_by_name.get(config_interface_name.lower())

            resolved_interface_name = get_interface_name_from_config_and_agent(
                config_interface_name,
                config_dict,
                guest_agent_interfaces,
                use_guest_agent_interface_name,
            )

            # Create node-level dcim bridge and per-VM bridge VMInterface if needed
            bridge_name = config_dict.get("bridge")
            bridge_id: int | None = None
            if bridge_name and vm_id:
                from proxbox_api.services.sync.bridge_interfaces import ensure_bridge_interfaces

                device_id = (
                    (device.get("id") if isinstance(device, dict) else getattr(device, "id", None))
                    if device
                    else None
                )
                bridge_id = await ensure_bridge_interfaces(
                    nb, device_id, int(vm_id), bridge_name, tag_refs, now
                )

            # Resolve VLAN tag from Proxmox config
            vlan_nb_id: int | None = None
            vlan_tag_raw = config_dict.get("tag")
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
                        "Failed to create/sync VLAN tag=%s: %s",
                        vlan_tag_raw,
                        vlan_exc,
                    )

            # Create VM interface
            vm_interface = await rest_reconcile_async(
                nb,
                "/api/virtualization/interfaces/",
                lookup={
                    "virtual_machine_id": vm_id,
                    "name": resolved_interface_name,
                },
                payload={
                    "virtual_machine": vm_id,
                    "name": resolved_interface_name,
                    "enabled": True,
                    "mac_address": config_dict.get("virtio") or config_dict.get("hwaddr"),
                    "bridge": None,
                    "untagged_vlan": vlan_nb_id,
                    "mode": "access" if vlan_nb_id is not None else None,
                    "tags": tag_refs,
                    "custom_fields": {
                        "proxmox_last_updated": now.isoformat(),
                        **({"proxbox_bridge": bridge_id} if bridge_id is not None else {}),
                    },
                },
                schema=NetBoxVirtualMachineInterfaceSyncState,
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "virtual_machine": record.get("virtual_machine"),
                    "enabled": record.get("enabled"),
                    "mac_address": record.get("mac_address"),
                    "type": record.get("type"),
                    "description": record.get("description"),
                    "bridge": record.get("bridge"),
                    "untagged_vlan": record.get("untagged_vlan"),
                    "mode": record.get("mode"),
                    "tags": record.get("tags"),
                    "custom_fields": record.get("custom_fields"),
                },
                nullable_fields={"bridge"},
            )
            if not isinstance(vm_interface, dict):
                vm_interface = vm_interface.dict()

            netbox_vm_interfaces.append(vm_interface)

            # Create IP address if available
            interface_ip = best_guest_agent_ip(guest_iface) or config_dict.get("ip")
            if interface_ip and interface_ip != "dhcp":
                try:
                    ip_record = await rest_reconcile_async(
                        nb,
                        "/api/ipam/ip-addresses/",
                        lookup={"address": interface_ip},
                        payload={
                            "address": interface_ip,
                            "assigned_object_type": "virtualization.vminterface",
                            "assigned_object_id": vm_interface.get("id"),
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
                            "custom_fields": record.get("custom_fields"),
                        },
                    )
                    if first_ip_id is None:
                        first_ip_id = (
                            ip_record.get("id")
                            if isinstance(ip_record, dict)
                            else getattr(ip_record, "id", None)
                        )
                except Exception as ip_exc:
                    logger.warning("Failed to create IP address %s: %s", interface_ip, ip_exc)

    return netbox_vm_interfaces, first_ip_id


async def sync_vm_disks(
    nb: NetBoxSessionDep,
    virtual_machine: dict[str, object],
    disk_entries: list[object],
    storage_index: dict[tuple[str, str], dict[str, object]],
    cluster_name: str,
    tag_refs: list[dict[str, object]],
    now: datetime | None = None,
) -> int:
    """Synchronize VM disks.

    Args:
        nb: NetBox session
        virtual_machine: NetBox VM record dict
        disk_entries: List of disk entries from VM config
        storage_index: Storage index for finding storage records
        cluster_name: Name of cluster for storage lookup
        tag_refs: Tag references for NetBox objects
        now: Current datetime for timestamps

    Returns:
        Number of disks synced
    """
    if now is None:
        now = datetime.now()

    vm_id = virtual_machine.get("id")
    if not vm_id:
        raise ProxboxException(message="Virtual machine missing ID")

    disk_count = 0

    from proxbox_api.services.sync.storage_links import storage_name_from_volume_id

    for disk_entry in disk_entries:
        storage_name = disk_entry.storage_name or storage_name_from_volume_id(disk_entry.storage)
        storage_record = find_storage_record(
            storage_index,
            cluster_name=cluster_name,
            storage_name=storage_name,
        )
        try:
            await rest_reconcile_async(
                nb,
                "/api/virtualization/virtual-disks/",
                lookup={
                    "virtual_machine_id": vm_id,
                    "name": disk_entry.name,
                },
                payload={
                    "virtual_machine": vm_id,
                    "name": disk_entry.name,
                    "size": disk_entry.size,
                    "storage": storage_record.get("id") if storage_record else None,
                    "description": disk_entry.description,
                    "tags": tag_refs,
                    "custom_fields": {"proxmox_last_updated": now.isoformat()},
                },
                schema=NetBoxVirtualDiskSyncState,
                current_normalizer=lambda record: {
                    "virtual_machine": record.get("virtual_machine"),
                    "name": record.get("name"),
                    "size": record.get("size"),
                    "storage": record.get("storage"),
                    "description": record.get("description"),
                    "tags": record.get("tags"),
                    "custom_fields": record.get("custom_fields"),
                },
            )
            disk_count += 1
        except Exception as e:
            logger.warning("Failed to sync disk %s: %s", disk_entry.name, e)

    return disk_count


async def ensure_ip_assigned_to_vm(
    nb: NetBoxSessionDep,
    ip_id: int,
    vm_id: int,
) -> bool:
    """Verify the IP address is assigned to an interface on the given VM.

    If the IP exists but is assigned to the wrong object (or unassigned), this
    function PATCHes the IP to assign it to the VM's first interface so that
    NetBox will accept it as the VM's primary IP.

    Returns True if the IP is (or was fixed to be) assigned to the VM, False otherwise.
    """
    try:
        ip_record = await rest_first_async(
            nb, "/api/ipam/ip-addresses/", query={"id": ip_id}
        )
        if not ip_record:
            logger.warning("ensure_ip_assigned_to_vm: IP id=%s not found in NetBox", ip_id)
            return False

        raw_assigned_id = ip_record.get("assigned_object_id")
        assigned_object_id = (
            raw_assigned_id.get("id") if isinstance(raw_assigned_id, dict) else raw_assigned_id
        )
        assigned_object_type = ip_record.get("assigned_object_type")

        ifaces = await rest_list_async(
            nb,
            "/api/virtualization/interfaces/",
            query={"virtual_machine_id": vm_id, "limit": 50},
        )
        if not ifaces:
            logger.warning(
                "ensure_ip_assigned_to_vm: VM id=%s has no interfaces; cannot assign IP id=%s",
                vm_id,
                ip_id,
            )
            return False

        vm_interface_ids = {
            iface.get("id") if isinstance(iface, dict) else getattr(iface, "id", None)
            for iface in ifaces
        }

        if (
            assigned_object_type == "virtualization.vminterface"
            and assigned_object_id in vm_interface_ids
        ):
            return True

        # IP is not assigned to this VM — reassign to the first available interface
        first_iface = ifaces[0]
        first_iface_id = (
            first_iface.get("id") if isinstance(first_iface, dict) else getattr(first_iface, "id", None)
        )
        await rest_patch_async(
            nb,
            "/api/ipam/ip-addresses/",
            ip_id,
            {
                "assigned_object_type": "virtualization.vminterface",
                "assigned_object_id": first_iface_id,
            },
        )
        logger.info(
            "ensure_ip_assigned_to_vm: reassigned IP id=%s to interface id=%s on VM id=%s",
            ip_id,
            first_iface_id,
            vm_id,
        )
        return True
    except Exception as exc:
        logger.warning(
            "ensure_ip_assigned_to_vm: failed for IP id=%s VM id=%s: %s",
            ip_id,
            vm_id,
            exc,
        )
        return False


async def set_primary_ip(
    nb: NetBoxSessionDep,
    virtual_machine: dict[str, object],
    primary_ip_id: int | None,
) -> bool:
    """Set the primary IPv4 address for a VM if not already set.

    Args:
        nb: NetBox session
        virtual_machine: NetBox VM record dict
        primary_ip_id: ID of IP address to set as primary

    Returns:
        True if successful or no-op, False if skipped
    """
    vm_id = virtual_machine.get("id")
    if not vm_id or not primary_ip_id:
        return False

    # Skip if primary IP already set
    if virtual_machine.get("primary_ip4") is not None:
        return False

    # Verify (and fix if needed) that the IP is assigned to this VM before setting primary
    assigned = await ensure_ip_assigned_to_vm(nb, primary_ip_id, vm_id)
    if not assigned:
        logger.warning(
            "IP id=%s is not assigned to VM id=%s; skipping primary_ip4 assignment",
            primary_ip_id,
            vm_id,
        )
        return False

    try:
        await rest_patch_async(
            nb,
            "/api/virtualization/virtual-machines/",
            vm_id,
            {"primary_ip4": primary_ip_id},
        )
        return True
    except Exception as exc:
        logger.warning(
            "Failed to set primary_ip4 for VM id=%s: %s",
            vm_id,
            exc,
        )
        return False
