"""Helper functions for VM synchronization - extracted from sync_vm.py."""

from __future__ import annotations

import inspect
import re
from ipaddress import ip_address, ip_interface
from typing import Literal

from proxbox_api.logger import logger
from proxbox_api.schemas.sync import SyncOverwriteFlags

PrimaryIPPreference = Literal["ipv4", "ipv6"]

_VM_DISK_AGGREGATE_ERROR_RE = re.compile(
    r"aggregate size of assigned virtual disks \((\d+)\)",
    flags=re.IGNORECASE,
)
_PROXMOX_NET_CONFIG_KEY_RE = re.compile(r"^net(\d+)$")


def _compute_vm_patchable_fields(
    overwrite_flags: SyncOverwriteFlags | None,
    *,
    supports_virtual_machine_type_field: bool = True,
) -> set[str]:
    """Build the patchable_fields allowlist for virtual machine reconciliation."""
    # Issue #365: tenant is owned by the netbox-proxbox plugin (name-regex
    # mapping); proxbox-api must never patch tenant on existing VMs nor send
    # it on the create body.
    fields: set[str] = {
        "name",
        "cluster",
        "device",
        "site",
        "vcpus",
        "memory",
        "disk",
        "status",
    }
    if supports_virtual_machine_type_field and (
        overwrite_flags is None or overwrite_flags.overwrite_vm_type
    ):
        fields.add("virtual_machine_type")
    # ``role`` and ``custom_fields`` are always patchable: per-VM lock is
    # enforced by the snapshot decision in the payload, and the snapshot
    # custom field itself must always be writable.
    fields.add("role")
    fields.add("custom_fields")
    if overwrite_flags is None or overwrite_flags.overwrite_vm_tags:
        fields.add("tags")
    if overwrite_flags is None or overwrite_flags.overwrite_vm_description:
        fields.add("description")
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


def extract_vm_disk_aggregate_size(error: Exception) -> int | None:
    """Extract NetBox's current virtual-disk aggregate from VM validation errors."""
    detail = getattr(error, "detail", None)
    text = str(detail) if detail else str(error)
    match = _VM_DISK_AGGREGATE_ERROR_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def to_mapping(value: object) -> dict[str, object]:
    """Coerce a NetBox record-ish value to a dictionary mapping.

    Supports plain dicts, netbox-sdk ``Record`` objects (``serialize()``),
    Pydantic v1 models (``dict()``), Pydantic v2 models (``model_dump()``), and
    Pydantic ``RootModel`` wrappers (``root``).

    Returning an empty mapping is a *failure* mode, not a neutral one: callers
    read ``name``/``custom_fields`` off the result and an empty dict makes a
    populated record look blank. Every path that gives up therefore logs loudly
    with the offending type so the cause is visible in backend logs.

    An un-awaited coroutine is called out explicitly because it is always a
    caller bug: the netbox-sdk accessors are ``async def``, so a missing
    ``await`` yields a coroutine here and previously degraded into a silent
    ``{}`` (see netbox-proxbox issue #616, where targeted single-VM sync failed
    with "has no name and no proxmox_vm_id custom field to match in Proxmox").
    """
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    if inspect.isawaitable(value):
        logger.error(
            "to_mapping() received an un-awaited %s -- the caller is missing an "
            "'await' on an async netbox-sdk call; treating the record as empty",
            type(value).__name__,
        )
        return {}
    for method_name in ("serialize", "model_dump", "dict"):
        method = getattr(value, method_name, None)
        if not callable(method):
            continue
        try:
            dumped = method()
        except Exception as error:
            logger.warning(
                "%s() failed while coercing %s to a mapping: %s",
                method_name,
                type(value).__name__,
                error,
            )
            return {}
        if isinstance(dumped, dict):
            return dumped
    root = getattr(value, "root", None)
    if root is not None and not callable(root):
        return to_mapping(root)
    logger.warning(
        "to_mapping() could not coerce %s to a mapping; treating it as empty",
        type(value).__name__,
    )
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


