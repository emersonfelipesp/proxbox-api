"""VM and node interface + IP synchronization helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from ipaddress import ip_interface as _ip_interface

from proxbox_api.enum.status_mapping import NetBoxInterfaceType
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    rest_bulk_delete_async,
    rest_bulk_reconcile_async,
    rest_first_async,
    rest_list_async,
    rest_reconcile_async,
)
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxInterfaceSyncState,
    NetBoxIpAddressSyncState,
    NetBoxVirtualMachineInterfaceSyncState,
    NetBoxVlanSyncState,
)
from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.sync.vm_helpers import (
    _is_skippable_ip,
    all_guest_agent_ips,
    normalized_mac,
    preferred_primary_ip_order,
)


async def sync_node_interface_and_ip(
    nb,
    device: dict,
    interface_name: str,
    interface_config: dict,
    tag_refs: list[dict],
) -> dict:
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
            "type": NetBoxInterfaceType.from_proxmox(iface_type),
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


def _resolve_vm_interface_identity(
    interface_name: str,
    interface_config: dict,
    guest_iface: dict | None,
    use_guest_agent_interface_name: bool,
) -> tuple[str, str | None]:
    """Resolve the display name and MAC address for a VM interface."""
    mac_address = interface_config.get("virtio") or interface_config.get("hwaddr")
    resolved_name = interface_name
    if use_guest_agent_interface_name and guest_iface:
        guest_name = str(guest_iface.get("name") or "").strip()
        if guest_name:
            resolved_name = guest_name
            guest_mac = guest_iface.get("mac_address")
            if guest_mac and not mac_address:
                mac_address = normalized_mac(guest_mac)
    return resolved_name, mac_address


def build_vlan_payload(
    vlan_tag: int,
    tag_refs: list[dict],
    now: datetime,
) -> dict:
    """Build a VLAN payload dict for bulk operations (no NetBox writes).

    Args:
        vlan_tag: VLAN ID (vid)
        tag_refs: List of tag references
        now: Current datetime for custom fields

    Returns:
        Payload dict for bulk reconciliation
    """
    return {
        "vid": vlan_tag,
        "name": f"VLAN {vlan_tag}",
        "status": "active",
        "tags": tag_refs,
        "custom_fields": {"proxmox_last_updated": now.isoformat()},
    }


def build_vm_interface_payload(
    resolved_name: str,
    mac_address: str | None,
    bridge_id: int | None,
    vlan_id: int | None,
    tag_refs: list[dict],
    vm_id: int,
    now: datetime,
) -> dict:
    """Build a VM interface payload dict for bulk operations (no NetBox writes).

    Args:
        resolved_name: Interface name
        mac_address: MAC address
        bridge_id: Node dcim.Interface ID for the bridge (stored as proxbox_bridge custom field)
        vlan_id: VLAN ID (if applicable)
        tag_refs: List of tag references
        vm_id: Virtual machine ID
        now: Current datetime for custom fields

    Returns:
        Payload dict for bulk reconciliation
    """
    payload: dict = {
        "name": resolved_name,
        "enabled": True,
        "mac_address": mac_address,
        "untagged_vlan": vlan_id,
        "mode": "access" if vlan_id is not None else None,
        "tags": tag_refs,
        "custom_fields": {
            "proxmox_last_updated": now.isoformat(),
            **({"proxbox_bridge": bridge_id} if bridge_id is not None else {}),
        },
    }
    if vm_id is not None:
        payload["virtual_machine"] = vm_id
    return payload


def build_vm_interface_ip_payload(
    address: str,
    interface_id: int,
    tag_refs: list[dict],
    now: datetime,
    dns_name: str | None = None,
    ignore_ipv6_link_local: bool = True,
) -> dict | None:
    """Build a VM interface IP payload dict for bulk operations (no NetBox writes).

    Strips the IPv6 zone-ID suffix (``%eth0``) from ``address`` and returns
    ``None`` when the address is empty, unparseable, loopback, or — when the
    toggle is on — IPv6 link-local. Defends the bulk-reconcile path against
    raw config-fallback IPs that bypass ``all_guest_agent_ips``.

    Args:
        address: IP address with optional CIDR (e.g., ``"192.168.1.10/24"``)
        interface_id: Interface ID
        tag_refs: List of tag references
        now: Current datetime for custom fields
        dns_name: Guest hostname to set as IPAM dns_name; empty/None becomes ""
        ignore_ipv6_link_local: When True (default), skip ``fe80::/10`` hosts

    Returns:
        Payload dict for bulk reconciliation, or ``None`` if the address
        should be skipped.
    """
    host, _, prefix_part = str(address or "").partition("/")
    skip, cleaned = _is_skippable_ip(host, ignore_ipv6_link_local=ignore_ipv6_link_local)
    if skip or cleaned is None:
        return None
    cleaned_address = f"{cleaned}/{prefix_part}" if prefix_part else cleaned
    return {
        "address": cleaned_address,
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": interface_id,
        "status": "active",
        "dns_name": dns_name or "",
        "tags": tag_refs,
        "custom_fields": {"proxmox_last_updated": now.isoformat()},
    }


async def _resolve_vm_interface_vlan(
    nb,
    tag_refs: list[dict],
    interface_config: dict,
    *,
    now: datetime,
    interface_name: str,
) -> int | None:
    """Create or update the VLAN referenced by a VM interface."""
    vlan_tag_raw = interface_config.get("tag")
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
        logger.warning(
            "Failed to create/sync VLAN tag=%s for VM interface %s: %s",
            vlan_tag_raw,
            interface_name,
            vlan_exc,
        )
        return None


async def _reconcile_vm_interface_record(
    nb,
    virtual_machine: dict,
    interface_name: str,
    interface_config: dict,
    guest_iface: dict | None,
    tag_refs: list[dict],
    use_guest_agent_interface_name: bool,
    now: datetime,
    device: dict | None = None,
) -> tuple[dict[str, object], int | None, str | None]:
    """Create or update the VM interface record."""
    from proxbox_api.services.sync.bridge_interfaces import ensure_bridge_interfaces

    vm_id = virtual_machine.get("id")
    bridge_id: int | None = None
    bridge_name = interface_config.get("bridge")
    if bridge_name and vm_id is not None:
        device_id = (
            (device.get("id") if isinstance(device, dict) else getattr(device, "id", None))
            if device
            else None
        )
        bridge_id = await ensure_bridge_interfaces(
            nb, device_id, int(vm_id), bridge_name, tag_refs, now
        )

    vlan_nb_id = await _resolve_vm_interface_vlan(
        nb,
        tag_refs,
        interface_config,
        now=now,
        interface_name=interface_name,
    )

    resolved_name, mac_address = _resolve_vm_interface_identity(
        interface_name,
        interface_config,
        guest_iface,
        use_guest_agent_interface_name,
    )

    payload: dict = {
        "name": resolved_name,
        "enabled": True,
        "mac_address": mac_address,
        "bridge": None,
        "untagged_vlan": vlan_nb_id,
        "mode": "access" if vlan_nb_id is not None else None,
        "tags": tag_refs,
        "custom_fields": {
            "proxmox_last_updated": now.isoformat(),
            **({"proxbox_bridge": bridge_id} if bridge_id is not None else {}),
        },
    }
    if vm_id is not None:
        payload["virtual_machine"] = vm_id

    lookup: dict = {"name": resolved_name}
    if vm_id is not None:
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
        vm_interface = getattr(vm_interface, "dict", lambda: {})()

    interface_id = (
        vm_interface.get("id")
        if isinstance(vm_interface, dict)
        else getattr(vm_interface, "id", None)
    )
    return vm_interface, interface_id, resolved_name


async def bulk_reconcile_vlans(
    nb,
    vlan_payloads: list[dict],
) -> dict[int, int]:
    """Perform bulk reconciliation of VLAN payloads. Returns mapping of vid → NetBox ID.

    Args:
        nb: NetBox session
        vlan_payloads: List of VLAN payload dicts

    Returns:
        Dict mapping VLAN vid to NetBox ID
    """
    if not vlan_payloads:
        return {}

    vlan_vid_to_id = {}
    try:
        result = await rest_bulk_reconcile_async(
            nb,
            "/api/ipam/vlans/",
            payloads=vlan_payloads,
            lookup_fields=["vid"],
            schema=NetBoxVlanSyncState,
            current_normalizer=lambda record: {
                "vid": record.get("vid"),
                "name": record.get("name"),
                "status": record.get("status"),
                "tags": record.get("tags"),
                "custom_fields": record.get("custom_fields"),
            },
        )
        # Build mapping of vid → ID from returned records
        for record in result.records:
            vid = record.get("vid")
            if vid:
                vlan_vid_to_id[vid] = record.get("id")
    except Exception as e:
        logger.error("Error during bulk VLAN reconciliation: %s", e)
    return vlan_vid_to_id


async def bulk_reconcile_vm_interfaces(
    nb,
    interface_payloads: list[dict],
    overwrite_flags: SyncOverwriteFlags | None = None,
) -> tuple[list, dict[tuple, int]]:
    """Perform bulk reconciliation of VM interface payloads.

    Returns:
        (created_interfaces_list, name_vm_to_id_mapping)
    """
    if not interface_payloads:
        return [], {}

    # VM interface scalar identity/state fields are always patchable; tags and
    # custom_fields follow per-resource overwrite_vm_interface_* flags. When
    # overwrite_flags is None, all normalizer keys are patchable, preserving
    # the historical always-overwrite behavior.
    _vm_interface_patchable: set[str] = {
        "name",
        "virtual_machine",
        "enabled",
        "mac_address",
        "type",
        "description",
        "untagged_vlan",
        "mode",
    }
    if overwrite_flags is None or overwrite_flags.overwrite_vm_interface_tags:
        _vm_interface_patchable.add("tags")
    if overwrite_flags is None or overwrite_flags.overwrite_vm_interface_custom_fields:
        _vm_interface_patchable.add("custom_fields")

    interface_name_vm_to_id = {}
    result = None
    try:
        result = await rest_bulk_reconcile_async(
            nb,
            "/api/virtualization/interfaces/",
            payloads=interface_payloads,
            lookup_fields=["name", "virtual_machine"],
            schema=NetBoxVirtualMachineInterfaceSyncState,
            patchable_fields=frozenset(_vm_interface_patchable),
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "virtual_machine": record.get("virtual_machine"),
                "enabled": record.get("enabled"),
                "mac_address": record.get("mac_address"),
                "type": record.get("type"),
                "description": record.get("description"),
                "untagged_vlan": record.get("untagged_vlan"),
                "mode": record.get("mode"),
                "tags": record.get("tags"),
                "custom_fields": record.get("custom_fields"),
            },
        )
        # Build mapping (name, vm_id) → interface_id
        for record in result.records:
            name = record.get("name")
            vm_obj = record.get("virtual_machine")
            vm_id = vm_obj.get("id") if isinstance(vm_obj, dict) else vm_obj
            iface_id = record.get("id")
            if name and vm_id and iface_id:
                interface_name_vm_to_id[(name, vm_id)] = iface_id
    except Exception as e:
        logger.error("Error during bulk VM interface reconciliation: %s", e)
    return result.records if result and hasattr(result, "records") else [], interface_name_vm_to_id


async def bulk_reconcile_vm_interface_ips(
    nb,
    ip_payloads: list[dict],
    overwrite_flags: SyncOverwriteFlags | None = None,
) -> list:
    """Perform bulk reconciliation of VM interface IP payloads.

    Returns:
        List of created/updated IP records
    """
    if not ip_payloads:
        return []

    # Never patch assignment fields on existing IPs.  NetBox rejects
    # reassignment when the IP is the primary IP of the parent object
    # ("Cannot reassign IP address while it is designated as the primary
    # IP for the parent object"). Assignment is established at create
    # time; status/tags/custom_fields are safe to update, gated by the
    # per-field overwrite_ip_* flags.
    if overwrite_flags is None:
        patchable_fields: frozenset[str] = frozenset(
            {"status", "tags", "custom_fields", "dns_name"}
        )
    else:
        gated: set[str] = set()
        if overwrite_flags.overwrite_ip_status:
            gated.add("status")
        if overwrite_flags.overwrite_ip_tags:
            gated.add("tags")
        if overwrite_flags.overwrite_ip_custom_fields:
            gated.add("custom_fields")
        if overwrite_flags.overwrite_ip_address_dns_name:
            gated.add("dns_name")
        patchable_fields = frozenset(gated)

    result = None
    try:
        result = await rest_bulk_reconcile_async(
            nb,
            "/api/ipam/ip-addresses/",
            payloads=ip_payloads,
            lookup_fields=["address"],
            schema=NetBoxIpAddressSyncState,
            current_normalizer=lambda record: {
                "address": record.get("address"),
                "assigned_object_type": record.get("assigned_object_type"),
                "assigned_object_id": record.get("assigned_object_id"),
                "status": record.get("status"),
                "dns_name": record.get("dns_name"),
                "tags": record.get("tags"),
            },
            patchable_fields=patchable_fields,
        )
        return result.records if result and hasattr(result, "records") else []
    except Exception as e:
        logger.error("Error during bulk VM interface IP reconciliation: %s", e)
        return []


async def cleanup_stale_ips_for_interface(
    nb,
    interface_id: int,
    current_ips: set[str],
    tag_slug: str = "proxbox",
) -> int:
    """Delete Proxbox-managed IPs assigned to an interface that are no longer current.

    Args:
        nb: NetBox session
        interface_id: The VM interface ID in NetBox
        current_ips: Set of IP addresses (CIDR notation) that SHOULD exist
        tag_slug: Only delete IPs with this tag (safety guard against deleting manually-added IPs)

    Returns:
        Number of stale IPs deleted
    """
    existing_ips = await rest_list_async(
        nb,
        "/api/ipam/ip-addresses/",
        query={
            "vminterface_id": interface_id,
            "tag": tag_slug,
            "limit": 500,
        },
    )
    if not existing_ips:
        return 0

    # Normalize current IPs for comparison (NetBox normalizes CIDR notation)
    normalized_current: set[str] = set()
    for ip in current_ips:
        try:
            normalized_current.add(str(_ip_interface(ip)))
        except ValueError:
            normalized_current.add(ip)

    stale_ids: list[int] = []
    for ip_record in existing_ips:
        address = (
            ip_record.get("address")
            if isinstance(ip_record, dict)
            else getattr(ip_record, "address", None)
        )
        record_id = (
            ip_record.get("id") if isinstance(ip_record, dict) else getattr(ip_record, "id", None)
        )
        if record_id is None:
            continue
        # Normalize the stored address for comparison
        try:
            normalized_address = str(_ip_interface(str(address or "")))
        except ValueError:
            normalized_address = str(address or "")
        if normalized_address not in normalized_current:
            stale_ids.append(int(record_id))

    if not stale_ids:
        return 0

    logger.info(
        "Cleaning up %d stale IPs for interface id=%s (keeping %d current IPs)",
        len(stale_ids),
        interface_id,
        len(normalized_current),
    )
    try:
        deleted = await rest_bulk_delete_async(nb, "/api/ipam/ip-addresses/", stale_ids)
        return deleted
    except Exception as exc:
        logger.warning("Failed to bulk-delete stale IPs for interface id=%s: %s", interface_id, exc)
        return 0


async def _resolve_vm_interface_ips(  # noqa: C901
    nb,
    interface_config: dict,
    guest_iface: dict | None,
    tag_refs: list[dict],
    *,
    interface_id: int | None,
    interface_name: str,
    now: datetime,
    create_ip: bool,
    ignore_ipv6_link_local: bool = True,
    primary_ip_preference: str = "ipv4",
    tag_slug: str = "proxbox",
    dns_name: str | None = None,
    bridge: object | None = None,
    vm_name: str | None = None,
) -> list[tuple[int | None, str]]:
    """Create or update ALL IPs attached to a VM interface, then clean up stale ones.

    Returns list of (ip_id, ip_address) tuples for all synced IPs.

    When ``bridge`` is provided and any guest-agent IPs were dropped by
    ``_is_skippable_ip`` (link-local under the toggle, loopback, or
    unparseable after zone-ID stripping), emits a single aggregated
    ``phase_summary`` SSE frame for this interface.
    """
    if not create_ip or interface_id is None:
        return []

    raw_guest_ip_count = 0
    if isinstance(guest_iface, dict):
        raw_guest_ip_count = sum(
            1 for addr in (guest_iface.get("ip_addresses") or []) if isinstance(addr, dict)
        )

    all_ips: list[str] = []
    if guest_iface:
        all_ips = all_guest_agent_ips(
            guest_iface,
            ignore_ipv6_link_local,
            primary_ip_preference=primary_ip_preference,
        )

    skipped_guest_ips = max(0, raw_guest_ip_count - len(all_ips))
    if skipped_guest_ips and bridge is not None and hasattr(bridge, "emit_phase_summary"):
        target = f"{vm_name}.{interface_name}" if vm_name else interface_name
        try:
            await bridge.emit_phase_summary(
                phase="vm-ip-addresses",
                skipped=skipped_guest_ips,
                message=(
                    f"Skipped {skipped_guest_ips} link-local/zone-scoped/loopback IPs on {target}"
                ),
            )
        except Exception as emit_exc:
            logger.debug(
                "emit_phase_summary failed for interface %s: %s",
                interface_name,
                emit_exc,
            )

    if not all_ips:
        config_ip = interface_config.get("ip")
        if config_ip and config_ip != "dhcp":
            all_ips = [str(config_ip)]

    all_ips = preferred_primary_ip_order(
        all_ips,
        primary_ip_preference=primary_ip_preference,
    )

    if not all_ips:
        return []

    results: list[tuple[int | None, str]] = []
    for ip_addr in all_ips:
        if ip_addr == "dhcp":
            continue
        host, _, prefix_part = str(ip_addr).partition("/")
        skip, cleaned_host = _is_skippable_ip(host, ignore_ipv6_link_local=ignore_ipv6_link_local)
        if skip or cleaned_host is None:
            continue
        ip_addr = f"{cleaned_host}/{prefix_part}" if prefix_part else cleaned_host
        try:
            ip_record = await rest_reconcile_async(
                nb,
                "/api/ipam/ip-addresses/",
                lookup={"address": ip_addr},
                payload={
                    "address": ip_addr,
                    "assigned_object_type": "virtualization.vminterface",
                    "assigned_object_id": interface_id,
                    "status": "active",
                    "dns_name": dns_name or "",
                    "tags": tag_refs,
                    "custom_fields": {"proxmox_last_updated": now.isoformat()},
                },
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
            ip_id = (
                ip_record.get("id")
                if isinstance(ip_record, dict)
                else getattr(ip_record, "id", None)
            )
            results.append((ip_id, ip_addr))
        except Exception as ip_exc:
            logger.warning(
                "Failed to create IP %s for VM interface %s: %s",
                ip_addr,
                interface_name,
                ip_exc,
            )

    if results:
        current_ip_set = {ip_addr for _, ip_addr in results}
        try:
            await cleanup_stale_ips_for_interface(
                nb, interface_id, current_ip_set, tag_slug=tag_slug
            )
        except Exception as cleanup_exc:
            logger.warning(
                "Failed to cleanup stale IPs for interface %s: %s",
                interface_name,
                cleanup_exc,
            )

    return results


async def sync_vm_interface_and_ip(
    nb,
    virtual_machine: dict,
    interface_name: str,
    interface_config: dict,
    guest_iface: dict | None,
    tag_refs: list[dict],
    use_guest_agent_interface_name: bool = True,
    create_interface: bool = True,
    create_ip: bool = True,
    ignore_ipv6_link_local_addresses: bool = True,
    primary_ip_preference: str = "ipv4",
    now: datetime | None = None,
    device: dict | None = None,
    dns_name: str | None = None,
) -> dict:
    if now is None:
        now = datetime.now(timezone.utc)

    vm_id = virtual_machine.get("id")
    if create_interface:
        vm_interface, interface_id, resolved_name = await _reconcile_vm_interface_record(
            nb,
            virtual_machine,
            interface_name,
            interface_config,
            guest_iface,
            tag_refs,
            use_guest_agent_interface_name,
            now,
            device=device,
        )
    else:
        vm_interface = await rest_first_async(
            nb,
            "/api/virtualization/interfaces/",
            query={
                "name": interface_name,
                **({"virtual_machine_id": vm_id} if vm_id is not None else {}),
                "limit": 2,
            },
        )
        if not vm_interface:
            logger.warning(
                "Skipping VM IP sync for %s: interface %s not found on VM %s",
                interface_name,
                interface_name,
                vm_id,
            )
            return {
                "id": None,
                "mac_address": interface_config.get("virtio") or interface_config.get("hwaddr"),
            }
        if not isinstance(vm_interface, dict):
            vm_interface = getattr(vm_interface, "dict", lambda: {})()
        interface_id = (
            vm_interface.get("id")
            if isinstance(vm_interface, dict)
            else getattr(vm_interface, "id", None)
        )

    result: dict = {
        "id": interface_id,
        "mac_address": interface_config.get("virtio") or interface_config.get("hwaddr"),
        "interface": vm_interface,
    }

    ip_results = await _resolve_vm_interface_ips(
        nb,
        interface_config,
        guest_iface,
        tag_refs,
        interface_id=interface_id,
        interface_name=interface_name,
        now=now,
        create_ip=create_ip,
        ignore_ipv6_link_local=ignore_ipv6_link_local_addresses,
        primary_ip_preference=primary_ip_preference,
        dns_name=dns_name,
    )
    if ip_results:
        first_ip_id, first_ip = ip_results[0]
        if first_ip_id is not None:
            result["ip_id"] = first_ip_id
        result["ip_address"] = first_ip
        result["all_ips"] = [{"id": iid, "address": addr} for iid, addr in ip_results]

    return result
