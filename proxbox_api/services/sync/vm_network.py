"""VM Interface and Disk synchronization - extracted from sync_vm.py."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from ipaddress import ip_address, ip_interface

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
    NetBoxVirtualDiskSyncState,
    NetBoxVirtualMachineInterfaceSyncState,
    NetBoxVlanSyncState,
)
from proxbox_api.services.sync.network import _resolve_vm_interface_ips
from proxbox_api.services.sync.storage_links import find_storage_record, storage_name_from_volume_id
from proxbox_api.services.sync.vm_filter import get_interface_name_from_config_and_agent
from proxbox_api.services.sync.vm_helpers import (
    normalize_primary_ip_preference,
    normalized_mac,
)

_VM_DISK_AGGREGATE_ERROR_RE = re.compile(
    r"aggregate size of assigned virtual disks \((\d+)\)",
    flags=re.IGNORECASE,
)


def _extract_vm_disk_aggregate_size(error: Exception) -> int | None:
    """Extract the expected VM disk size from NetBox disk aggregate validation errors."""
    detail = getattr(error, "detail", None)
    text = str(detail) if detail else str(error)
    match = _VM_DISK_AGGREGATE_ERROR_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _primary_field_from_ip_address(address: object) -> str | None:
    """Map an IP address value to the appropriate NetBox VM primary field."""
    text = str(address or "").strip()
    if not text:
        return None

    try:
        parsed_ip = ip_interface(text).ip
    except ValueError:
        host = text.split("/", 1)[0]
        try:
            parsed_ip = ip_address(host)
        except ValueError:
            return None

    return "primary_ip4" if parsed_ip.version == 4 else "primary_ip6"


async def sync_vm_interfaces(  # noqa: C901
    nb: NetBoxSessionDep,
    virtual_machine: dict[str, object],
    vm_config: dict[str, object],
    guest_agent_interfaces: list[dict[str, object]],
    network_configs: list[dict[str, object]],
    tag_refs: list[dict[str, object]],
    use_guest_agent_interface_name: bool = True,
    primary_ip_preference: str = "ipv4",
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
        now = datetime.now(timezone.utc)

    primary_ip_preference = normalize_primary_ip_preference(primary_ip_preference)

    vm_id = virtual_machine.get("id")
    if not vm_id:
        raise ProxboxException(message="Virtual machine missing ID")

    netbox_vm_interfaces: list[dict[str, object]] = []
    first_ip_id: int | None = None

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

            interface_id_for_ip = vm_interface.get("id")
            ip_results = await _resolve_vm_interface_ips(
                nb,
                config_dict,
                guest_iface,
                tag_refs,
                interface_id=interface_id_for_ip,
                interface_name=resolved_interface_name,
                now=now,
                create_ip=True,
                primary_ip_preference=primary_ip_preference,
            )
            if ip_results and first_ip_id is None:
                first_ip_id = ip_results[0][0]

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
        now = datetime.now(timezone.utc)

    vm_id = virtual_machine.get("id")
    if not vm_id:
        raise ProxboxException(message="Virtual machine missing ID")

    disk_count = 0

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
) -> tuple[bool, str]:
    """Verify the IP address is assigned to an interface on the given VM.

    If the IP exists but is assigned to the wrong object (or unassigned), this
    function PATCHes the IP to assign it to the VM's first interface so that
    NetBox will accept it as the VM's primary IP.

    Returns (True, reason) if the IP is (or was fixed to be) assigned to the VM,
    (False, reason) otherwise. The reason string describes the outcome for diagnostics.
    """
    try:
        ip_record = await rest_first_async(
            nb, "/api/ipam/ip-addresses/", query={"id": ip_id}
        )
        if not ip_record:
            logger.warning("ensure_ip_assigned_to_vm: IP id=%s not found in NetBox", ip_id)
            return False, "ip_not_found"

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
            return False, "no_interfaces"

        vm_interface_ids = {
            iface.get("id") if isinstance(iface, dict) else getattr(iface, "id", None)
            for iface in ifaces
        }

        if (
            assigned_object_type == "virtualization.vminterface"
            and assigned_object_id in vm_interface_ids
        ):
            return True, "already_assigned"

        # IP is not assigned to this VM — reassign to the first available interface
        first_iface = ifaces[0]
        first_iface_id = (
            first_iface.get("id") if isinstance(first_iface, dict) else getattr(first_iface, "id", None)
        )
        patched = await rest_patch_async(
            nb,
            "/api/ipam/ip-addresses/",
            ip_id,
            {
                "assigned_object_type": "virtualization.vminterface",
                "assigned_object_id": first_iface_id,
            },
        )
        # Verify the PATCH took effect by inspecting the returned record
        patched_obj_id = patched.get("assigned_object_id") if isinstance(patched, dict) else None
        if isinstance(patched_obj_id, dict):
            patched_obj_id = patched_obj_id.get("id")
        patched_obj_type = patched.get("assigned_object_type") if isinstance(patched, dict) else None
        if patched_obj_type == "virtualization.vminterface" and patched_obj_id == first_iface_id:
            logger.info(
                "ensure_ip_assigned_to_vm: reassigned IP id=%s to interface id=%s on VM id=%s",
                ip_id,
                first_iface_id,
                vm_id,
            )
            return True, "reassigned"
        logger.warning(
            "ensure_ip_assigned_to_vm: PATCH did not take effect for IP id=%s "
            "(got type=%s obj_id=%s, expected type=virtualization.vminterface obj_id=%s)",
            ip_id,
            patched_obj_type,
            patched_obj_id,
            first_iface_id,
        )
        return False, f"reassign_failed(type={patched_obj_type},obj_id={patched_obj_id})"
    except Exception as exc:
        logger.warning(
            "ensure_ip_assigned_to_vm: failed for IP id=%s VM id=%s: %s",
            ip_id,
            vm_id,
            exc,
        )
        return False, f"exception: {exc}"


async def set_primary_ip(  # noqa: C901
    nb: NetBoxSessionDep,
    virtual_machine: dict[str, object],
    primary_ip_id: int | None,
    primary_ip_preference: str = "ipv4",
) -> bool:
    """Set the primary IP address for a VM if not already set.

    Chooses ``primary_ip4`` or ``primary_ip6`` based on the IP family.
    """
    vm_id = virtual_machine.get("id")
    if not vm_id or not primary_ip_id:
        return False

    primary_ip_preference = normalize_primary_ip_preference(primary_ip_preference)

    # Preserve existing explicit primary choice.
    if virtual_machine.get("primary_ip4") is not None or virtual_machine.get("primary_ip6") is not None:
        return False

    # Verify (and fix if needed) that the IP is assigned to this VM before setting primary.
    assigned, reason = await ensure_ip_assigned_to_vm(nb, primary_ip_id, vm_id)
    if not assigned:
        logger.info(
            "Primary IP check failed (reason=%s) for VM id=%s; retrying once",
            reason,
            vm_id,
        )
        await asyncio.sleep(1.0)
        assigned, reason = await ensure_ip_assigned_to_vm(nb, primary_ip_id, vm_id)

    if not assigned:
        logger.warning(
            "IP id=%s is not assigned to VM id=%s (reason=%s); skipping primary IP assignment",
            primary_ip_id,
            vm_id,
            reason,
        )
        return False

    ip_record = await rest_first_async(nb, "/api/ipam/ip-addresses/", query={"id": primary_ip_id})
    if not ip_record:
        logger.warning("Primary IP record id=%s not found for VM id=%s", primary_ip_id, vm_id)
        return False

    primary_field = _primary_field_from_ip_address(ip_record.get("address"))
    if primary_field is None:
        logger.warning(
            "Could not determine primary IP family for VM id=%s from IP record id=%s (%s)",
            vm_id,
            primary_ip_id,
            ip_record.get("address"),
        )
        return False

    patch_payload: dict[str, object] = {primary_field: primary_ip_id}

    if (
        (primary_ip_preference == "ipv4" and primary_field == "primary_ip6")
        or (primary_ip_preference == "ipv6" and primary_field == "primary_ip4")
    ):
        logger.debug(
            "Primary IP family mismatch for VM id=%s: preferred=%s, selected_field=%s",
            vm_id,
            primary_ip_preference,
            primary_field,
        )

    try:
        await rest_patch_async(
            nb,
            "/api/virtualization/virtual-machines/",
            vm_id,
            patch_payload,
        )
        return True
    except Exception as exc:
        aggregate_disk = _extract_vm_disk_aggregate_size(exc)
        if aggregate_disk and aggregate_disk > 0:
            try:
                await rest_patch_async(
                    nb,
                    "/api/virtualization/virtual-machines/",
                    vm_id,
                    {"disk": aggregate_disk, **patch_payload},
                )
                logger.info(
                    "Set %s for VM id=%s after reconciling disk=%s to match virtual disks",
                    primary_field,
                    vm_id,
                    aggregate_disk,
                )
                return True
            except Exception as retry_exc:
                logger.warning(
                    "Failed to set %s for VM id=%s after disk reconciliation retry: %s",
                    primary_field,
                    vm_id,
                    retry_exc,
                )
                return False
        logger.warning(
            "Failed to set %s for VM id=%s: %s",
            primary_field,
            vm_id,
            exc,
        )
        return False
