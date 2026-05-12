"""Helper functions for VM synchronization - extracted from sync_vm.py."""

from __future__ import annotations

from ipaddress import ip_address, ip_interface
from typing import Literal

from proxbox_api.logger import logger
from proxbox_api.schemas.sync import SyncOverwriteFlags

PrimaryIPPreference = Literal["ipv4", "ipv6"]


def _compute_vm_patchable_fields(
    overwrite_flags: SyncOverwriteFlags | None,
    *,
    supports_virtual_machine_type_field: bool = True,
) -> set[str]:
    """Build the patchable_fields allowlist for virtual machine reconciliation."""
    fields: set[str] = {
        "name",
        "cluster",
        "device",
        "site",
        "tenant",
        "vcpus",
        "memory",
        "disk",
        "status",
    }
    if supports_virtual_machine_type_field and (
        overwrite_flags is None or overwrite_flags.overwrite_vm_type
    ):
        fields.add("virtual_machine_type")
    if overwrite_flags is None or overwrite_flags.overwrite_vm_role:
        fields.add("role")
    if overwrite_flags is None or overwrite_flags.overwrite_vm_tags:
        fields.add("tags")
    if overwrite_flags is None or overwrite_flags.overwrite_vm_description:
        fields.add("description")
    if overwrite_flags is None or overwrite_flags.overwrite_vm_custom_fields:
        fields.add("custom_fields")
    return fields


def normalize_current_virtual_machine_payload(
    record: dict[str, object],
    *,
    supports_virtual_machine_type_field: bool = True,
) -> dict[str, object]:
    """Normalize a NetBox VM record for diffing across NetBox 4.5 and 4.6."""
    payload = {
        "name": record.get("name"),
        "status": record.get("status"),
        "cluster": record.get("cluster"),
        "device": record.get("device"),
        "site": record.get("site"),
        "tenant": record.get("tenant"),
        "role": record.get("role"),
        "vcpus": record.get("vcpus"),
        "memory": record.get("memory"),
        "disk": record.get("disk"),
        "tags": record.get("tags"),
        "custom_fields": record.get("custom_fields"),
        "description": record.get("description"),
    }
    if supports_virtual_machine_type_field:
        payload["virtual_machine_type"] = record.get("virtual_machine_type")
    return payload


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


def parse_comma_separated_ints(value: object) -> list[int]:
    """Parse a comma-separated list of ints from any value.

    Non-string values are treated as absent instead of raising on `.split()`.
    """
    if not isinstance(value, str):
        return []
    result: list[int] = []
    for item in (part.strip() for part in value.split(",")):
        if item.isdigit():
            result.append(int(item))
    return result


def parse_key_value_string(value: object) -> dict[str, str]:
    """Parse comma-separated `key=value` text into a mapping."""
    if not isinstance(value, str):
        return {}
    parsed: dict[str, str] = {}
    for part in (segment.strip() for segment in value.split(",")):
        if not part or "=" not in part:
            continue
        key, raw = part.split("=", 1)
        key = key.strip()
        raw = raw.strip()
        if key:
            parsed[key] = raw
    return parsed


def _is_skippable_ip(ip_text: str, ignore_ipv6_link_local: bool = True) -> tuple[bool, str | None]:
    """Decide whether an IP should be skipped before reaching NetBox IPAM.

    Strips the IPv6 zone-ID suffix (``%eth0``, ``%vmbr0``...) unconditionally,
    since NetBox IPAM rejects zone-scoped addresses with a 400. Then checks
    whether the address is empty, unparseable, loopback, or (when the toggle
    is on) IPv6 link-local.

    Returns ``(True, None)`` when the address should be skipped, and
    ``(False, cleaned)`` with the canonical compressed form when it should
    be kept.
    """
    cleaned = str(ip_text or "").strip()
    if not cleaned:
        return (True, None)
    cleaned = cleaned.split("%", 1)[0]
    if not cleaned:
        return (True, None)
    try:
        parsed = ip_address(cleaned)
    except ValueError:
        return (True, None)
    if parsed.is_loopback:
        return (True, None)
    if ignore_ipv6_link_local and parsed.is_link_local:
        return (True, None)
    return (False, parsed.compressed)


