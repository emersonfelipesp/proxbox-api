"""Individual IP Address sync service."""

from __future__ import annotations

from datetime import datetime, timezone

from proxbox_api.netbox_rest import rest_first_async, rest_list_async, rest_reconcile_async
from proxbox_api.proxmox_to_netbox.models import NetBoxIpAddressSyncState
from proxbox_api.services.proxmox_helpers import (
    get_qemu_guest_agent_hostname,
    get_qemu_guest_agent_network_interfaces,
    get_vm_config,
    get_vm_config_individual,
    sanitize_dns_hostname,
)
from proxbox_api.services.sync.individual.base import BaseIndividualSyncService
from proxbox_api.services.sync.individual.helpers import (
    build_ip_lookup_key,
    build_sync_response,
    ensure_vm_record,
    extract_net_interface_config,
    get_serialized_first_record,
    normalize_mac,
    resolve_guest_interface_by_ip,
)
from proxbox_api.services.sync.ip_ownership import _reconcile_interface_ip
from proxbox_api.services.sync.vm_helpers import (
    build_guest_mac_index,
    merged_guest_iface_from_mac_index,
    record_id,
)
from proxbox_api.utils.async_compat import maybe_await


def _interface_config_mac(config: dict[str, str]) -> str:
    return normalize_mac(config.get("virtio") or config.get("hwaddr"))


def _guest_iface_has_ip(guest_iface: dict[str, object], ip_address: str) -> bool:
    ip_address_clean = ip_address.split("/")[0] if "/" in ip_address else ip_address
    for addr in guest_iface.get("ip_addresses") or []:
        if not isinstance(addr, dict):
            continue
        addr_ip = str(addr.get("ip_address") or "").strip()
        addr_ip_clean = addr_ip.split("/")[0] if "/" in addr_ip else addr_ip
        if addr_ip_clean == ip_address_clean:
            return True
    return False


async def _fetch_qemu_guest_interfaces(
    px: object,
    node: str,
    vmid: int,
) -> list[dict[str, object]]:
    try:
        result = get_qemu_guest_agent_network_interfaces(px, node, vmid)
        guest_interfaces = await maybe_await(result)
    except Exception:
        return []
    if not isinstance(guest_interfaces, list):
        return []
    return [iface for iface in guest_interfaces if isinstance(iface, dict)]


async def _fetch_vm_net_config(
    px: object,
    node: str,
    vm_type: str,
    vmid: int,
) -> dict[str, dict[str, str]]:
    try:
        vm_config = await maybe_await(get_vm_config_individual(px, node, vm_type, vmid))
    except Exception:
        return {}
    if not isinstance(vm_config, dict):
        return {}
    return extract_net_interface_config(vm_config)


def _config_interface_name_for_guest_ip(
    guest_interfaces: list[dict[str, object]],
    net_config: dict[str, dict[str, str]],
    ip_address: str,
) -> str | None:
    """Map a guest IP owner back to its Proxmox config NIC name."""
    guest_by_mac = build_guest_mac_index(guest_interfaces)
    for interface_name, config in net_config.items():
        config_mac = _interface_config_mac(config)
        if not config_mac:
            continue
        merged_guest_iface = merged_guest_iface_from_mac_index(guest_by_mac, config_mac)
        if merged_guest_iface is not None and _guest_iface_has_ip(
            merged_guest_iface,
            ip_address,
        ):
            return interface_name
    return None


def _config_interface_name_for_guest_name(
    guest_interfaces: list[dict[str, object]],
    net_config: dict[str, dict[str, str]],
    guest_interface_name: str,
    ip_address: str,
) -> str | None:
    """Map an explicit guest interface name to its config NIC when the MAC matches."""
    guest_iface = next(
        (
            iface
            for iface in guest_interfaces
            if str(iface.get("name") or "").strip().lower() == guest_interface_name.lower()
        ),
        None,
    )
    if guest_iface is None:
        return None
    guest_mac = normalize_mac(guest_iface.get("mac_address"))
    if not guest_mac:
        return None
    guest_by_mac = build_guest_mac_index(guest_interfaces)
    for interface_name, config in net_config.items():
        config_mac = _interface_config_mac(config)
        if config_mac != guest_mac:
            continue
        merged_guest_iface = merged_guest_iface_from_mac_index(guest_by_mac, config_mac)
        if merged_guest_iface is None or _guest_iface_has_ip(merged_guest_iface, ip_address):
            return interface_name
    return None


