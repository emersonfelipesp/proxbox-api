"""VM and node interface + IP synchronization helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from ipaddress import ip_interface as _ip_interface
from typing import Callable, Mapping, cast

from proxmox_sdk.sdk.exceptions import ResourceException

from proxbox_api.enum.status_mapping import NetBoxInterfaceType
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import (
    RestRecord,
    clear_rest_get_cache_for_path,
    rest_bulk_delete_async,
    rest_bulk_reconcile_async,
    rest_first_async,
    rest_list_async,
    rest_reconcile_async,
)
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxInterfaceSyncState,
    NetBoxIpAddressSyncState,
    NetBoxVirtualMachineInterfaceSyncState,
    NetBoxVlanSyncState,
)
from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.sync.guest_vm_interface import (
    should_use_guest_agent_core_interface_name,
)
from proxbox_api.services.sync.ip_ownership import (
    _ip_address_current_normalizer,
    _reconcile_interface_ip,
)
from proxbox_api.services.sync.sync_state_writer import write_vm_interface_sync_state
from proxbox_api.services.sync.vm_helpers import (
    _is_skippable_ip,
    all_guest_agent_ips,
    normalized_mac,
    preferred_primary_ip_order,
)

NETBOX_VM_INTERFACE_NAME_MAX_LENGTH = 64
_MIGRATED_NODE_INTERFACE_TYPES = frozenset({"ovsbridge", "ovsbond", "ovsintport"})


def _interface_choice_value(value: object) -> str:
    if isinstance(value, Mapping):
        value = cast(Mapping[str, object], value).get("value")
    return str(value or "").strip().lower()


def _interface_retype_blockers(record: RestRecord) -> tuple[str, ...]:
    blockers: list[str] = []
    if record.get("cable") not in (None, "", False):
        blockers.append("cable")
    if bool(record.get("mark_connected")):
        blockers.append("mark_connected")
    return tuple(blockers)


async def _safe_node_interface_type(
    nb: object,
    *,
    device_id: object,
    device_name: object,
    interface_name: str,
    proxmox_type: object,
) -> tuple[str, RestRecord | None, bool]:
    desired = NetBoxInterfaceType.from_proxmox(proxmox_type).value
    if str(proxmox_type or "").strip().lower() not in _MIGRATED_NODE_INTERFACE_TYPES:
        return desired, None, False

    clear_rest_get_cache_for_path(nb, "/api/dcim/interfaces/")
    existing = await rest_first_async(
        nb,
        "/api/dcim/interfaces/",
        query={"device_id": device_id, "name": interface_name, "limit": 2},
    )
    if existing is None:
        return desired, None, True

    current = _interface_choice_value(existing.get("type"))
    blockers = _interface_retype_blockers(existing)
    if not current or current == desired or not blockers:
        return desired, existing, True

    logger.warning(
        "Preserving NetBox interface type %s for device %s interface %s; "
        "cannot retype to %s while %s is set",
        current,
        device_name or device_id,
        interface_name,
        desired,
        " and ".join(blockers),
    )
    return current, existing, True


async def _call_node_interface_reconcile(
    nb: object,
    *,
    lookup: dict[str, object],
    payload: dict[str, object],
    current_normalizer: Callable[[dict[str, object]], dict[str, object]],
    patchable_fields: set[str] | frozenset[str] | None,
    strict_lookup: bool,
    lookup_query_field_map: dict[str, str] | None,
    existing_record: RestRecord | None,
    existing_record_supplied: bool,
) -> RestRecord:
    if existing_record_supplied:
        return await rest_reconcile_async(
            nb,
            "/api/dcim/interfaces/",
            lookup=lookup,
            payload=payload,
            schema=NetBoxInterfaceSyncState,
            current_normalizer=current_normalizer,
            patchable_fields=patchable_fields,
            strict_lookup=strict_lookup,
            lookup_query_field_map=lookup_query_field_map,
            existing_record=existing_record,
        )
    return await rest_reconcile_async(
        nb,
        "/api/dcim/interfaces/",
        lookup=lookup,
        payload=payload,
        schema=NetBoxInterfaceSyncState,
        current_normalizer=current_normalizer,
        patchable_fields=patchable_fields,
        strict_lookup=strict_lookup,
        lookup_query_field_map=lookup_query_field_map,
    )


async def _reconcile_node_interface_with_type_guard(
    nb: object,
    *,
    device_id: object,
    device_name: object,
    interface_name: str,
    proxmox_type: object,
    lookup: dict[str, object],
    payload: dict[str, object],
    current_normalizer: Callable[[dict[str, object]], dict[str, object]],
    patchable_fields: set[str] | frozenset[str] | None = None,
    strict_lookup: bool = False,
    lookup_query_field_map: dict[str, str] | None = None,
) -> RestRecord:
    desired, existing, checked = await _safe_node_interface_type(
        nb,
        device_id=device_id,
        device_name=device_name,
        interface_name=interface_name,
        proxmox_type=proxmox_type,
    )
    desired_payload = {**payload, "type": desired}
    try:
        return await _call_node_interface_reconcile(
            nb,
            lookup=lookup,
            payload=desired_payload,
            current_normalizer=current_normalizer,
            patchable_fields=patchable_fields,
            strict_lookup=strict_lookup,
            lookup_query_field_map=lookup_query_field_map,
            existing_record=existing,
            existing_record_supplied=checked,
        )
    except ProxboxException:
        if not checked:
            raise
        clear_rest_get_cache_for_path(nb, "/api/dcim/interfaces/")
        refreshed = await rest_first_async(
            nb,
            "/api/dcim/interfaces/",
            query={"device_id": device_id, "name": interface_name, "limit": 2},
        )
        if refreshed is None:
            raise
        current = _interface_choice_value(refreshed.get("type"))
        blockers = _interface_retype_blockers(refreshed)
        if not current or current == desired or not blockers:
            raise
        logger.warning(
            "Preserving NetBox interface type %s for device %s interface %s after "
            "concurrent migration conflict; cannot retype to %s while %s is set",
            current,
            device_name or device_id,
            interface_name,
            desired,
            " and ".join(blockers),
        )
        return await _call_node_interface_reconcile(
            nb,
            lookup=lookup,
            payload={**desired_payload, "type": current},
            current_normalizer=current_normalizer,
            patchable_fields=patchable_fields,
            strict_lookup=strict_lookup,
            lookup_query_field_map=lookup_query_field_map,
            existing_record=refreshed,
            existing_record_supplied=True,
        )


def _proxmox_node_interface_payload(interface: object) -> dict[str, object]:
    """Return a normalized dict for one Proxmox node network row."""
    if hasattr(interface, "model_dump"):
        data = interface.model_dump(mode="python", by_alias=True, exclude_none=True)
    elif isinstance(interface, dict):
        data = dict(interface)
    else:
        data = dict(getattr(interface, "__dict__", {}) or {})

    if "vlan-id" in data:
        data["vlan_id"] = data.pop("vlan-id")
    if "vlan-raw-device" in data:
        data["vlan_raw_device"] = data.pop("vlan-raw-device")
    return data


async def load_proxmox_node_network(
    proxmox_session: object,
    node: str,
    *,
    network_type: object | None = None,
) -> list[dict[str, object]]:
    """Fetch and normalize ``GET /nodes/{node}/network`` for one Proxmox node."""
    try:
        accessor = proxmox_session.session(f"/nodes/{node}/network")
        if network_type is not None:
            raw_networks = await resolve_async(accessor.get(type=network_type))
        else:
            raw_networks = await resolve_async(accessor.get())
    except ResourceException as error:
        raise ProxboxException(
            message="Error getting node network interfaces from Proxmox",
            python_exception=str(error),
        ) from error

    if raw_networks is None:
        return []
    if not isinstance(raw_networks, list):
        raise ProxboxException(
            message="Unexpected Proxmox node network response",
            detail=f"Expected list for node {node}, got {type(raw_networks).__name__}.",
        )
    return [_proxmox_node_interface_payload(interface) for interface in raw_networks]


def _relation_id_or_none(value: object) -> int | None:
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_vm_interface_name(
    interface_name: object,
    *,
    fallback: str = "net0",
    vm_name: str | None = None,
) -> str:
    """Normalize VMInterface.name before NetBox validation."""

    def _sanitize(value: object) -> str:
        text = str(value or "").strip()
        return "".join(char for char in text if ord(char) >= 32 and ord(char) != 127).strip()

    raw_name = str(interface_name or "").strip() or str(fallback or "").strip()
    # Sanitize the primary candidate; if it collapses to empty, fall back to the
    # sanitized fallback, and finally to a guaranteed-safe hard default. This
    # ensures control characters can never reach VMInterface.name via the
    # fallback path (callers pass guest/caller-derived names as fallback).
    sanitized_name = _sanitize(interface_name) or _sanitize(fallback) or "net0"
    if sanitized_name != raw_name:
        logger.warning(
            "Sanitized VM interface name for VM %s: %r -> %r",
            vm_name or "unknown",
            raw_name,
            sanitized_name,
        )
    normalized_name = sanitized_name[:NETBOX_VM_INTERFACE_NAME_MAX_LENGTH]
    if normalized_name != sanitized_name:
        logger.warning(
            "Truncated VM interface name from %d to %d chars for VM %s: %r -> %r",
            len(sanitized_name),
            NETBOX_VM_INTERFACE_NAME_MAX_LENGTH,
            vm_name or "unknown",
            sanitized_name,
            normalized_name,
        )
    return normalized_name


async def _sync_legacy_node_vlan(
    nb: object,
    *,
    iface_type: object,
    vlan_id_raw: object,
    interface_name: str,
    tag_refs: list[dict],
) -> int | None:
    if iface_type != "vlan" or vlan_id_raw is None:
        return None
    try:
        vlan_vid = int(str(vlan_id_raw))
        record = await rest_reconcile_async(
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
            current_normalizer=lambda row: {
                "vid": row.get("vid"),
                "name": row.get("name"),
                "status": row.get("status"),
                "tags": row.get("tags"),
            },
        )
        return _record_id(record)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to create/sync VLAN vid=%s for node interface %s: %s",
            vlan_id_raw,
            interface_name,
            exc,
        )
        return None


async def sync_node_interface_and_ip(
    nb,
    device: dict,
    interface_name: str,
    interface_config: dict,
    tag_refs: list[dict],
) -> dict:
    node_cidr = interface_config.get("cidr") or interface_config.get("address")
    iface_type = interface_config.get("type", "other")
    vlan_id_raw = interface_config.get("vlan_id")
    vlan_nb_id = await _sync_legacy_node_vlan(
        nb,
        iface_type=iface_type,
        vlan_id_raw=vlan_id_raw,
        interface_name=interface_name,
        tag_refs=tag_refs,
    )

    interface = await _reconcile_node_interface_with_type_guard(
        nb,
        device_id=device.get("id", 0),
        device_name=device.get("name"),
        interface_name=interface_name,
        proxmox_type=iface_type,
        lookup={
            "device": device.get("id", 0),
            "name": interface_name,
        },
        payload={
            "device": device.get("id", 0),
            "name": interface_name,
            "status": "active",
            "untagged_vlan": vlan_nb_id,
            "mode": "access" if vlan_nb_id is not None else None,
            "tags": tag_refs,
        },
        current_normalizer=lambda record: {
            "device": record.get("device"),
            "name": record.get("name"),
            "status": record.get("status"),
            "type": record.get("type"),
            "untagged_vlan": record.get("untagged_vlan"),
            "mode": record.get("mode"),
            "tags": record.get("tags"),
        },
        lookup_query_field_map={"device": "device_id"},
        strict_lookup=True,
    )
    interface_id = getattr(interface, "id", None) or (
        interface.get("id") if isinstance(interface, dict) else None
    )
    result: dict = {"id": interface_id, "name": interface_name}

    if node_cidr and interface_id is not None:
        try:
            ip_id = await _reconcile_interface_ip(
                nb,
                ip_addr=node_cidr,
                interface_id=int(interface_id),
                tag_refs=tag_refs,
                now=datetime.now(timezone.utc),
                dns_name=None,
                interface_name=interface_name,
                assigned_object_type="dcim.interface",
                interface_lookup_field="interface_id",
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


# Proxmox /network entry types that are not modeled as standalone NetBox
# interfaces (loopback, and Open vSwitch internal plumbing / ifupdown aliases).
_NODE_IFACE_SKIP_TYPES = {"loopback", "alias"}


def _node_network_members(entry: dict, *fields: str) -> list[str]:
    return " ".join(str(entry.get(field) or "") for field in fields).split()


def _node_network_membership(
    entries: list[dict],
) -> tuple[dict[str, str], dict[str, str]]:
    """Map each member interface name -> its bridge / bond parent name.

    Built from the Linux and Open vSwitch parent/member fields in the raw
    Proxmox network payload.
    """
    member_bridge: dict[str, str] = {}
    member_bond: dict[str, str] = {}
    for entry in entries:
        parent = str(entry.get("iface") or "")
        for member in _node_network_members(entry, "bridge_ports", "ovs_ports"):
            member_bridge[member] = parent
        for member in _node_network_members(entry, "bond_slaves", "ovs_bonds"):
            member_bond[member] = parent
        ovs_bridge = str(entry.get("ovs_bridge") or "").strip()
        if ovs_bridge:
            member_bridge[parent] = ovs_bridge
    return member_bridge, member_bond


def _is_network_id(cidr: str) -> bool:
    """True if ``cidr`` is a subnet's network address (host bits all zero).

    NetBox refuses to assign such an address to an interface. Mirrors its
    leniency for host-style prefixes (/31,/32 and /127,/128), where the
    "network" address is a valid assignable host.
    """
    try:
        iface = _ip_interface(cidr)
    except ValueError:
        return False
    return iface.ip == iface.network.network_address and iface.network.prefixlen < (
        iface.max_prefixlen - 1
    )


def _hwaddress_from_options(entry: dict) -> str | None:
    """Extract a MAC from a Proxmox interface entry's ``options`` (``hwaddress ...``).

    Proxmox exposes a MAC in /network only for bridges/bonds carrying an explicit
    ``hwaddress`` option; physical NIC MACs are not present in the network API
    (they require ethtool/sysfs via the hardware-discovery path).
    """
    for opt in entry.get("options") or []:
        parts = str(opt).split()
        if len(parts) == 2 and parts[0].lower() == "hwaddress":
            return parts[1]
    return None


def _node_iface_normalizer(record: dict) -> dict:
    return {
        "device": record.get("device"),
        "name": record.get("name"),
        "status": record.get("status"),
        "enabled": record.get("enabled"),
        "type": record.get("type"),
        "bridge": record.get("bridge"),
        "lag": record.get("lag"),
        "parent": record.get("parent"),
        "untagged_vlan": record.get("untagged_vlan"),
        "tagged_vlans": record.get("tagged_vlans"),
        "mode": record.get("mode"),
        "tags": record.get("tags"),
    }


def _record_id(record: object) -> int | None:
    """Extract a NetBox record id from a RestRecord-like object or a dict."""
    raw = getattr(record, "id", None) or (record.get("id") if isinstance(record, dict) else None)
    return _relation_id_or_none(raw)


def _node_interface_payload(
    device_id: object,
    iface: str,
    entry: dict,
    tag_refs: list[dict],
) -> dict:
    return {
        "device": device_id,
        "name": iface,
        "status": "active",
        "enabled": bool(entry.get("active")),
        "tags": tag_refs,
    }


async def _reconcile_node_interface_scalar(
    nb: object,
    *,
    device: dict,
    entry: dict,
    tag_refs: list[dict],
) -> object:
    device_id = device.get("id")
    iface = entry["iface"]
    return await _reconcile_node_interface_with_type_guard(
        nb,
        device_id=device_id,
        device_name=device.get("name"),
        interface_name=iface,
        proxmox_type=entry.get("type"),
        lookup={"device_id": device_id, "name": iface},
        payload=_node_interface_payload(device_id, iface, entry, tag_refs),
        current_normalizer=_node_iface_normalizer,
        patchable_fields=frozenset({"device", "name", "status", "enabled", "type", "tags"}),
    )


async def _sync_node_interface_vlan(
    nb: object, entry: dict, iface: str, tag_refs: list[dict]
) -> int | None:
    if entry.get("type") != "vlan" or not entry.get("vlan-id"):
        return None
    try:
        vid = int(entry["vlan-id"])
        record = await rest_reconcile_async(
            nb,
            "/api/ipam/vlans/",
            lookup={"vid": vid},
            payload={"vid": vid, "name": f"VLAN {vid}", "status": "active", "tags": tag_refs},
            schema=NetBoxVlanSyncState,
            current_normalizer=lambda row: {
                "vid": row.get("vid"),
                "name": row.get("name"),
                "status": row.get("status"),
                "tags": row.get("tags"),
            },
        )
        return _record_id(record)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to sync VLAN for node interface %s: %s", iface, exc)
        return None


async def _sync_node_interface_addresses(
    nb: object,
    entry: dict,
    iface: str,
    iface_id: int | None,
    tag_refs: list[dict],
    now: datetime,
) -> list[object]:
    addresses: list[object] = []
    if iface_id is None:
        return addresses
    for field in ("cidr", "cidr6"):
        cidr = entry.get(field)
        if not cidr or _is_network_id(cidr):
            continue
        try:
            await _reconcile_interface_ip(
                nb,
                ip_addr=cidr,
                interface_id=iface_id,
                tag_refs=tag_refs,
                now=now,
                dns_name=None,
                interface_name=iface,
                assigned_object_type="dcim.interface",
                interface_lookup_field="interface_id",
            )
            addresses.append(cidr)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to sync IP %s on node interface %s: %s", cidr, iface, exc)
    return addresses


async def _sync_node_interface_mac(
    nb: object, entry: dict, iface: str, iface_id: int | None, tag_refs: list[dict]
) -> str | None:
    from proxbox_api.services.sync.mac_address import normalize_mac, reconcile_mac_for_interface

    mac = normalize_mac(_hwaddress_from_options(entry))
    if not mac or iface_id is None:
        return None
    try:
        await reconcile_mac_for_interface(
            nb,
            mac=mac,
            assigned_object_type="dcim.interface",
            assigned_object_id=iface_id,
            interface_list_path="/api/dcim/interfaces/",
            tag_refs=tag_refs,
        )
        return mac
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to sync MAC %s on node interface %s: %s", mac, iface, exc)
        return None


async def _sync_node_network_phase_one(
    nb: object,
    device: dict,
    entries: list[dict],
    tag_refs: list[dict],
    now: datetime,
) -> tuple[dict[str, int], dict[str, int], list[dict]]:
    name_to_id: dict[str, int] = {}
    vlan_ids: dict[str, int] = {}
    results: list[dict] = []
    for entry in entries:
        iface = entry["iface"]
        interface = await _reconcile_node_interface_scalar(
            nb, device=device, entry=entry, tag_refs=tag_refs
        )
        iface_id = _record_id(interface)
        if iface_id is None:
            raise ProxboxException(
                message="Node interface reconciliation returned no persisted record",
                detail=f"Device {device.get('name') or device.get('id')} interface {iface}",
            )
        name_to_id[iface] = iface_id
        result: dict = {"id": iface_id, "name": iface}
        vlan_id = await _sync_node_interface_vlan(nb, entry, iface, tag_refs)
        if vlan_id is not None:
            vlan_ids[iface] = vlan_id
        addresses = await _sync_node_interface_addresses(nb, entry, iface, iface_id, tag_refs, now)
        if addresses:
            result["ip_addresses"] = addresses
        mac = await _sync_node_interface_mac(nb, entry, iface, iface_id, tag_refs)
        if mac:
            result["mac_address"] = mac
        results.append(result)
    return name_to_id, vlan_ids, results


def _node_interface_topology_patch(
    entry: dict,
    name_to_id: dict[str, int],
    vlan_ids: dict[str, int],
    member_bridge: dict[str, str],
    member_bond: dict[str, str],
) -> dict:
    iface = entry["iface"]
    patch: dict = {
        "bridge": None,
        "lag": None,
        "parent": None,
        "mode": None,
        "tagged_vlans": [],
    }
    bridge = member_bridge.get(iface)
    bond = member_bond.get(iface)
    if bridge in name_to_id:
        patch["bridge"] = name_to_id[bridge]
    if bond in name_to_id:
        patch["lag"] = name_to_id[bond]
    if entry.get("type") != "vlan":
        return patch
    parent = entry.get("vlan-raw-device")
    if parent in name_to_id:
        patch["parent"] = name_to_id[parent]
    if iface in vlan_ids:
        patch.update(mode="tagged", tagged_vlans=[vlan_ids[iface]])
    return patch


async def _sync_node_network_topology(
    nb: object,
    device_id: object,
    entries: list[dict],
    name_to_id: dict[str, int],
    vlan_ids: dict[str, int],
) -> None:
    member_bridge, member_bond = _node_network_membership(entries)
    for entry in entries:
        iface = entry["iface"]
        if iface not in name_to_id:
            continue
        patch = _node_interface_topology_patch(
            entry, name_to_id, vlan_ids, member_bridge, member_bond
        )
        await rest_reconcile_async(
            nb,
            "/api/dcim/interfaces/",
            lookup={"device_id": device_id, "name": iface},
            payload={
                "device": device_id,
                "name": iface,
                "type": NetBoxInterfaceType.from_proxmox(entry.get("type")).value,
                **patch,
            },
            schema=NetBoxInterfaceSyncState,
            current_normalizer=_node_iface_normalizer,
            patchable_fields=frozenset(patch),
            nullable_fields=frozenset({"bridge", "lag", "parent", "mode"}),
        )


async def sync_node_network(
    nb,
    device: dict,
    network_entries: list[dict],
    tag_refs: list[dict],
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Reconcile a Proxmox node's full ``/nodes/{node}/network`` config into NetBox.

    Models physical NICs, bridges, bonds and VLAN sub-interfaces as
    ``dcim.Interface`` records on the node device, including topology
    (bridge/bond membership, VLAN sub-interface parent), enabled state and IPs.

    ``network_entries`` must be the **raw** ``/nodes/{node}/network`` payload
    (a direct proxmox-sdk call), not the normalized ``ProxmoxNodeInterface`` SDK
    model. The reconcile reads hyphenated keys (``vlan-id``,
    ``vlan-raw-device``) and topology/state keys (``bridge_ports``,
    ``bond_slaves``, ``options``, ``active``, ``cidr6``) that the normalized
    model renames or drops.

    Two phases are required because the topology FKs (``bridge``/``lag``/
    ``parent``) reference *sibling* interfaces: phase 1 reconciles every
    interface's scalar fields + IPs (+ VLAN objects) and collects a name -> id
    map; phase 2 patches the cross-references once all ids are known.

    A VLAN sub-interface (e.g. ``vmbr1.200``) is modeled as ``mode=tagged`` with
    the VLAN in ``tagged_vlans`` because in Proxmox it is an 802.1Q-tagged
    sub-interface carrying that single VID on its parent — distinct from the
    legacy per-interface path (``sync_node_interface_and_ip``), which models a
    bridge's ``untagged_vlan`` as ``mode=access``.
    """
    now = now or datetime.now(timezone.utc)
    device_id = device.get("id")
    entries = [
        e
        for e in (network_entries or [])
        if e.get("iface") and e.get("iface") != "lo" and e.get("type") not in _NODE_IFACE_SKIP_TYPES
    ]
    name_to_id, vlan_ids, results = await _sync_node_network_phase_one(
        nb, device, entries, tag_refs, now
    )
    await _sync_node_network_topology(nb, device_id, entries, name_to_id, vlan_ids)
    return results