def record_id(value: object) -> int | None:
    """Extract a NetBox record ID from dict, serialized, or object-shaped values."""
    direct_id = relation_id(value)
    if direct_id is not None:
        return direct_id

    attr_id = getattr(value, "id", None)
    attr_id_int = relation_id(attr_id)
    if attr_id_int is not None:
        return attr_id_int

    mapped = to_mapping(value)
    if mapped:
        return relation_id(mapped)
    return None


def _match_cluster_id(clusters: object, cache_key: str) -> int | None:
    """Pick the cluster ID whose name matches ``cache_key``, else a nameless candidate."""
    nameless_candidate: int | None = None
    for cluster in clusters or []:
        cluster_id = record_id(cluster)
        if cluster_id is None:
            continue
        mapped = to_mapping(cluster)
        resolved_name = relation_name(mapped) or getattr(cluster, "name", None)
        if resolved_name is None:
            nameless_candidate = nameless_candidate or cluster_id
            continue
        if str(resolved_name).strip().casefold() == cache_key:
            return cluster_id
    return nameless_candidate


async def resolve_netbox_cluster_id_by_name(
    nb: object,
    cluster_name: str | None,
    *,
    cache: dict[str, int | None] | None = None,
) -> int | None:
    """Resolve a NetBox virtualization cluster ID by exact cluster name without creating it."""
    name = str(cluster_name or "").strip()
    if not name:
        return None

    cache_key = name.casefold()
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    from proxbox_api.netbox_rest import rest_list_async

    try:
        clusters = await rest_list_async(
            nb,
            "/api/virtualization/clusters/",
            query={"name": name, "limit": 2},
        )
    except Exception as error:
        logger.warning("Could not resolve NetBox cluster %s: %s", name, error)
        if cache is not None:
            cache[cache_key] = None
        return None

    resolved_id = _match_cluster_id(clusters, cache_key)
    if cache is not None:
        cache[cache_key] = resolved_id
    return resolved_id


def normalized_mac(value: object | None) -> str:
    """Normalize MAC address to lowercase stripped string."""
    return str(value or "").strip().lower()


