"""Shared helper functions for individual sync services."""

from __future__ import annotations


def normalize_mac(value: str | None) -> str:
    """Normalize a MAC address to lowercase string.

    Args:
        value: MAC address string or None.

    Returns:
        Normalized (lowercase, trimmed) MAC address string, or empty string.
    """
    return str(value or "").strip().lower()


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
    if px_list:
        return px_list[0]
    return None


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


def parse_key_value_string(raw_value: object) -> dict[str, str]:
    """Parse a Proxmox `netX` or `virtio` config entry into a key/value mapping.

    Proxmox returns these values as comma-separated `key=value` pairs.

    Args:
        raw_value: Raw config value (string or None).

    Returns:
        Dict of parsed key-value pairs.
    """
    if raw_value is None:
        return {}
    if not isinstance(raw_value, str):
        return {}
    result: dict[str, str] = {}
    for part in raw_value.split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            result[key.strip()] = value.strip()
    return result


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
    for part in raw_value.split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            result[key.strip()] = value.strip()
    return result


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