def _resolve_core_interface_name(
    *,
    requested_interface_name: str | None,
    guest_interfaces: list[dict[str, object]],
    net_config: dict[str, dict[str, str]],
    ip_address: str,
) -> str | None:
    if requested_interface_name:
        if requested_interface_name in net_config:
            return requested_interface_name
        return (
            _config_interface_name_for_guest_name(
                guest_interfaces,
                net_config,
                requested_interface_name,
                ip_address,
            )
            or requested_interface_name
        )
    return _config_interface_name_for_guest_ip(
        guest_interfaces,
        net_config,
        ip_address,
    ) or resolve_guest_interface_by_ip(guest_interfaces, ip_address)


async def _resolve_dns_name(px: object, node: str, vm_type: str, vmid: int) -> str | None:
    if vm_type == "lxc":
        try:
            lxc_config = await maybe_await(get_vm_config(px, node, "lxc", vmid))
            config_dict = lxc_config.model_dump(by_alias=True, exclude_none=True)
            return sanitize_dns_hostname(config_dict.get("hostname"))
        except Exception:
            return None
    if vm_type == "qemu":
        try:
            hostname = await maybe_await(get_qemu_guest_agent_hostname(px, node, vmid))
            return hostname if isinstance(hostname, str) else None
        except Exception:
            return None
    return None


async def _resolve_interface_id(
    nb: object,
    px: object,
    tag: object,
    *,
    node: str,
    vm_type: str,
    netbox_vm_id: int,
    proxmox_vmid: int,
    resolved_interface: str | None,
    auto_create_interface: bool,
) -> int | None:
    if not resolved_interface:
        return None

    existing_ifaces = await rest_list_async(
        nb,
        "/api/virtualization/interfaces/",
        query={"virtual_machine_id": netbox_vm_id, "name": resolved_interface},
    )
    if existing_ifaces:
        return record_id(existing_ifaces[0])

    if not auto_create_interface:
        return None

    from proxbox_api.services.sync.individual.interface_sync import sync_interface_individual

    iface_result = await sync_interface_individual(
        nb,
        px,
        tag,
        node,
        vm_type,
        proxmox_vmid,
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

    guest_interfaces: list[dict[str, object]] = []
    net_config: dict[str, dict[str, str]] = {}
    if vm_type == "qemu":
        guest_interfaces = await _fetch_qemu_guest_interfaces(px, node, vmid)
        net_config = await _fetch_vm_net_config(px, node, vm_type, vmid)

    resolved_interface = _resolve_core_interface_name(
        requested_interface_name=interface_name,
        guest_interfaces=guest_interfaces,
        net_config=net_config,
        ip_address=ip_address,
    )

    dns_name = await maybe_await(_resolve_dns_name(px, node, vm_type, vmid))

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

        vm_id = record_id(vm_record)
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
            netbox_vm_id=vm_id,
            proxmox_vmid=vmid,
            resolved_interface=resolved_interface,
            auto_create_interface=auto_create_interface,
        )

        existing_ips = await rest_list_async(
            nb,
            "/api/ipam/ip-addresses/",
            query={"address": ip_address.split("/")[0]},
        )
        if interface_id:
            ip_id = await _reconcile_interface_ip(
                nb,
                ip_addr=ip_address,
                interface_id=interface_id,
                tag_refs=tag_refs,
                now=now,
                dns_name=dns_name,
                interface_name=resolved_interface or "",
            )
            ip_record = (
                await rest_first_async(
                    nb,
                    "/api/ipam/ip-addresses/",
                    query={"id": ip_id, "limit": 1},
                )
                if ip_id is not None
                else None
            )
        else:
            ip_payload: dict[str, object] = {
                "address": ip_address,
                "status": "active",
                "dns_name": dns_name or "",
                "tags": tag_refs,
                "custom_fields": {"proxmox_last_updated": now.isoformat()},
            }
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
                    "dns_name": record.get("dns_name"),
                    "tags": record.get("tags"),
                },
            )

        netbox_object = (
            ip_record.serialize()
            if hasattr(ip_record, "serialize")
            else dict(ip_record)
            if isinstance(ip_record, dict)
            else None
        )
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