def build_guest_mac_index(
    guest_interfaces: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    """Index guest-agent interfaces by normalized MAC address."""
    guest_by_mac: dict[str, list[dict[str, object]]] = {}
    for iface in guest_interfaces:
        if not isinstance(iface, dict):
            continue
        mac = normalized_mac(iface.get("mac_address"))
        if not mac:
            continue
        guest_by_mac.setdefault(mac, []).append(iface)
    return guest_by_mac


def _merged_guest_iface_from_matches(
    matches: list[dict[str, object]],
) -> dict[str, object] | None:
    """Merge guest-agent interface records that share one config NIC MAC."""
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    first = matches[0]
    merged_ip_addresses: list[dict[str, object]] = []
    seen_ip_keys: set[tuple[object, object]] = set()
    for iface in matches:
        for addr in iface.get("ip_addresses") or []:
            if not isinstance(addr, dict):
                continue
            dedupe_key = (addr.get("ip_address"), addr.get("prefix"))
            if dedupe_key in seen_ip_keys:
                continue
            seen_ip_keys.add(dedupe_key)
            merged_ip_addresses.append(addr)

    merged: dict[str, object] = {
        "name": first.get("name"),
        "mac_address": first.get("mac_address"),
        "ip_addresses": merged_ip_addresses,
    }
    for key in ("fqdn", "hostname"):
        if key in first:
            merged[key] = first[key]
    return merged


def merged_guest_iface_from_mac_index(
    guest_by_mac: dict[str, list[dict[str, object]]],
    mac: object | None,
) -> dict[str, object] | None:
    """Return one guest interface view for a config NIC MAC."""
    normalized = normalized_mac(mac)
    if not normalized:
        return None
    return _merged_guest_iface_from_matches(guest_by_mac.get(normalized) or [])


def merged_guest_iface_for_mac(
    guest_interfaces: list[dict[str, object]],
    mac: object | None,
) -> dict[str, object] | None:
    """Merge guest-agent interfaces that match one config NIC MAC."""
    return merged_guest_iface_from_mac_index(build_guest_mac_index(guest_interfaces), mac)


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


def iter_proxmox_net_config_items(
    vm_config: dict[str, object],
) -> list[tuple[str, object]]:
    """Return exact ``net<N>`` config entries sorted by numeric suffix.

    Proxmox VM config keys are sparse: a VM can legitimately have ``net1``
    without ``net0``. Iterate over the keys that are present instead of walking
    from zero until the first gap.
    """
    entries: list[tuple[int, str, object]] = []
    for key, value in vm_config.items():
        key_text = str(key)
        match = _PROXMOX_NET_CONFIG_KEY_RE.match(key_text)
        if match:
            entries.append((int(match.group(1)), key_text, value))
    entries.sort(key=lambda item: item[0])
    return [(key_text, value) for _index, key_text, value in entries]


def parse_proxmox_net_configs(
    vm_config: dict[str, object],
) -> list[dict[str, dict[str, str]]]:
    """Parse all exact Proxmox ``net<N>`` entries from a VM config payload."""
    networks: list[dict[str, dict[str, str]]] = []
    for network_name, network_info in iter_proxmox_net_config_items(vm_config):
        network_dict = parse_key_value_string(network_info)
        if not network_dict:
            logger.debug(
                "Skipping non-string or empty network config %s during parse: %r",
                network_name,
                type(network_info).__name__,
            )
            continue
        networks.append({network_name: network_dict})
    return networks


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


LAST_RUN_ID_CUSTOM_FIELD = "proxbox_last_run_id"


def _coerce_vm_record_to_dict(vm_record: object) -> dict[str, object] | None:
    """Coerce a NetBox VM record (dict or pynetbox-style) into a plain dict."""
    if isinstance(vm_record, dict):
        return vm_record
    if hasattr(vm_record, "dict"):
        try:
            coerced = vm_record.dict()
        except Exception as error:
            logger.debug("Failed to coerce VM record for stamping: %s", error)
            return None
        return coerced if isinstance(coerced, dict) else None
    return None


def _extract_vm_id(record: dict[str, object]) -> int | None:
    """Extract an integer id from a NetBox VM record dict."""
    raw_id = record.get("id")
    if isinstance(raw_id, int):
        return raw_id
    if raw_id is None:
        return None
    try:
        return int(raw_id)
    except (TypeError, ValueError):
        return None


async def stamp_vm_last_run_id(
    nb: object,
    vm_record: object,
    run_id: str | None,
) -> None:
    """Stamp `custom_fields.proxbox_last_run_id` on a NetBox VM after reconcile.

    Idempotent: if the record already carries the same run_id, no PATCH is issued.
    NetBox merges custom_field keys server-side on PATCH, so we only send the
    target key. Spreading the existing custom_fields dict into the payload would
    cause a serialization failure if any value is not JSON-serializable.
    This runs as a separate narrow PATCH so the stamp is written regardless of
    the operator's `overwrite_vm_custom_fields` gate.
    """
    if not isinstance(run_id, str) or not run_id or not vm_record:
        return

    record = _coerce_vm_record_to_dict(vm_record)
    if record is None:
        return

    record_id = _extract_vm_id(record)
    if not record_id:
        return

    current_cf = record.get("custom_fields")
    if not isinstance(current_cf, dict):
        current_cf = {}

    if current_cf.get(LAST_RUN_ID_CUSTOM_FIELD) == run_id:
        return

    from proxbox_api.netbox_rest import rest_patch_async

    try:
        await rest_patch_async(
            nb,
            "/api/virtualization/virtual-machines/",
            record_id,
            {"custom_fields": {LAST_RUN_ID_CUSTOM_FIELD: run_id}},
        )
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "Failed to stamp proxbox_last_run_id on VM id=%s name=%s: %s",
            record_id,
            record.get("name"),
            error,
        )

    from proxbox_api.services.sync.sync_state_writer import write_vm_last_run_sync_state

    await write_vm_last_run_sync_state(
        nb,
        virtual_machine_id=record_id,
        run_id=run_id,
    )
