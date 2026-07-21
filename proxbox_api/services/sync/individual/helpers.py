"""Shared helper functions for individual sync services."""

from __future__ import annotations

from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_list_async
from proxbox_api.services.custom_fields import legacy_custom_field_fallback_query
from proxbox_api.services.sync.sync_state_reader import (
    resolve_unique_virtual_machine_by_sync_state,
    resolve_virtual_machine_by_sync_state,
)
from proxbox_api.services.sync.vm_helpers import (
    build_guest_mac_index,
    iter_proxmox_net_config_items,
    merged_guest_iface_from_mac_index,
    normalized_mac,
    parse_key_value_string,
    resolve_netbox_cluster_id_by_name,
)
from proxbox_api.services.sync.vmid_helpers import extract_proxmox_session_endpoint_id


def normalize_mac(value: str | None) -> str:
    """Normalize a MAC address to lowercase string.

    Args:
        value: MAC address string or None.

    Returns:
        Normalized (lowercase, trimmed) MAC address string, or empty string.
    """
    return normalized_mac(value)


def resolve_proxmox_session(
    px_list: list[object],
    cluster_name: str,
) -> object | None:
    """Resolve a Proxmox session by cluster name.

    Args:
        px_list: List of Proxmox sessions.
        cluster_name: Name of the cluster to find.

    Returns:
        Matching Proxmox session or None.
    """
    for px in px_list:
        px_name = getattr(px, "name", None)
        if px_name and px_name.lower() == cluster_name.lower():
            return px
    return None


def resolve_proxmox_session_for_request(
    px_list: list[object],
    cluster_name: str | None,
    *,
    resource_name: str,
) -> object:
    """Resolve a target Proxmox session for request handlers.

    Single-session deployments continue to work without an explicit cluster name.
    Multi-session deployments must provide a cluster name so the request is not
    bound to whichever session happens to be first in the list.
    """

    target_cluster = (cluster_name or "").strip()
    if target_cluster:
        px = resolve_proxmox_session(px_list, target_cluster)
        if px is not None:
            return px
        raise ProxboxException(
            message=f"No Proxmox session found for cluster: {target_cluster}",
            detail=f"Unable to resolve {resource_name} request to a Proxmox session.",
        )

    if len(px_list) == 1:
        return px_list[0]

    raise ProxboxException(
        message=f"Multiple Proxmox sessions configured; provide cluster_name for {resource_name}.",
        detail="The requested cluster cannot be inferred when more than one Proxmox session is configured.",
    )


def build_interface_lookup_key(
    interface_name: str,
    vm_id: int | None = None,
) -> dict[str, object]:
    """Build lookup key for interface reconciliation.

    Args:
        interface_name: Name of the interface (e.g., 'net0', 'eth0').
        vm_id: Optional VM ID for more specific lookup.

    Returns:
        Lookup dict for rest_reconcile_async.
    """
    lookup: dict[str, object] = {"name": interface_name}
    if vm_id is not None:
        lookup["virtual_machine_id"] = vm_id
    return lookup


def build_disk_lookup_key(
    disk_name: str,
    vm_id: int | None = None,
) -> dict[str, object]:
    """Build lookup key for virtual disk reconciliation.

    Args:
        disk_name: Name of the disk (e.g., 'scsi0', 'virtio0', 'rootfs').
        vm_id: Optional VM ID for more specific lookup.

    Returns:
        Lookup dict for rest_reconcile_async.
    """
    lookup: dict[str, object] = {"name": disk_name}
    if vm_id is not None:
        lookup["virtual_machine_id"] = vm_id
    return lookup


def build_ip_lookup_key(ip_address: str) -> dict[str, str]:
    """Build lookup key for IP address reconciliation.

    Args:
        ip_address: IP address string (e.g., '192.168.1.1/24' or '192.168.1.1').

    Returns:
        Lookup dict for rest_reconcile_async.
    """
    address = ip_address.split("/")[0] if "/" in ip_address else ip_address
    return {"address": address}


def _build_guest_interface_maps(
    guest_interfaces: list[dict],
) -> tuple[dict[str, dict], dict[str, list[dict[str, object]]]]:
    """Build lookup maps for guest interfaces by name and MAC."""
    by_name: dict[str, dict] = {}
    for iface in guest_interfaces:
        name_key = str(iface.get("name", "")).strip().lower()
        if name_key:
            by_name[name_key] = iface
    return by_name, build_guest_mac_index(guest_interfaces)