def guest_agent_ip_with_prefix(
    addr: dict[str, object], ignore_ipv6_link_local: bool = True
) -> str | None:
    """Extract and format guest agent IP with prefix."""
    ip_text = str(addr.get("ip_address") or "").strip()
    skip, cleaned = _is_skippable_ip(ip_text, ignore_ipv6_link_local=ignore_ipv6_link_local)
    if skip or cleaned is None:
        return None
    prefix = addr.get("prefix")
    if isinstance(prefix, int) and 0 <= prefix <= 128:
        return f"{cleaned}/{prefix}"
    return cleaned


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


def all_guest_agent_ips(
    guest_iface: dict[str, object] | None,
    ignore_ipv6_link_local: bool = True,
    primary_ip_preference: PrimaryIPPreference = "ipv4",
) -> list[str]:
    """Return ALL valid IP addresses from guest agent interface data.

    Unlike best_guest_agent_ip() which returns only one, this returns every
    non-loopback IP (optionally filtering link-local). Each IP is returned
    in CIDR notation when prefix info is available.
    """
    if not isinstance(guest_iface, dict):
        return []
    results: list[str] = []
    for addr in guest_iface.get("ip_addresses") or []:
        if not isinstance(addr, dict):
            continue
        candidate = guest_agent_ip_with_prefix(addr, ignore_ipv6_link_local=ignore_ipv6_link_local)
        if candidate:
            results.append(candidate)
    return preferred_primary_ip_order(results, primary_ip_preference=primary_ip_preference)


def normalize_primary_ip_preference(value: object) -> PrimaryIPPreference:
    """Return normalized primary IP family preference."""
    normalized = str(value or "").strip().lower()
    return "ipv6" if normalized == "ipv6" else "ipv4"


def preferred_primary_ip_order(
    addresses: list[str],
    primary_ip_preference: PrimaryIPPreference = "ipv4",
) -> list[str]:
    """Sort addresses for primary selection preference by IP family."""
    preference = normalize_primary_ip_preference(primary_ip_preference)

    def _rank(address: str) -> tuple[int, int]:
        host = str(address or "").strip().split("/", 1)[0]
        try:
            parsed = ip_interface(str(address)).ip
        except ValueError:
            try:
                parsed = ip_address(host)
            except ValueError:
                return (2, 0)
        is_preferred = (parsed.version == 4 and preference == "ipv4") or (
            parsed.version == 6 and preference == "ipv6"
        )
        return (0 if is_preferred else 1, 0)

    # Keep input stability within each family bucket.
    return [
        addr for _, addr in sorted(enumerate(addresses), key=lambda item: (_rank(item[1]), item[0]))
    ]


def _matches_vm_criteria(
    resource: dict[str, object],
    vm_name: str,
    proxmox_vm_id: int | None,
    cluster_id: int | None,
) -> bool:
    """Check if a resource matches VM filtering criteria."""
    if resource.get("type") not in ("qemu", "lxc"):
        return False
    if str(resource.get("name", "")).strip() != vm_name:
        if proxmox_vm_id is None:
            return False
        if str(resource.get("vmid", "")).strip() != str(proxmox_vm_id):
            return False
    if cluster_id is not None:
        resource_cluster_id = relation_id(resource.get("cluster"))
        if resource_cluster_id is not None and resource_cluster_id != cluster_id:
            return False
    return True


def filter_cluster_resources_for_vm(
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
            selected = [
                r
                for r in resources
                if isinstance(r, dict)
                and _matches_vm_criteria(r, vm_name, proxmox_vm_id, cluster_id)
            ]
            if selected:
                filtered.append({cluster_key_str: selected})
    return filtered