def _resolve_vm_interface_identity(
    interface_name: str,
    interface_config: dict,
    guest_iface: dict | None,
    use_guest_agent_interface_name: bool,
    vm_interface_sync_strategy: object = "guest_os_model",
) -> tuple[str, str | None]:
    """Resolve the display name and MAC address for a VM interface."""
    mac_address = interface_config.get("virtio") or interface_config.get("hwaddr")
    resolved_name = interface_name
    if (
        should_use_guest_agent_core_interface_name(
            use_guest_agent_interface_name,
            vm_interface_sync_strategy,
        )
        and guest_iface
    ):
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
    *,
    site_id: int | None = None,
    tenant_id: int | None = None,
) -> dict:
    """Build a VLAN payload dict for bulk operations (no NetBox writes).

    Args:
        vlan_tag: VLAN ID (vid)
        tag_refs: List of tag references
        now: Retained for call-site compatibility.

    Returns:
        Payload dict for bulk reconciliation
    """
    payload: dict[str, object] = {
        "vid": vlan_tag,
        "name": f"VLAN {vlan_tag}",
        "status": "active",
        "tags": tag_refs,
    }
    if site_id is not None:
        payload["site"] = site_id
    if tenant_id is not None:
        payload["tenant"] = tenant_id
    return payload


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

    The ``mac_address`` argument is accepted for backward-compatible call sites
    but is no longer placed in the payload — NetBox 4.5/4.6 treat the inline
    field as read-only, so the value must be written to ``dcim.MACAddress``
    separately. See ``proxbox_api.services.sync.mac_address``.
    """
    _ = mac_address  # kept in the signature for source-call compatibility
    normalized_name = normalize_vm_interface_name(resolved_name)
    payload: dict = {
        "name": normalized_name,
        "enabled": True,
        "untagged_vlan": vlan_id,
        "mode": "access" if vlan_id is not None else None,
        "tags": tag_refs,
        "proxbox_bridge_id": bridge_id,
    }
    if vm_id is not None:
        payload["virtual_machine"] = vm_id
    return payload


def _vm_interface_result_summary(result: object | None) -> tuple[int, int, int, int]:
    if result is None:
        return 0, 0, 0, 0
    return (
        int(getattr(result, "created", 0) or 0),
        int(getattr(result, "updated", 0) or 0),
        int(getattr(result, "unchanged", 0) or 0),
        int(getattr(result, "failed", 0) or 0),
    )


def _log_vm_interface_partial_failures(
    *,
    interface_payloads: list[dict],
    records: list[dict],
    failed_count: int,
) -> None:
    succeeded_keys: set[tuple[object, object]] = set()
    for record in records:
        vm_obj = record.get("virtual_machine")
        vm_id = vm_obj.get("id") if isinstance(vm_obj, dict) else vm_obj
        succeeded_keys.add((record.get("name"), vm_id))

    failed_payloads = [
        payload
        for payload in interface_payloads
        if (payload.get("name"), payload.get("virtual_machine")) not in succeeded_keys
    ]
    logger.warning(
        "Bulk VM interface reconciliation completed with partial failures: "
        "succeeded=%d failed=%d requested=%d. See preceding per-item NetBox errors "
        "for the transport response detail.",
        max(len(interface_payloads) - failed_count, 0),
        failed_count,
        len(interface_payloads),
    )
    for payload in failed_payloads[:failed_count]:
        logger.warning(
            "VM interface reconcile record failed: vm_id=%s interface=%s payload=%s",
            payload.get("virtual_machine"),
            payload.get("name"),
            payload,
        )


def _vm_interface_bulk_failure_is_systemic(
    result: object,
    *,
    requested_count: int,
    failed_count: int,
) -> bool:
    if (
        bool(getattr(result, "systemic_failure", False))
        or bool(getattr(result, "transport_failure", False))
        or bool(getattr(result, "transport_error", False))
    ):
        return True
    if failed_count <= 0:
        return False
    return requested_count > 0 and not getattr(result, "records", None)


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
        now: Retained for call-site compatibility.
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
            },
            schema=NetBoxVlanSyncState,
            current_normalizer=lambda record: {
                "vid": record.get("vid"),
                "name": record.get("name"),
                "status": record.get("status"),
                "tags": record.get("tags"),
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
    vm_interface_sync_strategy: object = "guest_os_model",
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
        vm_interface_sync_strategy,
    )
    resolved_name = normalize_vm_interface_name(
        resolved_name,
        fallback=interface_name,
        vm_name=str(virtual_machine.get("name") or ""),
    )

    payload: dict = {
        "name": resolved_name,
        "enabled": True,
        "bridge": None,
        "untagged_vlan": vlan_nb_id,
        "mode": "access" if vlan_nb_id is not None else None,
        "tags": tag_refs,
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
            "type": record.get("type"),
            "description": record.get("description"),
            "bridge": record.get("bridge"),
            "untagged_vlan": record.get("untagged_vlan"),
            "mode": record.get("mode"),
            "tags": record.get("tags"),
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
    await write_vm_interface_sync_state(
        nb,
        vm_interface_id=interface_id,
        proxbox_bridge_id=bridge_id,
        overwrite_custom_fields=True,
    )

    # Write the MAC to dcim.MACAddress and link primary_mac_address. The
    # legacy inline field on VMInterface is read-only at NetBox 4.5/4.6, so
    # this is the only write path that actually persists the MAC.
    if interface_id is not None and mac_address:
        from proxbox_api.services.sync.mac_address import reconcile_mac_for_vm_interface

        try:
            await reconcile_mac_for_vm_interface(
                nb,
                vminterface_id=int(interface_id),
                mac=mac_address,
                tag_refs=tag_refs,
            )
        except Exception as mac_exc:
            logger.warning(
                "Failed to reconcile MAC %s for VM interface %s: %s",
                mac_address,
                resolved_name,
                mac_exc,
            )

    return vm_interface, interface_id, resolved_name


async def bulk_reconcile_vlans(
    nb,
    vlan_payloads: list[dict],
) -> dict[object, int]:
    """Perform bulk reconciliation of VLAN payloads. Returns mapping of VLAN lookup key → NetBox ID.

    Args:
        nb: NetBox session
        vlan_payloads: List of VLAN payload dicts

    Returns:
        Dict mapping VLAN vid and (vid, site_id, tenant_id) to NetBox ID
    """
    if not vlan_payloads:
        return {}

    vlan_vid_to_id: dict[object, int] = {}
    try:
        result = await rest_bulk_reconcile_async(
            nb,
            "/api/ipam/vlans/",
            payloads=vlan_payloads,
            lookup_fields=["vid", "site", "tenant"],
            # NetBox's `site`/`tenant` filters match by slug, but the payload
            # carries their NetBox ids. Without this remap the id is silently
            # ignored, so the existence check is not scoped to site/tenant and
            # the reconcile can match/patch the wrong VLAN (or recreate one).
            lookup_query_field_map={"site": "site_id", "tenant": "tenant_id"},
            schema=NetBoxVlanSyncState,
            current_normalizer=lambda record: {
                "vid": record.get("vid"),
                "name": record.get("name"),
                "status": record.get("status"),
                "site": record.get("site"),
                "tenant": record.get("tenant"),
                "tags": record.get("tags"),
            },
        )
        # Build mapping of vid → ID from returned records
        for record in result.records:
            vid = record.get("vid")
            vlan_id = _relation_id_or_none(record.get("id"))
            if vid and vlan_id is not None:
                normalized_vid = int(vid)
                site_id = _relation_id_or_none(record.get("site"))
                tenant_id = _relation_id_or_none(record.get("tenant"))
                vlan_vid_to_id[(normalized_vid, site_id, tenant_id)] = vlan_id
                vlan_vid_to_id.setdefault(normalized_vid, vlan_id)
    except Exception as e:
        logger.error("Error during bulk VLAN reconciliation: %s", e)
    return vlan_vid_to_id


def _vm_interface_sidecar_payloads_by_key(
    interface_payloads: list[dict],
) -> dict[tuple[object, object], dict]:
    sidecar_payload_by_key: dict[tuple[object, object], dict] = {}
    for payload in interface_payloads:
        if payload.get("proxbox_bridge_id") is not None:
            sidecar_payload_by_key[(payload.get("name"), payload.get("virtual_machine"))] = payload
    return sidecar_payload_by_key


async def _write_vm_interface_sidecars_for_bulk_result(
    nb: object,
    *,
    records: list[dict],
    sidecar_payload_by_key: dict[tuple[object, object], dict],
    overwrite_flags: SyncOverwriteFlags | None,
) -> None:
    for record in records:
        name = record.get("name")
        vm_obj = record.get("virtual_machine")
        vm_id = vm_obj.get("id") if isinstance(vm_obj, dict) else vm_obj
        iface_id = record.get("id")
        if not (name and vm_id and iface_id):
            continue
        sidecar_payload = sidecar_payload_by_key.get((name, vm_id))
        if sidecar_payload is None:
            continue
        await write_vm_interface_sync_state(
            nb,
            vm_interface_id=iface_id,
            proxbox_bridge_id=sidecar_payload.get("proxbox_bridge_id"),
            overwrite_custom_fields=(
                overwrite_flags is None or overwrite_flags.overwrite_vm_interface_custom_fields
            ),
        )


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

    # VM interface scalar identity/state fields are always patchable; tags
    # follow the per-resource overwrite flag. When overwrite_flags is None,
    # all normalizer keys are patchable, preserving
    # the historical always-overwrite behavior.
    # `mac_address` is intentionally absent: it is a read-only computed field
    # at NetBox 4.5/4.6; the MAC is persisted via dcim.MACAddress in a
    # follow-up post-step (see proxbox_api.services.sync.mac_address).
    _vm_interface_patchable: set[str] = {
        "name",
        "virtual_machine",
        "enabled",
        "type",
        "description",
        "untagged_vlan",
        "mode",
    }
    if overwrite_flags is None or overwrite_flags.overwrite_vm_interface_tags:
        _vm_interface_patchable.add("tags")
    interface_name_vm_to_id = {}
    result = None
    try:
        sidecar_payload_by_key = _vm_interface_sidecar_payloads_by_key(interface_payloads)
        result = await rest_bulk_reconcile_async(
            nb,
            "/api/virtualization/interfaces/",
            payloads=[
                {key: value for key, value in payload.items() if key != "proxbox_bridge_id"}
                for payload in interface_payloads
            ],
            lookup_fields=["name", "virtual_machine"],
            # NetBox's `virtual_machine` filter matches by VM *name*; the payload
            # carries the VM id, so without this remap the id is silently ignored
            # and the existence check is not scoped to the VM. The reconcile then
            # tries to re-create existing interfaces (HTTP 400 "already exists").
            lookup_query_field_map={"virtual_machine": "virtual_machine_id"},
            schema=NetBoxVirtualMachineInterfaceSyncState,
            patchable_fields=frozenset(_vm_interface_patchable),
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "virtual_machine": _relation_id_or_none(record.get("virtual_machine")),
                "enabled": record.get("enabled"),
                "type": record.get("type"),
                "description": record.get("description"),
                "untagged_vlan": _relation_id_or_none(record.get("untagged_vlan")),
                "mode": record.get("mode"),
                "tags": record.get("tags"),
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
        await _write_vm_interface_sidecars_for_bulk_result(
            nb,
            records=result.records,
            sidecar_payload_by_key=sidecar_payload_by_key,
            overwrite_flags=overwrite_flags,
        )
        failed_count = _vm_interface_result_summary(result)[3]
        if failed_count:
            if _vm_interface_bulk_failure_is_systemic(
                result,
                requested_count=len(interface_payloads),
                failed_count=failed_count,
            ):
                raise ProxboxException(
                    message=(
                        "VM interface bulk reconciliation failed for every payload or "
                        "systemically; interface sync is incomplete for this pass."
                    ),
                    detail=(
                        f"{failed_count} of {len(interface_payloads)} VM interface "
                        "payload(s) failed."
                    ),
                )
            _log_vm_interface_partial_failures(
                interface_payloads=interface_payloads,
                records=result.records,
                failed_count=failed_count,
            )
    except Exception as e:
        logger.error("Error during bulk VM interface reconciliation: %s", e)
        raise
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
    # time; status, tags, and DNS name are safe to update, gated by the
    # per-field overwrite_ip_* flags.
    if overwrite_flags is None:
        patchable_fields = frozenset({"status", "tags", "dns_name"})
    else:
        gated: set[str] = set()
        if overwrite_flags.overwrite_ip_status:
            gated.add("status")
        if overwrite_flags.overwrite_ip_tags:
            gated.add("tags")
        if overwrite_flags.overwrite_ip_address_dns_name:
            gated.add("dns_name")
        patchable_fields = frozenset(gated)

    result = None
    try:
        result = await rest_bulk_reconcile_async(
            nb,
            "/api/ipam/ip-addresses/",
            payloads=ip_payloads,
            lookup_fields=["address", "assigned_object_id"],
            schema=NetBoxIpAddressSyncState,
            current_normalizer=_ip_address_current_normalizer,
            patchable_fields=patchable_fields,
            base_query={"assigned_object_type": "virtualization.vminterface"},
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
        ip_id = await _reconcile_interface_ip(
            nb,
            ip_addr=ip_addr,
            interface_id=interface_id,
            tag_refs=tag_refs,
            now=now,
            dns_name=dns_name,
            interface_name=interface_name,
        )
        if ip_id is not None:
            results.append((ip_id, ip_addr))

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
    vm_interface_sync_strategy: object = "guest_os_model",
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
            vm_interface_sync_strategy=vm_interface_sync_strategy,
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
