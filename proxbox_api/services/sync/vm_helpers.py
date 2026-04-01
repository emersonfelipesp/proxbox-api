"""Helper functions for VM synchronization - extracted from sync_vm.py."""

from __future__ import annotations

from ipaddress import ip_address

from proxbox_api.logger import logger


def to_mapping(value: object) -> dict[str, object]:
    """Coerce any value to a dictionary mapping."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "serialize"):
        try:
            serialized = value.serialize()
            if isinstance(serialized, dict):
                return serialized
        except Exception as error:
            logger.debug("serialize() failed while coercing mapping: %s", error)
            return {}
    if hasattr(value, "dict"):
        try:
            dumped = value.dict()
            if isinstance(dumped, dict):
                return dumped
        except Exception as error:
            logger.debug("dict() failed while coercing mapping: %s", error)
            return {}
    return {}


def relation_name(value: object) -> str | None:
    """Extract relation name from a value."""
    if isinstance(value, dict):
        for key in ("name", "display", "label", "value"):
            candidate = value.get(key)
            if candidate:
                return str(candidate)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def relation_id(value: object) -> int | None:
    """Extract relation ID from a value."""
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        for key in ("id", "value"):
            candidate = value.get(key)
            if isinstance(candidate, int):
                return candidate
            if isinstance(candidate, str) and candidate.isdigit():
                return int(candidate)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def normalized_mac(value: str | None) -> str:
    """Normalize MAC address to lowercase stripped string."""
    return str(value or "").strip().lower()


def guest_agent_ip_with_prefix(
    addr: dict[str, object], ignore_ipv6_link_local: bool = True
) -> str | None:
    """Extract and format guest agent IP with prefix."""
    ip_text = str(addr.get("ip_address") or "").strip()
    if not ip_text:
        return None
    try:
        parsed = ip_address(ip_text)
    except ValueError:
        return None
    if parsed.is_loopback:
        return None
    if ignore_ipv6_link_local and parsed.is_link_local:
        return None
    prefix = addr.get("prefix")
    if isinstance(prefix, int) and 0 <= prefix <= 128:
        return f"{parsed.compressed}/{prefix}"
    return parsed.compressed


def best_guest_agent_ip(
    guest_iface: dict[str, object] | None, ignore_ipv6_link_local: bool = True
) -> str | None:
    """Find the best IP address from guest agent interface data."""
    if not isinstance(guest_iface, dict):
        return None
    for addr in guest_iface.get("ip_addresses") or []:
        if not isinstance(addr, dict):
            continue
        if str(addr.get("ip_address_type") or "").lower() == "ipv6":
            continue
        candidate = guest_agent_ip_with_prefix(addr, ignore_ipv6_link_local=ignore_ipv6_link_local)
        if candidate:
            return candidate
    for addr in guest_iface.get("ip_addresses") or []:
        if not isinstance(addr, dict):
            continue
        candidate = guest_agent_ip_with_prefix(addr, ignore_ipv6_link_local=ignore_ipv6_link_local)
        if candidate:
            return candidate
    return None


def filter_cluster_resources_for_vm(  # noqa: C901
    cluster_resources: list[dict[str, object]],
    *,
    vm_name: str,
    proxmox_vm_id: int | None,
    cluster_name: str | None,
    cluster_id: int | None,
) -> list[dict[str, object]]:
    """Filter cluster resources to find matching VM resources."""
    cluster_hint = (cluster_name or "").strip().lower()
    filtered: list[dict[str, object]] = []
    for cluster in cluster_resources:
        if not isinstance(cluster, dict):
            continue
        for cluster_key, resources in cluster.items():
            if not isinstance(resources, list):
                continue
            cluster_key_str = str(cluster_key)
            if cluster_hint and cluster_key_str.strip().lower() != cluster_hint:
                continue
            selected = []
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                if resource.get("type") not in ("qemu", "lxc"):
                    continue
                same_name = str(resource.get("name", "")).strip() == vm_name
                same_vmid = proxmox_vm_id is not None and str(
                    resource.get("vmid", "")
                ).strip() == str(proxmox_vm_id)
                if not (same_name or same_vmid):
                    continue
                if cluster_id is not None:
                    resource_cluster_id = relation_id(resource.get("cluster"))
                    if resource_cluster_id is not None and resource_cluster_id != cluster_id:
                        continue
                selected.append(resource)
            if selected:
                filtered.append({cluster_key_str: selected})
    return filtered