def resolve_guest_interface(
    guest_interfaces: list[dict],
    interface_name: str,
    mac_address: str | None = None,
) -> tuple[dict | None, str, str | None]:
    """Resolve a guest interface by name or MAC and normalize its display name."""
    if not guest_interfaces:
        return None, interface_name, mac_address
    guest_by_name, guest_by_mac = _build_guest_interface_maps(guest_interfaces)
    guest_iface = None
    if mac_address:
        guest_iface = merged_guest_iface_from_mac_index(guest_by_mac, mac_address)
    if guest_iface is None:
        guest_iface = guest_by_name.get(interface_name.lower())
    if guest_iface is None:
        return None, interface_name, mac_address
    guest_name = str(guest_iface.get("name") or "").strip()
    if not guest_name:
        return guest_iface, interface_name, mac_address
    guest_mac = guest_iface.get("mac_address")
    resolved_mac = normalize_mac(guest_mac) if guest_mac and not mac_address else mac_address
    return guest_iface, guest_name, resolved_mac


def resolve_guest_interface_by_ip(
    guest_interfaces: list[dict],
    ip_address: str,
) -> str | None:
    """Find the guest interface name that owns a given IP address."""
    ip_address_clean = ip_address.split("/")[0] if "/" in ip_address else ip_address
    for iface in guest_interfaces:
        for addr in iface.get("ip_addresses") or []:
            addr_ip = str(addr.get("ip_address") or "").strip()
            addr_ip_clean = addr_ip.split("/")[0] if "/" in addr_ip else addr_ip
            if addr_ip_clean == ip_address_clean:
                return str(iface.get("name") or "").strip() or None
    return None


def parse_disk_config_entry(raw_value: object) -> dict[str, str]:
    """Parse a Proxmox disk config entry (rootfs, scsi0, virtio0, etc.).

    Args:
        raw_value: Raw disk config string or None.

    Returns:
        Dict with parsed fields (volume, size, etc.).
    """
    if raw_value is None:
        return {}
    if not isinstance(raw_value, str):
        return {}
    result: dict[str, str] = {}
    for index, part in enumerate(raw_value.split(",")):
        if "=" in part:
            key, value = part.split("=", 1)
            result[key.strip()] = value.strip()
        elif index == 0 and part.strip():
            result["volume"] = part.strip()
    return result


def extract_net_interface_config(vm_config: dict[str, object]) -> dict[str, dict[str, str]]:
    """Extract parsed netX interface entries from a VM config payload."""
    net_config: dict[str, dict[str, str]] = {}
    for key, value in iter_proxmox_net_config_items(vm_config):
        config_entry = parse_key_value_string(value)
        if config_entry:
            net_config[key] = config_entry
    return net_config


def serialize_record(record: object) -> dict[str, object] | None:
    """Serialize a NetBox record-like object into a plain dictionary."""
    if isinstance(record, dict):
        return record
    if hasattr(record, "serialize"):
        try:
            serialized = record.serialize()
        except Exception:
            serialized = None
        if isinstance(serialized, dict):
            return serialized
    if hasattr(record, "dict"):
        try:
            dumped = record.dict()
        except Exception:
            dumped = None
        if isinstance(dumped, dict):
            return dumped
    return None


async def get_first_record(
    nb: object,
    endpoint: str,
    query: dict[str, object],
) -> object | None:
    """Return the first NetBox record for a query, or None."""
    records = await rest_list_async(nb, endpoint, query=query)
    if records:
        return records[0]
    return None


async def get_serialized_first_record(
    nb: object,
    endpoint: str,
    query: dict[str, object],
) -> dict[str, object] | None:
    """Return the first NetBox record for a query as a dict, if any."""
    record = await get_first_record(nb, endpoint, query)
    if record is None:
        return None
    return serialize_record(record)


async def _lookup_unique_vm_by_vmid(
    nb: object,
    *,
    vmid: int,
    cluster_name: str,
) -> tuple[object | None, bool]:
    """Return a vmid-only match only when it is globally unambiguous."""
    sidecar_match, sidecar_ambiguous = await resolve_unique_virtual_machine_by_sync_state(
        nb,
        proxmox_vm_id=vmid,
    )
    if sidecar_match is not None:
        return sidecar_match.record, False
    if sidecar_ambiguous:
        logger.warning(
            "ambiguous vmid across clusters: cluster=%s vmid=%s matched multiple sidecar VMs",
            cluster_name or "unknown",
            vmid,
        )
        return None, True
    existing_vms: list[object] = []
    fallback_query = legacy_custom_field_fallback_query({"cf_proxmox_vm_id": vmid})
    if fallback_query is not None:
        existing_vms = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query=fallback_query,
        )
    if len(existing_vms) == 1:
        return existing_vms[0], False
    if len(existing_vms) > 1:
        logger.warning(
            "ambiguous vmid across clusters: cluster=%s vmid=%s matched %s NetBox VMs",
            cluster_name or "unknown",
            vmid,
            len(existing_vms),
        )
        return None, True
    return None, False


async def _lookup_vm_by_scope_or_unique_vmid(
    nb: object,
    *,
    vmid: int,
    endpoint_id: int | None,
    cluster_id: int | None,
    cluster_name: str,
) -> tuple[object | None, bool]:
    """Resolve by scoped VMID when possible, otherwise by unique vmid-only fallback."""
    if endpoint_id is not None:
        existing = await resolve_virtual_machine_by_sync_state(
            nb,
            proxmox_vm_id=vmid,
            endpoint_id=endpoint_id,
            fallback_query=legacy_custom_field_fallback_query(
                {"cf_proxmox_vm_id": vmid, "cf_proxmox_endpoint_id": endpoint_id}
            ),
        )
        if existing is not None:
            return existing.record, False
        return None, False
    if cluster_id is not None:
        existing = await resolve_virtual_machine_by_sync_state(
            nb,
            proxmox_vm_id=vmid,
            cluster_id=cluster_id,
            fallback_query=legacy_custom_field_fallback_query(
                {"cf_proxmox_vm_id": vmid, "cluster_id": cluster_id}
            ),
        )
        if existing is not None:
            return existing.record, False
        return None, False
    return await _lookup_unique_vm_by_vmid(nb, vmid=vmid, cluster_name=cluster_name)


def _vm_not_found_message(vmid: int, cluster_name: str) -> str:
    """Build the existing VM-not-found message with optional cluster context."""
    cluster_msg = f" in cluster {cluster_name}" if cluster_name else ""
    return f"VM with vmid={vmid}{cluster_msg} not found in NetBox"


async def ensure_vm_record(
    nb: object,
    px: object,
    tag: object,
    *,
    vmid: int,
    node: str | None,
    vm_type: str,
    auto_create_vm: bool,
    cluster_name: str | None = None,
    cluster_id: int | None = None,
) -> tuple[object | None, str | None]:
    """Resolve the NetBox VM record for a Proxmox VM ID, creating it if requested."""
    resolved_cluster_name = str(cluster_name or getattr(px, "name", "") or "").strip()
    endpoint_id = extract_proxmox_session_endpoint_id(px)
    resolved_cluster_id = cluster_id
    if resolved_cluster_id is None:
        resolved_cluster_id = await resolve_netbox_cluster_id_by_name(nb, resolved_cluster_name)

    vm_record, ambiguous = await _lookup_vm_by_scope_or_unique_vmid(
        nb,
        vmid=vmid,
        endpoint_id=endpoint_id,
        cluster_id=resolved_cluster_id,
        cluster_name=resolved_cluster_name,
    )
    if vm_record is not None:
        return vm_record, None
    if ambiguous:
        return None, _vm_not_found_message(vmid, resolved_cluster_name)

    if not auto_create_vm:
        return None, _vm_not_found_message(vmid, resolved_cluster_name)

    from proxbox_api.services.sync.individual.vm_sync import sync_vm_individual

    await sync_vm_individual(
        nb,
        px,
        tag,
        resolved_cluster_name or "unknown",
        node or "",
        vm_type,
        vmid,
        dry_run=False,
    )

    if resolved_cluster_id is None:
        resolved_cluster_id = await resolve_netbox_cluster_id_by_name(nb, resolved_cluster_name)

    vm_record, ambiguous = await _lookup_vm_by_scope_or_unique_vmid(
        nb,
        vmid=vmid,
        endpoint_id=endpoint_id,
        cluster_id=resolved_cluster_id,
        cluster_name=resolved_cluster_name,
    )
    if vm_record is not None:
        return vm_record, None
    if ambiguous:
        return None, _vm_not_found_message(vmid, resolved_cluster_name)

    return None, f"VM with vmid={vmid} could not be created in NetBox"


def build_sync_response(
    *,
    object_type: str,
    action: str,
    proxmox_resource: dict[str, object],
    netbox_object: dict[str, object] | None,
    dry_run: bool,
    dependencies_synced: list[dict[str, object]],
    error: str | None,
) -> dict[str, object]:
    """Build the standard individual-sync response payload."""
    return {
        "object_type": object_type,
        "action": action,
        "proxmox_resource": proxmox_resource,
        "netbox_object": netbox_object,
        "dry_run": dry_run,
        "dependencies_synced": dependencies_synced,
        "error": error,
    }


def storage_name_from_volume_id(volume_id: str | None) -> str | None:
    """Extract storage name from a Proxmox volume ID.

    Args:
        volume_id: Proxmox volume ID (e.g., 'local-lvm:vm-100-disk-0').

    Returns:
        Storage name (e.g., 'local-lvm') or None.
    """
    if volume_id is None:
        return None
    if ":" in volume_id:
        return volume_id.split(":", 1)[0]
    return volume_id
