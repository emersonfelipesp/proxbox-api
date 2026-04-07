"""Virtual machine creation sync and SSE stream endpoints."""

# FastAPI Imports
import asyncio
import inspect
from dataclasses import dataclass, field
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from proxbox_api.cache import global_cache
from proxbox_api.dependencies import NetBoxSessionDep, ProxboxTagDep
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_compat import VirtualMachine
from proxbox_api.netbox_rest import (
    rest_create_async,
    rest_first_async,
    rest_list_async,
    rest_patch_async,
    rest_reconcile_async,
)
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxDeviceRoleSyncState,
    NetBoxVirtualDiskSyncState,
    NetBoxVirtualMachineCreateBody,
    NetBoxVirtualMachineInterfaceSyncState,
    NetBoxVlanSyncState,
    ProxmoxVmConfigInput,
)
from proxbox_api.routes.extras import CreateCustomFieldsDep
from proxbox_api.routes.proxmox import get_vm_config
from proxbox_api.routes.proxmox.cluster import ClusterResourcesDep, ClusterStatusDep
from proxbox_api.routes.virtualization.virtual_machines.helpers import (
    resolve_netbox_write_concurrency,
    resolve_vm_sync_concurrency,
)
from proxbox_api.schemas.stream_messages import ErrorCategory, ItemOperation, SubstepStatus
from proxbox_api.services.proxmox_helpers import get_qemu_guest_agent_network_interfaces
from proxbox_api.services.sync.devices import (
    _ensure_cluster,
    _ensure_cluster_type,
    _ensure_device,
    _ensure_device_type,
    _ensure_manufacturer,
    _ensure_site,
)
from proxbox_api.services.sync.devices import (
    _ensure_device_role as _ensure_proxmox_node_role,
)
from proxbox_api.services.sync.network import (
    _resolve_vm_interface_identity,
    _resolve_vm_interface_ip,
)
from proxbox_api.services.sync.storage_links import (
    build_storage_index,
    find_storage_record,
    storage_name_from_volume_id,
)
from proxbox_api.services.sync.task_history import (
    sync_virtual_machine_task_history,
)
from proxbox_api.services.sync.virtual_machines import (
    build_netbox_virtual_machine_payload,
)
from proxbox_api.services.sync.vm_helpers import (
    parse_comma_separated_ints,
    parse_key_value_string,
)
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.utils import return_status_html
from proxbox_api.utils.streaming import WebSocketSSEBridge, sse_event

router = APIRouter()


@dataclass(slots=True)
class _PreparedVMState:
    """In-memory VM snapshot prepared from Proxmox + dependency cache."""

    cluster_name: str
    resource: dict[str, object]
    vm_config: dict[str, object]
    vm_config_obj: ProxmoxVmConfigInput
    desired_payload: dict[str, object]
    lookup: dict[str, object]
    now: datetime
    vm_type: str


@dataclass(slots=True)
class _NetBoxVMOperation:
    """Queued NetBox VM operation determined by in-memory reconciliation."""

    method: Literal["GET", "CREATE", "UPDATE"]
    prepared: _PreparedVMState
    existing_record: dict[str, object] | None = None
    patch_payload: dict[str, object] = field(default_factory=dict)


def _normalize_current_virtual_machine_payload(record: dict[str, object]) -> dict[str, object]:
    """Normalize NetBox VM record for Pydantic diff comparison."""

    return {
        "name": record.get("name"),
        "status": record.get("status"),
        "cluster": record.get("cluster"),
        "device": record.get("device"),
        "role": record.get("role"),
        "vcpus": record.get("vcpus"),
        "memory": record.get("memory"),
        "disk": record.get("disk"),
        "tags": record.get("tags"),
        "custom_fields": record.get("custom_fields"),
        "description": record.get("description"),
    }


def _extract_cluster_and_proxmox_vmid(record: dict[str, object]) -> tuple[int, int] | None:
    """Build the in-memory index key used to correlate NetBox VM records."""

    cluster_id = _relation_id(record.get("cluster"))
    if cluster_id is None:
        return None
    custom_fields = record.get("custom_fields")
    if not isinstance(custom_fields, dict):
        return None
    raw_vmid = custom_fields.get("proxmox_vm_id")
    try:
        proxmox_vmid = int(str(raw_vmid).strip())
    except (TypeError, ValueError):
        return None
    return (cluster_id, proxmox_vmid)


async def _load_netbox_virtual_machine_snapshot(nb: object) -> list[dict[str, object]]:
    """Fetch all NetBox virtual machines once and keep them in-memory for comparison."""

    page_size = 200
    offset = 0
    snapshot: list[dict[str, object]] = []

    while True:
        records = await rest_list_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"limit": page_size, "offset": offset},
        )
        if not records:
            break

        serialized_page: list[dict[str, object]] = []
        for record in records:
            serialized = _to_mapping(record)
            if serialized:
                serialized_page.append(serialized)

        snapshot.extend(serialized_page)
        if len(records) < page_size:
            break
        offset += page_size

    return snapshot


def _build_vm_operation_queue(
    prepared_vms: list[_PreparedVMState],
    netbox_snapshot: list[dict[str, object]],
) -> list[_NetBoxVMOperation]:
    """Classify desired VM state into GET/CREATE/UPDATE operations using Pydantic."""

    snapshot_index: dict[tuple[int, int], dict[str, object]] = {}
    for current in netbox_snapshot:
        key = _extract_cluster_and_proxmox_vmid(current)
        if key is not None and key not in snapshot_index:
            snapshot_index[key] = current

    operation_queue: list[_NetBoxVMOperation] = []

    for prepared in prepared_vms:
        cluster_id = _relation_id(prepared.desired_payload.get("cluster"))
        proxmox_vmid = _relation_id(prepared.resource.get("vmid"))
        if cluster_id is None or proxmox_vmid is None:
            operation_queue.append(_NetBoxVMOperation(method="CREATE", prepared=prepared))
            continue

        existing_record = snapshot_index.get((cluster_id, proxmox_vmid))
        if existing_record is None:
            operation_queue.append(_NetBoxVMOperation(method="CREATE", prepared=prepared))
            continue

        desired_state = NetBoxVirtualMachineCreateBody.model_validate(prepared.desired_payload)
        desired_payload = desired_state.model_dump(exclude_none=True, by_alias=True)
        current_state = NetBoxVirtualMachineCreateBody.model_validate(
            _normalize_current_virtual_machine_payload(existing_record)
        )
        current_payload = current_state.model_dump(exclude_none=True, by_alias=True)

        patch_payload = {
            field_name: desired_value
            for field_name, desired_value in desired_payload.items()
            if current_payload.get(field_name) != desired_value
        }

        if patch_payload:
            operation_queue.append(
                _NetBoxVMOperation(
                    method="UPDATE",
                    prepared=prepared,
                    existing_record=existing_record,
                    patch_payload=patch_payload,
                )
            )
        else:
            operation_queue.append(
                _NetBoxVMOperation(
                    method="GET",
                    prepared=prepared,
                    existing_record=existing_record,
                )
            )

    return operation_queue


async def _dispatch_vm_operation_queue(
    nb: object,
    operation_queue: list[_NetBoxVMOperation],
) -> dict[tuple[str, int], dict[str, object]]:
    """Dispatch queued VM operations sequentially in deterministic batches."""

    if not operation_queue:
        return {}

    batch_size = max(1, resolve_netbox_write_concurrency())
    resolved_records: dict[tuple[str, int], dict[str, object]] = {}

    for start_index in range(0, len(operation_queue), batch_size):
        batch = operation_queue[start_index : start_index + batch_size]
        for operation in batch:
            vmid = int(operation.prepared.resource.get("vmid", 0) or 0)
            key = (operation.prepared.cluster_name, vmid)

            if operation.method == "GET":
                if operation.existing_record is not None:
                    resolved_records[key] = operation.existing_record
                continue

            if operation.method == "CREATE":
                try:
                    created = await rest_create_async(
                        nb,
                        "/api/virtualization/virtual-machines/",
                        operation.prepared.desired_payload,
                    )
                    resolved_records[key] = _to_mapping(created)
                except ProxboxException:
                    existing = await rest_first_async(
                        nb,
                        "/api/virtualization/virtual-machines/",
                        query={**operation.prepared.lookup, "limit": 2},
                    )
                    if existing is None:
                        raise
                    resolved_records[key] = _to_mapping(existing)
                continue

            if operation.existing_record is None:
                raise ProxboxException(
                    message="Cannot update VM without existing NetBox record",
                    python_exception=(f"cluster={operation.prepared.cluster_name} vmid={vmid}"),
                )

            record_id = _relation_id(operation.existing_record.get("id"))
            if record_id is None:
                raise ProxboxException(
                    message="Cannot update VM without NetBox id",
                    python_exception=f"cluster={operation.prepared.cluster_name} vmid={vmid}",
                )

            patched = await rest_patch_async(
                nb,
                "/api/virtualization/virtual-machines/",
                record_id,
                operation.patch_payload,
            )
            if isinstance(patched, dict) and patched:
                resolved_records[key] = patched
            else:
                merged = dict(operation.existing_record)
                merged.update(operation.patch_payload)
                merged["id"] = record_id
                resolved_records[key] = merged

    return resolved_records


def _to_mapping(value: object) -> dict[str, object]:
    """Coerce a value to a dictionary representation.

    Attempts multiple serialization strategies in order:
    1. Direct dict check
    2. Call serialize() method if available
    3. Call dict() method if available (Pydantic models)
    4. Return empty dict if all fail

    Args:
        value: Value to convert to a dictionary

    Returns:
        Dictionary representation of the value, or empty dict if conversion fails
    """
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


def _relation_name(value: object) -> str | None:
    """Extract a human-readable name from a relation object or value.

    Attempts to extract a name string from various object representations:
    - Dict values by key priority: 'name', 'display', 'label', 'value'
    - Direct string values (trimmed)

    Args:
        value: Value to extract name from (dict, string, or object)

    Returns:
        Extracted name string, or None if no valid name found
    """
    if isinstance(value, dict):
        for key in ("name", "display", "label", "value"):
            candidate = value.get(key)
            if candidate:
                return str(candidate)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _relation_id(value: object) -> int | None:
    """Extract a numeric ID from a relation object or value.

    Attempts to extract an integer ID from various object representations:
    - Direct int values
    - Dict values by key priority: 'id', 'value' (as int or digit string)
    - String digit values

    Args:
        value: Value to extract ID from (int, dict, or string)

    Returns:
        Extracted ID as integer, or None if no valid ID found
    """
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


def _parse_network_config_entry(raw_value: object) -> dict[str, str]:
    """Parse a Proxmox `netX` config entry into a key/value mapping.

    Proxmox returns these values as comma-separated `key=value` pairs.
    When a non-string value slips through, treat it as absent instead of
    raising an attribute error on `.split()`.
    """
    return parse_key_value_string(raw_value)


def _parse_vm_networks(vm_config: dict[str, object]) -> list[dict[str, dict[str, str]]]:
    """Extract and parse `net0`, `net1`, ... entries from a VM config."""
    networks: list[dict[str, dict[str, str]]] = []
    network_id = 0
    while True:
        network_name = f"net{network_id}"
        raw = vm_config.get(network_name)
        if raw is None:
            break
        network_dict = _parse_network_config_entry(raw)
        if network_dict:
            networks.append({network_name: network_dict})
        network_id += 1
    return networks


def _filter_cluster_resources_for_vm(  # noqa: C901
    cluster_resources: list[dict],
    *,
    vm_name: str,
    proxmox_vm_id: int | None,
    cluster_name: str | None,
    cluster_id: int | None,
) -> list[dict]:
    """Filter cluster resources to find VM matching name and/or ID criteria.

    Searches through cluster resource lists to find QEMU VMs or LXC containers
    matching the provided identifiers. Optionally filters by cluster name/ID.

    Args:
        cluster_resources: List of cluster resource dicts from Proxmox
        vm_name: VM name to match (exact match)
        proxmox_vm_id: Proxmox VM ID to match, or None
        cluster_name: Cluster name to filter by, or None for all clusters
        cluster_id: NetBox cluster ID to filter by, or None for all

    Returns:
        Filtered list of cluster resource dicts containing matching VMs
    """
    cluster_hint = (cluster_name or "").strip().lower()
    filtered: list[dict] = []
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
                    resource_cluster_id = _relation_id(resource.get("cluster"))
                    if resource_cluster_id is not None and resource_cluster_id != cluster_id:
                        continue
                selected.append(resource)
            if selected:
                filtered.append({cluster_key_str: selected})
    return filtered


async def _filter_cluster_resources_by_netbox_vm_ids(  # noqa: C901
    netbox_session: NetBoxSessionDep,
    cluster_resources: list[dict],
    netbox_vm_ids: list[int],
) -> list[dict]:
    """Filter cluster resources to only include VMs matching the given NetBox VM IDs."""
    from proxbox_api.netbox_rest import rest_list_async

    if not netbox_vm_ids:
        return cluster_resources

    id_to_vm: dict[int, dict] = {}
    for vm_id in netbox_vm_ids:
        id_to_vm[vm_id] = {"id": vm_id, "name": None, "cluster": None, "cf_proxmox_vm_id": None}

    try:
        vms = await rest_list_async(
            netbox_session,
            "/api/virtualization/virtual-machines/",
            query={"id": ",".join(str(vid) for vid in netbox_vm_ids)},
        )
        if vms and isinstance(vms, list):
            for vm in vms:
                if not isinstance(vm, dict):
                    continue
                vm_id = vm.get("id")
                if vm_id is not None:
                    id_to_vm[vm_id] = vm
    except Exception:
        pass

    target_proxmox_vm_ids: set[int] = set()
    target_vm_names: set[str] = set()
    target_cluster_ids: set[int] = set()

    for vm in id_to_vm.values():
        cf = vm.get("custom_fields", {}) or {}
        raw_vmid = cf.get("proxmox_vm_id")
        if raw_vmid is not None and str(raw_vmid).strip().isdigit():
            target_proxmox_vm_ids.add(int(str(raw_vmid).strip()))
        vm_name = str(vm.get("name", "")).strip()
        if vm_name:
            target_vm_names.add(vm_name.lower())
        cluster = vm.get("cluster")
        if isinstance(cluster, dict):
            cluster_id = cluster.get("id")
            if isinstance(cluster_id, int):
                target_cluster_ids.add(cluster_id)

    filtered: list[dict] = []
    for cluster in cluster_resources:
        if not isinstance(cluster, dict):
            continue
        for cluster_key, resources in cluster.items():
            if not isinstance(resources, list):
                continue
            selected = []
            for resource in resources:
                if not isinstance(resource, dict):
                    continue
                if resource.get("type") not in ("qemu", "lxc"):
                    continue
                res_vmid = resource.get("vmid")
                if res_vmid is not None and int(res_vmid) in target_proxmox_vm_ids:
                    selected.append(resource)
                    continue
                res_name = str(resource.get("name", "")).strip().lower()
                if res_name in target_vm_names:
                    selected.append(resource)
                    continue
            if selected:
                filtered.append({cluster_key: selected})

    return filtered


async def _resolve_netbox_virtual_machine_by_proxmox_id(
    netbox_session: NetBoxSessionDep,
    proxmox_vm_id: int | str | None,
) -> dict[str, object] | None:
    """Resolve the NetBox VM row that corresponds to a Proxmox VM id."""
    if proxmox_vm_id is None:
        return None

    try:
        vmid = int(str(proxmox_vm_id).strip())
    except (TypeError, ValueError):
        return None

    try:
        virtual_machines = await rest_list_async(
            netbox_session,
            "/api/virtualization/virtual-machines/",
            query={"cf_proxmox_vm_id": vmid},
        )
    except Exception as exc:
        error_detail = getattr(exc, "detail", str(exc))
        error_msg = f"{type(exc).__name__}: {error_detail}"
        logger.warning(
            "Could not resolve NetBox VM for Proxmox VMID %s: %s",
            proxmox_vm_id,
            error_msg,
        )
        return None

    if not virtual_machines:
        return None

    virtual_machine = virtual_machines[0]
    if isinstance(virtual_machine, dict):
        return virtual_machine
    if hasattr(virtual_machine, "dict"):
        dumped = virtual_machine.dict()
        if isinstance(dumped, dict):
            return dumped
    return None


def _normalized_mac(value: str | None) -> str:
    """Normalize a MAC address to lowercase string.

    Args:
        value: MAC address string or None

    Returns:
        Normalized (lowercase, trimmed) MAC address string, or empty string
    """
    return str(value or "").strip().lower()


def _guest_agent_ip_with_prefix(addr: dict, ignore_ipv6_link_local: bool = True) -> str | None:
    """Extract IP address with CIDR prefix from guest agent address dict.

    Filters out loopback and optionally link-local addresses. Returns the IP in CIDR
    notation (e.g., "192.168.1.1/24") when prefix is available.

    Args:
        addr: Address dict from guest agent with 'ip_address', 'prefix' keys
        ignore_ipv6_link_local: If True, skip IPv6 link-local addresses (fe80::/64)

    Returns:
        IP address with CIDR prefix, just IP, or None if invalid
    """
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


def _best_guest_agent_ip(
    guest_iface: dict | None, ignore_ipv6_link_local: bool = True
) -> str | None:
    """Select the best IP address from guest agent interface data.

    Prioritizes IPv4 addresses with valid CIDR prefixes, then falls back to
    any valid IPv4 address. Skips loopback and optionally link-local addresses.

    Args:
        guest_iface: Guest agent interface dict with 'ip_addresses' list
        ignore_ipv6_link_local: If True, skip IPv6 link-local addresses

    Returns:
        Best available IP address (with prefix if available), or None
    """
    if not isinstance(guest_iface, dict):
        return None
    for addr in guest_iface.get("ip_addresses") or []:
        if not isinstance(addr, dict):
            continue
        if str(addr.get("ip_address_type") or "").lower() == "ipv6":
            continue
        candidate = _guest_agent_ip_with_prefix(addr, ignore_ipv6_link_local=ignore_ipv6_link_local)
        if candidate:
            return candidate
    for addr in guest_iface.get("ip_addresses") or []:
        if not isinstance(addr, dict):
            continue
        candidate = _guest_agent_ip_with_prefix(addr, ignore_ipv6_link_local=ignore_ipv6_link_local)
        if candidate:
            return candidate
    return None


async def _create_vm_interface_parallel(
    nb,
    virtual_machine: dict,
    interface_name: str,
    interface_config: dict,
    guest_iface: dict | None,
    tag_refs: list[dict],
    use_guest_agent_interface_name: bool,
    ignore_ipv6_link_local_addresses: bool,
    now: datetime,
) -> dict:
    """Create a single VM interface with bridge, VLAN, and IP in parallel-friendly manner.

    Returns a dict with 'interface' (the created interface), 'ip' (the created IP or None),
    and 'first_ip_id' (first IP id found, for setting VM primary_ip).
    """
    vm_id = virtual_machine.get("id")
    result: dict = {"interface": None, "ip": None, "first_ip_id": None}

    bridge: dict = {}
    bridge_name = interface_config.get("bridge")
    if bridge_name and vm_id:
        existing_bridge = await rest_first_async(
            nb,
            "/api/virtualization/interfaces/",
            query={"name": bridge_name, "virtual_machine_id": vm_id},
        )
        if existing_bridge:
            bridge = (
                existing_bridge.serialize()
                if hasattr(existing_bridge, "serialize")
                else dict(existing_bridge)
            )
        else:
            bridge = await rest_reconcile_async(
                nb,
                "/api/virtualization/interfaces/",
                lookup={"name": bridge_name, "virtual_machine_id": vm_id},
                payload={
                    "name": bridge_name,
                    "virtual_machine": vm_id,
                    "type": "bridge",
                    "tags": tag_refs,
                    "custom_fields": {"proxmox_last_updated": now.isoformat()},
                },
                schema=NetBoxVirtualMachineInterfaceSyncState,
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "virtual_machine": record.get("virtual_machine"),
                    "type": record.get("type"),
                    "tags": record.get("tags"),
                    "custom_fields": record.get("custom_fields"),
                },
            )
            if not isinstance(bridge, dict):
                bridge = getattr(bridge, "dict", lambda: {})()
        if bridge and not isinstance(bridge, dict):
            bridge = getattr(bridge, "dict", lambda: {})()

    vlan_nb_id: int | None = None
    vlan_tag_raw = interface_config.get("tag")
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
                "Failed to create/sync VLAN tag=%s for interface %s: %s",
                vlan_tag_raw,
                interface_name,
                vlan_exc,
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
        "bridge": bridge.get("id") if bridge else None,
        "mac_address": mac_address,
        "untagged_vlan": vlan_nb_id,
        "mode": "access" if vlan_nb_id is not None else None,
        "tags": tag_refs,
        "custom_fields": {"proxmox_last_updated": now.isoformat()},
    }
    if vm_id:
        payload["virtual_machine"] = vm_id

    lookup: dict = {"name": resolved_name}
    if vm_id:
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
            "bridge": record.get("bridge"),
            "mac_address": record.get("mac_address"),
            "type": record.get("type"),
            "description": record.get("description"),
            "untagged_vlan": record.get("untagged_vlan"),
            "mode": record.get("mode"),
            "tags": record.get("tags"),
            "custom_fields": record.get("custom_fields"),
        },
    )
    if not isinstance(vm_interface, dict):
        vm_interface = getattr(vm_interface, "dict", lambda: {})()

    interface_id = (
        vm_interface.get("id")
        if isinstance(vm_interface, dict)
        else getattr(vm_interface, "id", None)
    )
    result["interface"] = vm_interface

    ip_id, interface_ip = await _resolve_vm_interface_ip(
        nb,
        interface_config,
        guest_iface,
        tag_refs,
        interface_id=interface_id,
        interface_name=interface_name,
        now=now,
        create_ip=True,
        ignore_ipv6_link_local=ignore_ipv6_link_local_addresses,
    )
    if interface_ip and interface_ip != "dhcp":
        result["ip"] = {"id": ip_id, "address": interface_ip}
        result["first_ip_id"] = ip_id

    return result


async def _create_vm_disk_parallel(
    nb,
    virtual_machine: dict,
    disk_entry,
    cluster_name: str,
    storage_index: dict,
    tag_refs: list[dict],
    now: datetime,
) -> dict | None:
    """Create a single VM disk.

    Returns the created disk record or None on failure.
    """
    storage_name = disk_entry.storage_name or storage_name_from_volume_id(disk_entry.storage)
    storage_record = find_storage_record(
        storage_index,
        cluster_name=cluster_name,
        storage_name=storage_name,
    )
    try:
        disk = await rest_reconcile_async(
            nb,
            "/api/virtualization/virtual-disks/",
            lookup={
                "virtual_machine_id": virtual_machine.get("id"),
                "name": disk_entry.name,
            },
            payload={
                "virtual_machine": virtual_machine.get("id"),
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
        return disk
    except Exception as exc:
        error_detail = getattr(exc, "detail", str(exc))
        error_msg = f"{type(exc).__name__}: {error_detail}"
        logger.warning(
            "Failed to create disk %s for VM %s: %s",
            disk_entry.name,
            virtual_machine.get("name"),
            error_msg,
        )
        return None


async def _create_virtual_machine_by_netbox_id(
    *,
    netbox_vm_id: int,
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket=None,
    use_websocket: bool = False,
    use_guest_agent_interface_name: bool = True,
    ignore_ipv6_link_local_addresses: bool = True,
):
    """Create a single virtual machine by its NetBox ID.

    Looks up the NetBox VM record, extracts metadata, filters Proxmox resources
    for matching VM, and creates/updates the VM in NetBox.
    The delegated VM bundle also reconciles interfaces, IP addresses, disks,
    and task history for the targeted VM.

    Args:
        netbox_vm_id: NetBox virtual machine ID to sync.
        netbox_session: NetBox API session.
        pxs: Proxmox session(s).
        cluster_status: Cluster status objects.
        cluster_resources: Proxmox cluster resources.
        custom_fields: Custom field configurations.
        tag: ProxBox tag reference.
        websocket: Optional WebSocket for progress updates.
        use_websocket: Whether to send WebSocket updates.
        use_guest_agent_interface_name: Use guest-agent interface names if available.
        ignore_ipv6_link_local_addresses: Ignore IPv6 link-local addresses when selecting IPs.

    Returns:
        List of created/synced VM records from NetBox.

    Raises:
        HTTPException: If VM not found, missing name, or no matching Proxmox resource.
    """
    vm_record = netbox_session.virtualization.virtual_machines.get(id=netbox_vm_id)
    if vm_record is None:
        raise HTTPException(
            status_code=404,
            detail=f"Virtual machine id={netbox_vm_id} was not found in NetBox.",
        )

    vm_data = _to_mapping(vm_record)
    vm_name = str(vm_data.get("name", "")).strip()
    if not vm_name:
        raise HTTPException(
            status_code=422,
            detail=f"Virtual machine id={netbox_vm_id} has no name to match in Proxmox.",
        )
    vm_cluster_name = _relation_name(vm_data.get("cluster"))
    vm_cluster_id = _relation_id(vm_data.get("cluster"))
    cf = vm_data.get("custom_fields")
    proxmox_vm_id = None
    if isinstance(cf, dict):
        raw_id = cf.get("proxmox_vm_id")
        if raw_id is not None and str(raw_id).strip().isdigit():
            proxmox_vm_id = int(str(raw_id).strip())

    filtered_resources = _filter_cluster_resources_for_vm(
        cluster_resources,
        vm_name=vm_name,
        proxmox_vm_id=proxmox_vm_id,
        cluster_name=vm_cluster_name,
        cluster_id=vm_cluster_id,
    )
    if not filtered_resources:
        raise HTTPException(
            status_code=404,
            detail=(
                "No matching Proxmox VM was found for NetBox virtual machine "
                f"id={netbox_vm_id} (name={vm_name!r})."
            ),
        )

    filtered_for_call = filtered_resources

    return await create_virtual_machines(
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=filtered_for_call,
        custom_fields=custom_fields,
        tag=tag,
        websocket=websocket,
        use_websocket=use_websocket,
        use_guest_agent_interface_name=use_guest_agent_interface_name,
        ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
    )


@router.get("/create-test")
async def create_test():
    """
    name:  DB-MASTER
    status:  active
    cluster:  1
    device:  29
    vcpus:  4
    memory:  4294
    disk:  34359
    tags:  [2]
    role:  786
    """

    virtual_machine = await asyncio.to_thread(
        lambda: VirtualMachine(
            name="DB-MASTER",
            status="active",
            cluster=1,
            device=29,
            vcpus=4,
            memory=4294,
            disk=34359,
            tags=[2],
            role=786,
            custom_fields={
                "proxmox_vm_id": 100,
                "proxmox_start_at_boot": True,
                "proxmox_unprivileged_container": False,
                "proxmox_qemu_agent": True,
                "proxmox_search_domain": "example.com",
            },
        )
    )

    return virtual_machine


@router.get("/create")
async def create_virtual_machines(  # noqa: C901
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket=None,
    use_css: bool = False,
    use_websocket: bool = False,
    sync_vm_network: bool = True,
    use_guest_agent_interface_name: bool = Query(
        default=True,
        title="Use Guest Agent Interface Name",
        description=(
            "When true and QEMU guest-agent data is available, VM interface names "
            "are created from guest-agent interface names instead of netX/nicX labels."
        ),
    ),
    netbox_vm_ids: str | None = Query(
        default=None,
        title="NetBox VM IDs",
        description="Comma-separated list of NetBox VM IDs to sync. When provided, only these VMs will be synced.",
    ),
    ignore_ipv6_link_local_addresses: bool = Query(
        default=True,
        title="Ignore IPv6 Link-Local Addresses",
        description=(
            "When true, IPv6 link-local addresses (fe80::/64) are ignored during "
            "VM interface IP address selection. Disable only if you need link-local addresses included."
        ),
    ),
):
    """Create and synchronize virtual machines from Proxmox to NetBox.

    Discovers virtual machines in Proxmox cluster resources and creates or updates
    corresponding NetBox VM objects with network interfaces, disks, and metadata.

    Args:
        netbox_session: NetBox API session for creating/updating VMs.
        pxs: Proxmox session(s) for fetching VM configurations.
        cluster_status: Cluster status objects containing node and resource information.
        cluster_resources: Proxmox cluster resources from Proxmox (VMs, LXC containers).
        custom_fields: Custom field configurations for NetBox.
        tag: ProxBox tag reference for tagging created objects.
        websocket: Optional WebSocket connection for streaming progress updates.
        use_css: Whether to include CSS styling in HTML status responses.
        use_websocket: Whether to send progress updates via WebSocket/SSE.
        sync_vm_network: When False, skip VM interface and IP reconciliation in this pass.
        use_guest_agent_interface_name: Use QEMU guest-agent interface names if available.
        netbox_vm_ids: Comma-separated NetBox VM IDs to filter sync.
        ignore_ipv6_link_local_addresses: Ignore IPv6 link-local addresses when selecting IPs.

    Returns:
        HTTP response with creation status, or streaming SSE response if using WebSocket.
    """

    filtered_cluster_resources = cluster_resources
    bridge: WebSocketSSEBridge | None = (
        websocket if isinstance(websocket, WebSocketSSEBridge) else None
    )

    if netbox_vm_ids and isinstance(netbox_vm_ids, str):
        vm_ids = parse_comma_separated_ints(netbox_vm_ids)
        if vm_ids:
            filtered_cluster_resources = await _filter_cluster_resources_by_netbox_vm_ids(
                netbox_session=netbox_session,
                cluster_resources=cluster_resources,
                netbox_vm_ids=vm_ids,
            )

    nb = netbox_session

    total_vms = 0  # Track total VMs processed
    successful_vms = 0  # Track successful VM creations
    failed_vms = 0  # Track failed VM creations
    tag_id = int(getattr(tag, "id", 0) or 0)
    tag_refs = [
        {
            "name": getattr(tag, "name", None),
            "slug": getattr(tag, "slug", None),
            "color": getattr(tag, "color", None),
        }
    ]
    tag_refs = [tag_ref for tag_ref in tag_refs if tag_ref.get("name") and tag_ref.get("slug")]
    flattened_results = []
    storage_index: dict[tuple[str, str], dict] = {}
    cluster_dependency_cache: dict[str, dict[str, object]] = {}
    node_device_cache: dict[tuple[str, str], object] = {}
    vm_role_cache: dict[str, object] = {}
    vm_role_mapping: dict[str, dict[str, object]] = {
        "qemu": {
            "name": "Virtual Machine (QEMU)",
            "slug": "virtual-machine-qemu",
            "color": "00ffff",
            "description": "Proxmox Virtual Machine",
            "tags": [tag_id],
            "vm_role": True,
        },
        "lxc": {
            "name": "Container (LXC)",
            "slug": "container-lxc",
            "color": "7fffd4",
            "description": "Proxmox LXC Container",
            "tags": [tag_id],
            "vm_role": True,
        },
        "undefined": {
            "name": "Unknown",
            "slug": "unknown",
            "color": "000000",
            "description": "VM Type not found. Neither QEMU nor LXC.",
            "tags": [tag_id],
            "vm_role": True,
        },
    }

    # Emit discovery event immediately if using bridge/SSE streaming.
    # This prevents the stream consumer from hanging while waiting for the first event.
    if bridge:
        vm_items: list[dict[str, object]] = []
        for cluster in filtered_cluster_resources:
            if isinstance(cluster, dict):
                for cluster_name, resources in cluster.items():
                    if isinstance(resources, list):
                        for resource in resources:
                            if isinstance(resource, dict) and resource.get("type") in (
                                "qemu",
                                "lxc",
                            ):
                                vm_items.append(
                                    {
                                        "name": str(
                                            resource.get("name")
                                            or resource.get("vmid")
                                            or "unknown"
                                        ),
                                        "type": str(resource.get("type") or "unknown"),
                                        "cluster": str(cluster_name),
                                        "node": str(resource.get("node") or ""),
                                    }
                                )
        await bridge.emit_discovery(
            phase="virtual-machines",
            items=vm_items,
            message=f"Discovered {len(vm_items)} virtual machine(s) to synchronize",
            metadata={"sync_vm_network": sync_vm_network},
        )

    async def _precompute_vm_dependencies() -> None:
        """Ensure shared dependencies in strict parent-to-child order.

        Dependency chain enforced here:
        manufacturer -> device type -> cluster type -> cluster/site -> node device -> VM role.
        """

        resources_by_cluster: dict[str, list[dict]] = {}
        for cluster in filtered_cluster_resources:
            if not isinstance(cluster, dict):
                continue
            for candidate_cluster_name, resources in cluster.items():
                if not isinstance(resources, list):
                    continue
                vm_resources = [
                    resource
                    for resource in resources
                    if isinstance(resource, dict) and resource.get("type") in ("qemu", "lxc")
                ]
                if vm_resources:
                    resources_by_cluster[str(candidate_cluster_name)] = vm_resources

        # Nothing to precompute when no VM resources were discovered.
        if not resources_by_cluster:
            return

        manufacturer = await _ensure_manufacturer(nb, tag_refs=tag_refs)
        device_type = await _ensure_device_type(
            nb,
            manufacturer_id=getattr(manufacturer, "id", None),
            tag_refs=tag_refs,
        )
        device_role = await _ensure_proxmox_node_role(nb, tag_refs=tag_refs)

        vm_types: set[str] = set()

        for cluster_name, vm_resources in resources_by_cluster.items():
            cluster_mode = next(
                (
                    cluster_state.mode
                    for cluster_state in cluster_status
                    if getattr(cluster_state, "name", None) == cluster_name
                ),
                "cluster",
            )
            cluster_type = await _ensure_cluster_type(
                nb,
                mode=cluster_mode,
                tag_refs=tag_refs,
            )
            cluster = await _ensure_cluster(
                nb,
                cluster_name=cluster_name,
                cluster_type_id=getattr(cluster_type, "id", None),
                mode=cluster_mode,
                tag_refs=tag_refs,
            )
            site = await _ensure_site(nb, cluster_name=cluster_name, tag_refs=tag_refs)

            cluster_dependency_cache[cluster_name] = {
                "cluster": cluster,
                "site": site,
                "device_type": device_type,
                "device_role": device_role,
            }

            node_names = {
                str(resource.get("node"))
                for resource in vm_resources
                if resource.get("node") is not None
            }
            for node_name in sorted(node_names):
                node_device_cache[(cluster_name, node_name)] = await _ensure_device(
                    nb,
                    device_name=node_name,
                    cluster_id=getattr(cluster, "id", None),
                    device_type_id=getattr(device_type, "id", None),
                    role_id=getattr(device_role, "id", None),
                    site_id=getattr(site, "id", None),
                    tag_refs=tag_refs,
                )

            for resource in vm_resources:
                vm_type = str(resource.get("type") or "undefined").lower()
                if vm_type not in vm_role_mapping:
                    vm_type = "undefined"
                vm_types.add(vm_type)

        for vm_type in sorted(vm_types):
            role_payload = vm_role_mapping.get(vm_type, vm_role_mapping["undefined"])
            vm_role_cache[vm_type] = await rest_reconcile_async(
                nb,
                "/api/dcim/device-roles/",
                lookup={"slug": role_payload.get("slug")},
                payload={
                    **role_payload,
                    "tags": tag_refs,
                },
                schema=NetBoxDeviceRoleSyncState,
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "slug": record.get("slug"),
                    "color": record.get("color"),
                    "description": record.get("description"),
                    "vm_role": record.get("vm_role"),
                    "tags": record.get("tags"),
                },
            )

    try:
        storage_records = await rest_list_async(nb, "/api/plugins/proxbox/storage/")
        storage_index = build_storage_index(storage_records)
    except Exception as error:
        error_detail = getattr(error, "detail", str(error))
        error_msg = f"{type(error).__name__}: {error_detail}"
        logger.warning("Error loading storage records for VM sync: %s", error_msg)

    try:
        await _precompute_vm_dependencies()
    except Exception as error:
        raise ProxboxException(
            message="Error creating Virtual Machine dependent objects (cluster, device, tag and role)",
            python_exception=f"Error: {str(error)}",
        )

    async def _prepare_vm_state(cluster_name: str, resource: dict) -> _PreparedVMState:  # noqa: C901
        vm_type = str(resource.get("type") or "unknown")
        vm_type_key = vm_type.lower() if vm_type else "undefined"
        if vm_type_key not in vm_role_mapping:
            vm_type_key = "undefined"

        vm_config_result = get_vm_config(
            pxs=pxs,
            cluster_status=cluster_status,
            node=resource.get("node"),
            type=vm_type,
            vmid=resource.get("vmid"),
        )
        if inspect.isawaitable(vm_config_result):
            vm_config_result = await vm_config_result
        vm_config = vm_config_result or {}
        vm_config_obj = ProxmoxVmConfigInput.model_validate(vm_config)

        cluster_dependencies = cluster_dependency_cache.get(str(cluster_name), {})
        cluster = cluster_dependencies.get("cluster")
        if cluster is None:
            raise ProxboxException(
                message="Error creating Virtual Machine dependent objects (cluster, device, tag and role)",
                python_exception=(
                    f"Missing precomputed cluster dependency for cluster={cluster_name}"
                ),
            )

        node_name = str(resource.get("node"))
        device = node_device_cache.get((str(cluster_name), node_name))
        if device is None:
            device = await _ensure_device(
                nb,
                device_name=node_name,
                cluster_id=getattr(cluster, "id", None),
                device_type_id=getattr(cluster_dependencies.get("device_type"), "id", None),
                role_id=getattr(cluster_dependencies.get("device_role"), "id", None),
                site_id=getattr(cluster_dependencies.get("site"), "id", None),
                tag_refs=tag_refs,
            )
            node_device_cache[(str(cluster_name), node_name)] = device

        role = vm_role_cache.get(vm_type_key)
        if role is None:
            role_payload = vm_role_mapping.get(vm_type_key, vm_role_mapping["undefined"])
            role = await rest_reconcile_async(
                nb,
                "/api/dcim/device-roles/",
                lookup={"slug": role_payload.get("slug")},
                payload={
                    **role_payload,
                    "tags": tag_refs,
                },
                schema=NetBoxDeviceRoleSyncState,
                current_normalizer=lambda record: {
                    "name": record.get("name"),
                    "slug": record.get("slug"),
                    "color": record.get("color"),
                    "description": record.get("description"),
                    "vm_role": record.get("vm_role"),
                    "tags": record.get("tags"),
                },
            )
            vm_role_cache[vm_type_key] = role

        now = datetime.now(timezone.utc)
        desired_payload = build_netbox_virtual_machine_payload(
            proxmox_resource=resource,
            proxmox_config=vm_config,
            cluster_id=int(getattr(cluster, "id", 0) or 0),
            device_id=int(getattr(device, "id", 0) or 0),
            role_id=int(getattr(role, "id", 0) or 0),
            tag_ids=[int(getattr(tag, "id", 0) or 0)],
            last_updated=now,
        )
        lookup = {
            "cf_proxmox_vm_id": int(resource.get("vmid")),
            "cluster_id": int(getattr(cluster, "id", 0) or 0),
        }

        return _PreparedVMState(
            cluster_name=str(cluster_name),
            resource=resource,
            vm_config=vm_config,
            vm_config_obj=vm_config_obj,
            desired_payload=desired_payload,
            lookup=lookup,
            now=now,
            vm_type=vm_type,
        )

    async def _run_full_update_vm_batch() -> list[dict[str, object]]:  # noqa: C901
        operation_inputs: list[tuple[str, dict]] = []
        for cluster in filtered_cluster_resources:
            if not isinstance(cluster, dict):
                continue
            for cluster_name, resources in cluster.items():
                if not isinstance(resources, list):
                    continue
                for resource in resources:
                    if isinstance(resource, dict) and resource.get("type") in ("qemu", "lxc"):
                        operation_inputs.append((str(cluster_name), resource))

        if not operation_inputs:
            return []

        fetch_semaphore = asyncio.Semaphore(max(1, resolve_vm_sync_concurrency()))

        async def _prepare_with_limit(cluster_name: str, resource: dict):
            async with fetch_semaphore:
                return await _prepare_vm_state(cluster_name, resource)

        prepared_results = await asyncio.gather(
            *[
                _prepare_with_limit(cluster_name, resource)
                for cluster_name, resource in operation_inputs
            ],
            return_exceptions=True,
        )

        prepared_vms: list[_PreparedVMState] = []
        for prepared_result in prepared_results:
            if isinstance(prepared_result, Exception):
                logger.warning("VM preparation failed: %s", prepared_result)
                continue
            prepared_vms.append(prepared_result)

        if not prepared_vms:
            return []

        netbox_snapshot = await _load_netbox_virtual_machine_snapshot(nb)
        operation_queue = _build_vm_operation_queue(prepared_vms, netbox_snapshot)

        operation_counts: dict[str, int] = {"GET": 0, "CREATE": 0, "UPDATE": 0}
        for operation in operation_queue:
            operation_counts[operation.method] = operation_counts.get(operation.method, 0) + 1
        logger.info(
            "VM reconciliation queue prepared: GET=%s CREATE=%s UPDATE=%s",
            operation_counts["GET"],
            operation_counts["CREATE"],
            operation_counts["UPDATE"],
        )

        resolved_records = await _dispatch_vm_operation_queue(nb, operation_queue)

        results: list[dict[str, object]] = []
        for operation in operation_queue:
            vmid = int(operation.prepared.resource.get("vmid", 0) or 0)
            key = (operation.prepared.cluster_name, vmid)
            vm_record = resolved_records.get(key)
            if vm_record is None and operation.existing_record is not None:
                vm_record = operation.existing_record
            if vm_record is None:
                logger.warning(
                    "VM operation completed without resolved NetBox record: cluster=%s vmid=%s method=%s",
                    operation.prepared.cluster_name,
                    vmid,
                    operation.method,
                )
                continue
            results.append(vm_record)

            vm_id = _relation_id(vm_record.get("id"))
            if vm_id is None:
                continue
            try:
                await sync_virtual_machine_task_history(
                    netbox_session=nb,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    virtual_machine_id=vm_id,
                    vm_type=str(operation.prepared.vm_type or "unknown"),
                    cluster_name=operation.prepared.cluster_name,
                    tag_refs=tag_refs,
                    websocket=websocket,
                    use_websocket=use_websocket,
                )
            except Exception as error:
                logger.warning(
                    "Error syncing task history for VM %s (%s): %s",
                    operation.prepared.resource.get("name"),
                    operation.prepared.resource.get("vmid"),
                    error,
                )

        return results

    if not sync_vm_network:
        flattened_results = await _run_full_update_vm_batch()
        total_vms = len(flattened_results)
        successful_vms = len(flattened_results)
        failed_vms = 0
        if bridge:
            await bridge.emit_phase_summary(
                phase="virtual-machines",
                created=successful_vms,
                failed=failed_vms,
                message=(f"Virtual machine sync completed: {successful_vms} synchronized"),
            )
        if all([use_websocket, websocket]):
            await websocket.send_json({"object": "virtual_machine", "end": True})
        global_cache.clear_cache()
        logger.info(
            "VM sync summary: total=%s ok=%s failed=%s",
            total_vms,
            successful_vms,
            failed_vms,
        )
        return flattened_results

    async def create_vm_task(cluster_name, resource):  # noqa: C901
        undefined_html = return_status_html("undefined", use_css)

        websocket_vm_json: dict = {
            "sync_status": return_status_html("syncing", use_css),
            "name": undefined_html,
            "netbox_id": undefined_html,
            "status": undefined_html,
            "cluster": undefined_html,
            "device": undefined_html,
            "role": undefined_html,
            "vcpus": undefined_html,
            "memory": undefined_html,
            "disk": undefined_html,
            "vm_interfaces": undefined_html,
        }

        vm_type = resource.get("type", "unknown")
        vm_type_key = str(vm_type).lower() if vm_type else "undefined"
        if vm_type_key not in vm_role_mapping:
            vm_type_key = "undefined"
        vm_config_result = get_vm_config(
            pxs=pxs,
            cluster_status=cluster_status,
            node=resource.get("node"),
            type=vm_type,
            vmid=resource.get("vmid"),
        )
        if inspect.isawaitable(vm_config_result):
            vm_config_result = await vm_config_result
        vm_config = vm_config_result

        if vm_config is None:
            vm_config = {}
        vm_config_obj = ProxmoxVmConfigInput.model_validate(vm_config)

        initial_vm_json = websocket_vm_json | {
            "completed": False,
            "rowid": str(resource.get("name")),
            "name": str(resource.get("name")),
            "cluster": str(cluster_name),
            "device": str(resource.get("node")),
        }

        vm_name = str(resource.get("name") or resource.get("vmid") or "unknown")
        timing_key = f"vm_{cluster_name}_{resource.get('vmid')}"
        if bridge:
            bridge.start_timer(timing_key)
            await bridge.emit_item_progress(
                phase="virtual-machines",
                item={
                    "name": vm_name,
                    "type": str(resource.get("type") or "unknown"),
                    "cluster": str(cluster_name),
                    "node": str(resource.get("node") or ""),
                },
                operation=ItemOperation.CREATED,
                status="processing",
                message=f"Processing VM '{vm_name}'",
                progress_current=0,
                progress_total=0,
            )

        if all([use_websocket, websocket]):
            await websocket.send_json(
                {"object": "virtual_machine", "type": "create", "data": initial_vm_json}
            )

        try:
            if bridge:
                await bridge.emit_substep(
                    phase="virtual-machines",
                    substep="resolve_dependencies",
                    status=SubstepStatus.PROCESSING,
                    message=f"Resolving dependencies for VM '{vm_name}'",
                    item={"name": vm_name},
                )
            cluster_dependencies = cluster_dependency_cache.get(str(cluster_name), {})
            cluster = cluster_dependencies.get("cluster")

            if cluster is None:
                raise ProxboxException(
                    message=(
                        "Error creating Virtual Machine dependent objects "
                        "(cluster, device, tag and role)"
                    ),
                    python_exception=(
                        f"Missing precomputed cluster dependency for cluster={cluster_name}"
                    ),
                )

            node_name = str(resource.get("node"))
            device = node_device_cache.get((str(cluster_name), node_name))
            if device is None:
                # Fallback for edge cases where a node appears after preflight filtering.
                device = await _ensure_device(
                    nb,
                    device_name=node_name,
                    cluster_id=getattr(cluster, "id", None),
                    device_type_id=getattr(cluster_dependencies.get("device_type"), "id", None),
                    role_id=getattr(cluster_dependencies.get("device_role"), "id", None),
                    site_id=getattr(cluster_dependencies.get("site"), "id", None),
                    tag_refs=tag_refs,
                )
                node_device_cache[(str(cluster_name), node_name)] = device

            role = vm_role_cache.get(vm_type_key)
            if role is None:
                role_payload = vm_role_mapping.get(vm_type_key, vm_role_mapping["undefined"])
                role = await rest_reconcile_async(
                    nb,
                    "/api/dcim/device-roles/",
                    lookup={"slug": role_payload.get("slug")},
                    payload={
                        **role_payload,
                        "tags": tag_refs,
                    },
                    schema=NetBoxDeviceRoleSyncState,
                    current_normalizer=lambda record: {
                        "name": record.get("name"),
                        "slug": record.get("slug"),
                        "color": record.get("color"),
                        "description": record.get("description"),
                        "vm_role": record.get("vm_role"),
                        "tags": record.get("tags"),
                    },
                )
                vm_role_cache[vm_type_key] = role

            logger.debug("VM deps cluster=%s device=%s role=%s", cluster, device, role)
            if bridge:
                await bridge.emit_substep(
                    phase="virtual-machines",
                    substep="resolve_dependencies",
                    status=SubstepStatus.COMPLETED,
                    message=f"Dependencies ready for VM '{vm_name}'",
                    item={"name": vm_name},
                    timing_key=timing_key,
                )

        except Exception as error:
            if bridge:
                await bridge.emit_error_detail(
                    message="Failed to resolve VM dependencies",
                    category=ErrorCategory.VALIDATION,
                    phase="virtual-machines",
                    item={"name": vm_name},
                    detail=str(error),
                    suggestion="Check cluster, node device, and VM role mappings in NetBox",
                )
            raise ProxboxException(
                message="Error creating Virtual Machine dependent objects (cluster, device, tag and role)",
                python_exception=f"Error: {str(error)}",
            )

        # try:
        now = datetime.now(timezone.utc)
        netbox_vm_payload = build_netbox_virtual_machine_payload(
            proxmox_resource=resource,
            proxmox_config=vm_config,
            cluster_id=int(getattr(cluster, "id", 0) or 0),
            device_id=int(getattr(device, "id", 0) or 0),
            role_id=int(getattr(role, "id", 0) or 0),
            tag_ids=[int(getattr(tag, "id", 0) or 0)],
            last_updated=now,
        )

        if bridge:
            await bridge.emit_substep(
                phase="virtual-machines",
                substep="reconcile_vm",
                status=SubstepStatus.PROCESSING,
                message=f"Reconciling VM '{vm_name}' in NetBox",
                item={"name": vm_name},
            )

        virtual_machine = await rest_reconcile_async(
            nb,
            "/api/virtualization/virtual-machines/",
            lookup={
                "cf_proxmox_vm_id": int(resource.get("vmid")),
                "cluster_id": int(getattr(cluster, "id", 0) or 0),
            },
            payload=netbox_vm_payload,
            schema=NetBoxVirtualMachineCreateBody,
            current_normalizer=lambda record: {
                "name": record.get("name"),
                "status": record.get("status"),
                "cluster": record.get("cluster"),
                "device": record.get("device"),
                "role": record.get("role"),
                "vcpus": record.get("vcpus"),
                "memory": record.get("memory"),
                "disk": record.get("disk"),
                "tags": record.get("tags"),
                "custom_fields": record.get("custom_fields"),
                "description": record.get("description"),
            },
        )

        logger.debug("Reconciled virtual_machine=%s", virtual_machine)
        if bridge:
            await bridge.emit_substep(
                phase="virtual-machines",
                substep="reconcile_vm",
                status=SubstepStatus.COMPLETED,
                message=f"VM '{vm_name}' reconciled in NetBox",
                item={"name": vm_name},
                timing_key=timing_key,
            )

        """
        except ProxboxException:
            raise
        except Exception as error:
            raise ProxboxException(
                message="Error creating Virtual Machine in Netbox",
                python_exception=f"Error: {str(error)}"
            )
        """

        if not isinstance(virtual_machine, dict):
            virtual_machine = virtual_machine.dict()

        # Create VM interfaces
        netbox_vm_interfaces = []
        first_ip_id: int | None = None
        if virtual_machine and vm_config and sync_vm_network:
            guest_agent_interfaces: list[dict] = []
            if vm_type == "qemu" and vm_config_obj.qemu_agent_enabled:
                proxmox_session = next(
                    (
                        px
                        for px, cluster in zip(pxs, cluster_status)
                        if getattr(cluster, "name", None) == cluster_name
                    ),
                    None,
                )
                if proxmox_session is not None:
                    guest_agent_interfaces = get_qemu_guest_agent_network_interfaces(
                        proxmox_session,
                        node=str(resource.get("node")),
                        vmid=int(resource.get("vmid")),
                    )
                    if not guest_agent_interfaces:
                        logger.info(
                            "Guest agent network data unavailable for VM %s (vmid=%s); falling back to config networks.",
                            resource.get("name"),
                            resource.get("vmid"),
                        )

            guest_by_name = {
                str(iface.get("name", "")).strip().lower(): iface
                for iface in guest_agent_interfaces
            }
            guest_by_mac = {
                _normalized_mac(iface.get("mac_address")): iface
                for iface in guest_agent_interfaces
                if _normalized_mac(iface.get("mac_address"))
            }

            vm_networks = _parse_vm_networks(vm_config)

            if vm_networks:
                interface_tasks = []
                for network in vm_networks:
                    for interface_name, value in network.items():
                        config_interface_name = (
                            str(value.get("name", interface_name)).strip() or interface_name
                        )
                        interface_mac = value.get("virtio", value.get("hwaddr", None))
                        guest_iface = None
                        if interface_mac:
                            guest_iface = guest_by_mac.get(_normalized_mac(interface_mac))
                        if guest_iface is None:
                            guest_iface = guest_by_name.get(config_interface_name.lower())
                        resolved_interface_name = config_interface_name
                        if use_guest_agent_interface_name and guest_iface:
                            guest_name = str(guest_iface.get("name") or "").strip()
                            if guest_name:
                                resolved_interface_name = guest_name

                        interface_tasks.append(
                            _create_vm_interface_parallel(
                                nb=nb,
                                virtual_machine=virtual_machine,
                                interface_name=resolved_interface_name,
                                interface_config=value,
                                guest_iface=guest_iface,
                                tag_refs=tag_refs,
                                use_guest_agent_interface_name=use_guest_agent_interface_name,
                                ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
                                now=now,
                            )
                        )

                interface_results = await asyncio.gather(*interface_tasks, return_exceptions=True)
                for result in interface_results:
                    if isinstance(result, Exception):
                        error_detail = getattr(result, "detail", str(result))
                        error_msg = f"{type(result).__name__}: {error_detail}"
                        logger.warning("Interface creation failed: %s", error_msg)
                        continue
                    if result.get("interface"):
                        netbox_vm_interfaces.append(result["interface"])
                    ip_id = result.get("first_ip_id")
                    if ip_id and first_ip_id is None:
                        first_ip_id = ip_id

            disk_tasks = [
                _create_vm_disk_parallel(
                    nb=nb,
                    virtual_machine=virtual_machine,
                    disk_entry=disk_entry,
                    cluster_name=cluster_name,
                    storage_index=storage_index,
                    tag_refs=tag_refs,
                    now=now,
                )
                for disk_entry in vm_config_obj.disks
            ]
            if disk_tasks:
                await asyncio.gather(*disk_tasks, return_exceptions=True)

        # Set primary IP only when NetBox has no primary IP yet (user choice is preserved)
        vm_id = virtual_machine.get("id")
        if virtual_machine.get("primary_ip4") is None:
            if first_ip_id is not None:
                try:
                    await rest_patch_async(
                        nb,
                        "/api/virtualization/virtual-machines/",
                        vm_id,
                        {"primary_ip4": first_ip_id},
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to set primary_ip4 for VM %s (id=%s): %s",
                        virtual_machine.get("name"),
                        vm_id,
                        exc,
                    )
                    if websocket:
                        await websocket.send_json(
                            {
                                "object": "virtual_machine",
                                "data": {
                                    "error": f"Could not set primary IP: {exc}",
                                    "rowid": virtual_machine.get("name"),
                                },
                            }
                        )
            else:
                logger.info(
                    "No IP available for VM %s (vmid=%s), skipping primary_ip4 assignment.",
                    resource.get("name"),
                    resource.get("vmid"),
                )
                if websocket:
                    await websocket.send_json(
                        {
                            "object": "virtual_machine",
                            "data": {
                                "completed": True,
                                "status": "warning",
                                "warning": "No IP address found; primary IP not set.",
                                "rowid": virtual_machine.get("name"),
                            },
                        }
                    )

        try:
            task_history_count = await sync_virtual_machine_task_history(
                netbox_session=nb,
                pxs=pxs,
                cluster_status=cluster_status,
                virtual_machine_id=int(virtual_machine.get("id")),
                vm_type=str(vm_type or "unknown"),
                cluster_name=cluster_name,
                tag_refs=tag_refs,
                websocket=websocket,
                use_websocket=use_websocket,
            )
            logger.debug(
                "Synced %s task history records for VM %s",
                task_history_count,
                resource.get("name"),
            )
        except Exception as error:
            logger.warning(
                "Error syncing task history for VM %s (%s): %s",
                resource.get("name"),
                resource.get("vmid"),
                error,
            )

        if bridge:
            await bridge.emit_item_progress(
                phase="virtual-machines",
                item={
                    "name": vm_name,
                    "type": str(resource.get("type") or "unknown"),
                    "cluster": str(cluster_name),
                    "node": str(resource.get("node") or ""),
                    "netbox_id": virtual_machine.get("id"),
                    "netbox_url": virtual_machine.get("display_url"),
                },
                operation=ItemOperation.CREATED,
                status="completed",
                message=f"Synced VM '{vm_name}'",
                progress_current=0,
                progress_total=0,
                timing_key=timing_key,
            )
            bridge.clear_timer(timing_key)

        return virtual_machine

    max_concurrency = resolve_vm_sync_concurrency()
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _run_vm_task(cluster_name: str, resource: dict):
        async with semaphore:
            return await create_vm_task(cluster_name, resource)

    async def _create_cluster_vms(cluster: dict) -> list:
        """
        Create virtual machines for a cluster.

        Args:
            cluster: A dictionary containing cluster information.

        Returns:
            A list of virtual machine creation results.
        """

        tasks = []  # Collect coroutines
        for cluster_name, resources in cluster.items():
            for resource in resources:
                if resource.get("type") in ("qemu", "lxc"):
                    tasks.append(_run_vm_task(cluster_name, resource))

        return await asyncio.gather(*tasks, return_exceptions=True)  # Gather coroutines

    try:
        vm_items: list[dict[str, object]] = []
        # Process each cluster
        for cluster in filtered_cluster_resources:
            cluster_name = list(cluster.keys())[0]
            resources = cluster[cluster_name]
            vm_resources = [r for r in resources if r.get("type") in ("qemu", "lxc")]
            vm_count = len(vm_resources)

            for resource in vm_resources:
                vm_items.append(
                    {
                        "name": str(resource.get("name") or resource.get("vmid") or "unknown"),
                        "type": str(resource.get("type") or "unknown"),
                        "cluster": str(cluster_name),
                        "node": str(resource.get("node") or ""),
                    }
                )

            total_vms += vm_count

        if bridge:
            await bridge.emit_discovery(
                phase="virtual-machines",
                items=vm_items,
                message=f"Discovered {total_vms} virtual machine(s) to synchronize",
                metadata={"sync_vm_network": sync_vm_network},
            )

        # Return the created virtual machines.
        result_list = await asyncio.gather(
            *[_create_cluster_vms(cluster) for cluster in filtered_cluster_resources],
            return_exceptions=True,
        )

        logger.info(f"VM Creation Result list: {result_list}")
        for cluster_result in result_list:
            if isinstance(cluster_result, Exception):
                continue
            for result in cluster_result:
                if isinstance(result, Exception):
                    logger.warning(
                        "VM sub-task failed: %s",
                        getattr(result, "python_exception", str(result)),
                    )

        # Flatten the nested results and process them
        for cluster_results in result_list:
            if isinstance(cluster_results, Exception):
                failed_vms += 1
            else:
                # cluster_results is a list of VM creation results
                for vm_result in cluster_results:
                    if isinstance(vm_result, Exception):
                        failed_vms += 1
                    else:
                        successful_vms += 1
                        flattened_results.append(vm_result)

        if bridge:
            await bridge.emit_phase_summary(
                phase="virtual-machines",
                created=successful_vms,
                failed=failed_vms,
                message=(
                    f"Virtual machine sync completed: {successful_vms} synchronized, {failed_vms} failed"
                ),
            )

        # Send end message to websocket
        if all([use_websocket, websocket]):
            await websocket.send_json({"object": "virtual_machine", "end": True})

        # Clear cache after creating virtual machines
        global_cache.clear_cache()

        logger.info(
            "VM sync summary: total=%s ok=%s failed=%s",
            total_vms,
            successful_vms,
            failed_vms,
        )

    except Exception as error:
        error_msg = f"Error during VM sync: {str(error)}"
        if bridge:
            await bridge.emit_error_detail(
                message="Virtual machine sync failed",
                category=ErrorCategory.INTERNAL,
                phase="virtual-machines",
                detail=str(error),
                suggestion="Review backend logs and retry the synchronization",
            )
        raise ProxboxException(message=error_msg)

    return flattened_results


async def create_only_vm_interfaces(  # noqa: C901
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket=None,
    use_websocket: bool = False,
    use_guest_agent_interface_name: bool = True,
    ignore_ipv6_link_local_addresses: bool = True,
) -> list[dict]:
    """Sync VM interfaces only (no VM creation) with per-interface progress events.

    Args:
        netbox_session: NetBox session.
        pxs: Proxmox sessions.
        cluster_status: Cluster status from Proxmox.
        cluster_resources: Filtered cluster resources containing VMs.
        custom_fields: Custom field configs.
        tag: Proxbox tag reference.
        websocket: Optional bridge for SSE events.
        use_websocket: Whether to emit per-interface events.
        use_guest_agent_interface_name: Prefer guest-agent interface names.
        ignore_ipv6_link_local_addresses: Skip IPv6 link-local addresses.

    Returns:
        List of synced interface records.
    """
    from proxbox_api.services.sync.network import sync_vm_interface_and_ip

    nb = netbox_session
    tag_refs = [
        {
            "name": getattr(tag, "name", None),
            "slug": getattr(tag, "slug", None),
            "color": getattr(tag, "color", None),
        }
    ]
    tag_refs = [t for t in tag_refs if t.get("name") and t.get("slug")]
    now = datetime.now(timezone.utc)
    results: list[dict] = []

    async def _sync_vm_interfaces(cluster_name: str, resource: dict) -> list[dict]:  # noqa: C901
        cluster_name_str = str(cluster_name)
        resource_node = str(resource.get("node", ""))
        vm_type = resource.get("type", "unknown")
        vm_name = str(resource.get("name", "")).strip()

        vm_record = None
        for cluster in cluster_resources:
            if not isinstance(cluster, dict):
                continue
            for c_name, c_resources in cluster.items():
                for r in c_resources:
                    if r.get("name", "").strip() == vm_name and r.get("type") == vm_type:
                        vm_record = r
                        break

        if not vm_record:
            return []

        vmid = resource.get("vmid")
        if vmid is None:
            return []

        netbox_vm = await _resolve_netbox_virtual_machine_by_proxmox_id(nb, vmid)
        if not netbox_vm:
            logger.warning(
                "Skipping VM interface sync for %s (vmid=%s): NetBox VM not found",
                vm_name,
                vmid,
            )
            return []

        proxmox_session = next(
            (
                px
                for px, cs in zip(pxs, cluster_status)
                if getattr(cs, "name", None) == cluster_name_str
            ),
            None,
        )

        vm_config: dict[str, object] = {}
        try:
            if proxmox_session and resource_node:
                vm_config_result = get_vm_config(
                    pxs=pxs,
                    cluster_status=cluster_status,
                    node=resource_node,
                    type=vm_type,
                    vmid=int(vmid),
                )
                if inspect.isawaitable(vm_config_result):
                    vm_config_result = await vm_config_result
                vm_config = vm_config_result or {}
        except Exception as exc:
            logger.warning("Could not fetch VM config for %s (vmid=%s): %s", vm_name, vmid, exc)

        guest_agent_interfaces: list[dict[str, object]] = []
        if vm_type == "qemu" and vm_config.get("agent"):
            if proxmox_session and resource_node:
                guest_agent_interfaces = (
                    get_qemu_guest_agent_network_interfaces(
                        proxmox_session, resource_node, int(vmid)
                    )
                    or []
                )

        guest_by_name = {
            str(iface.get("name", "")).strip().lower(): iface for iface in guest_agent_interfaces
        }
        guest_by_mac = {
            _normalized_mac(iface.get("mac_address")): iface
            for iface in guest_agent_interfaces
            if _normalized_mac(iface.get("mac_address"))
        }

        vm_networks = _parse_vm_networks(vm_config)

        interfaces_synced: list[dict] = []

        for network in vm_networks:
            for iface_name, config_dict in network.items():
                config_interface_name = (
                    str(config_dict.get("name", iface_name)).strip() or iface_name
                )
                interface_mac = config_dict.get("virtio") or config_dict.get("hwaddr")
                guest_iface = None
                if interface_mac:
                    guest_iface = guest_by_mac.get(_normalized_mac(interface_mac))
                if guest_iface is None:
                    guest_iface = guest_by_name.get(config_interface_name.lower())

                resolved_name = config_interface_name
                if use_guest_agent_interface_name and guest_iface:
                    guest_name = str(guest_iface.get("name") or "").strip()
                    if guest_name:
                        resolved_name = guest_name

                if use_websocket and websocket:
                    await websocket.send_json(
                        {
                            "object": "vm_interface",
                            "data": {
                                "completed": False,
                                "sync_status": "syncing",
                                "rowid": resolved_name,
                                "name": resolved_name,
                                "vm": vm_name,
                            },
                        }
                    )

                try:
                    result = await sync_vm_interface_and_ip(
                        nb=nb,
                        virtual_machine={
                            "id": netbox_vm.get("id"),
                            "name": netbox_vm.get("name") or vm_name,
                        },
                        interface_name=resolved_name,
                        interface_config=config_dict,
                        guest_iface=guest_iface,
                        tag_refs=tag_refs,
                        use_guest_agent_interface_name=use_guest_agent_interface_name,
                        create_ip=False,
                        ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
                        now=now,
                    )
                    interfaces_synced.append(result)

                    if use_websocket and websocket:
                        await websocket.send_json(
                            {
                                "object": "vm_interface",
                                "data": {
                                    "completed": True,
                                    "rowid": resolved_name,
                                    "name": resolved_name,
                                    "vm": vm_name,
                                    "netbox_id": result.get("id"),
                                    "mac_address": result.get("mac_address"),
                                    "ip_address": result.get("ip_address"),
                                },
                            }
                        )
                except Exception as exc:
                    error_detail = getattr(exc, "detail", str(exc))
                    error_msg = f"{type(exc).__name__}: {error_detail}"
                    logger.warning(
                        "Failed to sync interface %s for VM %s: %s",
                        resolved_name,
                        vm_name,
                        error_msg,
                    )
                    if use_websocket and websocket:
                        await websocket.send_json(
                            {
                                "object": "vm_interface",
                                "data": {
                                    "completed": False,
                                    "rowid": resolved_name,
                                    "name": resolved_name,
                                    "vm": vm_name,
                                    "error": str(exc),
                                },
                            }
                        )

        return interfaces_synced

    max_concurrency = resolve_vm_sync_concurrency()
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _run_task(cluster_name: str, resource: dict) -> list[dict]:
        async with semaphore:
            return await _sync_vm_interfaces(cluster_name, resource)

    async def _create_cluster_tasks(cluster: dict) -> list:
        tasks = []
        for cluster_name, resources in cluster.items():
            for resource in resources:
                if resource.get("type") in ("qemu", "lxc"):
                    tasks.append(_run_task(cluster_name, resource))
        return await asyncio.gather(*tasks, return_exceptions=True)

    try:
        for cluster in cluster_resources:
            cluster_results = await _create_cluster_tasks(cluster)
            for cluster_result in cluster_results:
                if isinstance(cluster_result, Exception):
                    continue
                for result in cluster_result:
                    if isinstance(result, Exception):
                        continue
                    results.append(result)
    except Exception as exc:
        error_detail = getattr(exc, "detail", str(exc))
        error_msg = f"{type(exc).__name__}: {error_detail}"
        logger.warning("Error during VM interfaces sync: %s", error_msg)

    if use_websocket and websocket:
        await websocket.send_json({"object": "vm_interface", "end": True})

    return results


async def create_only_vm_ip_addresses(  # noqa: C901
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    websocket=None,
    use_websocket: bool = False,
    use_guest_agent_interface_name: bool = True,
    ignore_ipv6_link_local_addresses: bool = True,
) -> list[dict]:
    """Sync VM IP addresses and primary IP assignment.

    This function resolves the existing VM interfaces created by the interface
    sync stage, assigns the best discovered IPs to them, and promotes the first
    IP to primary IP when available.

    Args:
        netbox_session: NetBox session.
        pxs: Proxmox sessions.
        cluster_status: Cluster status from Proxmox.
        cluster_resources: Filtered cluster resources containing VMs.
        custom_fields: Custom field configs.
        tag: Proxbox tag reference.
        websocket: Optional bridge for SSE events.
        use_websocket: Whether to emit per-IP events.
        use_guest_agent_interface_name: Prefer guest-agent interface names.
        ignore_ipv6_link_local_addresses: Skip IPv6 link-local addresses.

    Returns:
        List of synced IP address records.
    """
    from proxbox_api.services.sync.network import sync_vm_interface_and_ip
    from proxbox_api.services.sync.vm_network import set_primary_ip

    nb = netbox_session
    tag_refs = [
        {
            "name": getattr(tag, "name", None),
            "slug": getattr(tag, "slug", None),
            "color": getattr(tag, "color", None),
        }
    ]
    tag_refs = [t for t in tag_refs if t.get("name") and t.get("slug")]
    now = datetime.now(timezone.utc)
    results: list[dict] = []

    async def _sync_vm_ips(cluster_name: str, resource: dict) -> list[dict]:  # noqa: C901
        cluster_name_str = str(cluster_name)
        resource_node = str(resource.get("node", ""))
        vm_type = resource.get("type", "unknown")
        vm_name = str(resource.get("name", "")).strip()

        vmid = resource.get("vmid")
        if vmid is None:
            return []

        netbox_vm = await _resolve_netbox_virtual_machine_by_proxmox_id(nb, vmid)
        if not netbox_vm:
            logger.warning(
                "Skipping VM IP sync for %s (vmid=%s): NetBox VM not found",
                vm_name,
                vmid,
            )
            return []

        proxmox_session = next(
            (
                px
                for px, cs in zip(pxs, cluster_status)
                if getattr(cs, "name", None) == cluster_name_str
            ),
            None,
        )

        vm_config: dict[str, object] = {}
        try:
            if proxmox_session and resource_node:
                vm_config_result = get_vm_config(
                    pxs=pxs,
                    cluster_status=cluster_status,
                    node=resource_node,
                    type=vm_type,
                    vmid=int(vmid),
                )
                if inspect.isawaitable(vm_config_result):
                    vm_config_result = await vm_config_result
                vm_config = vm_config_result or {}
        except Exception as exc:
            logger.warning(
                "Could not fetch VM config for IP sync %s (vmid=%s): %s", vm_name, vmid, exc
            )

        guest_agent_interfaces: list[dict[str, object]] = []
        if vm_type == "qemu" and vm_config.get("agent"):
            if proxmox_session and resource_node:
                guest_agent_interfaces = (
                    get_qemu_guest_agent_network_interfaces(
                        proxmox_session, resource_node, int(vmid)
                    )
                    or []
                )

        guest_by_name = {
            str(iface.get("name", "")).strip().lower(): iface for iface in guest_agent_interfaces
        }
        guest_by_mac = {
            _normalized_mac(iface.get("mac_address")): iface
            for iface in guest_agent_interfaces
            if _normalized_mac(iface.get("mac_address"))
        }

        vm_networks = _parse_vm_networks(vm_config)

        ips_synced: list[dict] = []
        first_ip_id: int | None = None

        for network in vm_networks:
            for iface_name, config_dict in network.items():
                config_interface_name = (
                    str(config_dict.get("name", iface_name)).strip() or iface_name
                )
                interface_mac = config_dict.get("virtio") or config_dict.get("hwaddr")
                guest_iface = None
                if interface_mac:
                    guest_iface = guest_by_mac.get(_normalized_mac(interface_mac))
                if guest_iface is None:
                    guest_iface = guest_by_name.get(config_interface_name.lower())

                resolved_name = config_interface_name
                if use_guest_agent_interface_name and guest_iface:
                    guest_name = str(guest_iface.get("name") or "").strip()
                    if guest_name:
                        resolved_name = guest_name

                if use_websocket and websocket:
                    await websocket.send_json(
                        {
                            "object": "vm_ip",
                            "data": {
                                "completed": False,
                                "sync_status": "syncing",
                                "rowid": resolved_name,
                                "name": resolved_name,
                                "vm": vm_name,
                            },
                        }
                    )

                try:
                    result = await sync_vm_interface_and_ip(
                        nb=nb,
                        virtual_machine={
                            "id": netbox_vm.get("id"),
                            "name": netbox_vm.get("name") or vm_name,
                        },
                        interface_name=resolved_name,
                        interface_config=config_dict,
                        guest_iface=guest_iface,
                        tag_refs=tag_refs,
                        use_guest_agent_interface_name=use_guest_agent_interface_name,
                        create_interface=False,
                        ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
                        now=now,
                    )
                    if result.get("ip_id"):
                        if first_ip_id is None:
                            first_ip_id = result.get("ip_id")
                        ips_synced.append(
                            {
                                "ip_id": result.get("ip_id"),
                                "address": result.get("ip_address"),
                                "interface_name": resolved_name,
                                "interface_id": result.get("id"),
                                "vm": vm_name,
                            }
                        )

                    if use_websocket and websocket:
                        await websocket.send_json(
                            {
                                "object": "vm_ip",
                                "data": {
                                    "completed": True,
                                    "rowid": resolved_name,
                                    "name": resolved_name,
                                    "vm": vm_name,
                                    "ip_id": result.get("ip_id"),
                                    "address": result.get("ip_address"),
                                },
                            }
                        )
                except Exception as exc:
                    logger.warning(
                        "Failed to sync IP for VM %s interface %s: %s",
                        vm_name,
                        resolved_name,
                        exc,
                    )
                    if use_websocket and websocket:
                        await websocket.send_json(
                            {
                                "object": "vm_ip",
                                "data": {
                                    "completed": False,
                                    "rowid": resolved_name,
                                    "name": resolved_name,
                                    "vm": vm_name,
                                    "error": str(exc),
                                },
                            }
                        )

        if first_ip_id is not None:
            await set_primary_ip(
                nb=nb,
                virtual_machine=netbox_vm,
                primary_ip_id=first_ip_id,
            )

        return ips_synced

    max_concurrency = resolve_vm_sync_concurrency()
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _run_task(cluster_name: str, resource: dict) -> list[dict]:
        async with semaphore:
            return await _sync_vm_ips(cluster_name, resource)

    async def _create_cluster_tasks(cluster: dict) -> list:
        tasks = []
        for cluster_name, resources in cluster.items():
            for resource in resources:
                if resource.get("type") in ("qemu", "lxc"):
                    tasks.append(_run_task(cluster_name, resource))
        return await asyncio.gather(*tasks, return_exceptions=True)

    try:
        for cluster in cluster_resources:
            cluster_results = await _create_cluster_tasks(cluster)
            for cluster_result in cluster_results:
                if isinstance(cluster_result, Exception):
                    continue
                for result in cluster_result:
                    if isinstance(result, Exception):
                        continue
                    results.append(result)
    except Exception as exc:
        error_detail = getattr(exc, "detail", str(exc))
        error_msg = f"{type(exc).__name__}: {error_detail}"
        logger.warning("Error during VM IP address sync: %s", error_msg)

    if use_websocket and websocket:
        await websocket.send_json({"object": "vm_ip", "end": True})

    return results


@router.get("/{netbox_vm_id}/create")
async def create_virtual_machine_by_netbox_id(
    netbox_vm_id: int,
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    use_guest_agent_interface_name: bool = Query(
        default=True,
        title="Use Guest Agent Interface Name",
        description=(
            "When true and QEMU guest-agent data is available, VM interface names "
            "are created from guest-agent interface names instead of netX/nicX labels."
        ),
    ),
    ignore_ipv6_link_local_addresses: bool = Query(
        default=True,
        title="Ignore IPv6 Link-Local Addresses",
        description=(
            "When true, IPv6 link-local addresses (fe80::/64) are ignored during "
            "VM interface IP address selection. Disable only if you need link-local addresses included."
        ),
    ),
):
    return await _create_virtual_machine_by_netbox_id(
        netbox_vm_id=netbox_vm_id,
        netbox_session=netbox_session,
        pxs=pxs,
        cluster_status=cluster_status,
        cluster_resources=cluster_resources,
        custom_fields=custom_fields,
        tag=tag,
        use_guest_agent_interface_name=use_guest_agent_interface_name,
        ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
    )


@router.get("/create/stream", response_model=None)
async def create_virtual_machines_stream(
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    use_guest_agent_interface_name: bool = Query(
        default=True,
        title="Use Guest Agent Interface Name",
        description=(
            "When true and QEMU guest-agent data is available, VM interface names "
            "are created from guest-agent interface names instead of netX/nicX labels."
        ),
    ),
    netbox_vm_ids: str | None = Query(
        default=None,
        title="NetBox VM IDs",
        description="Comma-separated list of NetBox VM IDs to sync. When provided, only these VMs will be synced.",
    ),
    ignore_ipv6_link_local_addresses: bool = Query(
        default=True,
        title="Ignore IPv6 Link-Local Addresses",
        description=(
            "When true, IPv6 link-local addresses (fe80::/64) are ignored during "
            "VM interface IP address selection. Disable only if you need link-local addresses included."
        ),
    ),
):
    filtered_cluster_resources = cluster_resources
    vm_ids: list[int] = []

    if netbox_vm_ids:
        vm_ids = parse_comma_separated_ints(netbox_vm_ids)
        if vm_ids:
            filtered_cluster_resources = await _filter_cluster_resources_by_netbox_vm_ids(
                netbox_session=netbox_session,
                cluster_resources=cluster_resources,
                netbox_vm_ids=vm_ids,
            )

    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await create_virtual_machines(
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    cluster_resources=filtered_cluster_resources,
                    custom_fields=custom_fields,
                    tag=tag,
                    websocket=bridge,
                    use_websocket=True,
                    use_guest_agent_interface_name=use_guest_agent_interface_name,
                    ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        try:
            yield sse_event(
                "step",
                {
                    "step": "virtual-machines",
                    "status": "started",
                    "message": "Starting virtual machines synchronization."
                    if not vm_ids
                    else f"Starting virtual machines synchronization for {len(vm_ids)} VM(s).",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame

            result = await sync_task
            yield sse_event(
                "step",
                {
                    "step": "virtual-machines",
                    "status": "completed",
                    "message": "Virtual machines synchronization finished.",
                    "result": {"count": len(result)},
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Virtual machines sync completed.",
                    "result": {"count": len(result)},
                },
            )
        except Exception as error:
            if not sync_task.done():
                sync_task.cancel()
                try:
                    await sync_task
                except asyncio.CancelledError:
                    pass
            yield sse_event(
                "error",
                {
                    "step": "virtual-machines",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Virtual machines sync failed.",
                    "errors": [{"detail": str(error)}],
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{netbox_vm_id}/create/stream", response_model=None)
async def create_virtual_machine_by_netbox_id_stream(
    netbox_vm_id: int,
    netbox_session: NetBoxSessionDep,
    pxs: ProxmoxSessionsDep,
    cluster_status: ClusterStatusDep,
    cluster_resources: ClusterResourcesDep,
    custom_fields: CreateCustomFieldsDep,
    tag: ProxboxTagDep,
    use_guest_agent_interface_name: bool = Query(
        default=True,
        title="Use Guest Agent Interface Name",
        description=(
            "When true and QEMU guest-agent data is available, VM interface names "
            "are created from guest-agent interface names instead of netX/nicX labels."
        ),
    ),
    ignore_ipv6_link_local_addresses: bool = Query(
        default=True,
        title="Ignore IPv6 Link-Local Addresses",
        description=(
            "When true, IPv6 link-local addresses (fe80::/64) are ignored during "
            "VM interface IP address selection. Disable only if you need link-local addresses included."
        ),
    ),
):
    async def event_stream():
        bridge = WebSocketSSEBridge()

        async def _run_sync():
            try:
                return await _create_virtual_machine_by_netbox_id(
                    netbox_vm_id=netbox_vm_id,
                    netbox_session=netbox_session,
                    pxs=pxs,
                    cluster_status=cluster_status,
                    cluster_resources=cluster_resources,
                    custom_fields=custom_fields,
                    tag=tag,
                    websocket=bridge,
                    use_websocket=True,
                    use_guest_agent_interface_name=use_guest_agent_interface_name,
                    ignore_ipv6_link_local_addresses=ignore_ipv6_link_local_addresses,
                )
            finally:
                await bridge.close()

        sync_task = asyncio.create_task(_run_sync())
        try:
            yield sse_event(
                "step",
                {
                    "step": "virtual-machine",
                    "status": "started",
                    "message": f"Starting virtual machine synchronization for id={netbox_vm_id}.",
                },
            )
            async for frame in bridge.iter_sse():
                yield frame

            result = await sync_task
            yield sse_event(
                "step",
                {
                    "step": "virtual-machine",
                    "status": "completed",
                    "message": "Virtual machine synchronization finished.",
                    "result": {"count": len(result)},
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": True,
                    "message": "Virtual machine sync completed.",
                    "result": {"count": len(result)},
                },
            )
        except HTTPException as error:
            yield sse_event(
                "error",
                {
                    "step": "virtual-machine",
                    "status": "failed",
                    "error": str(error.detail),
                    "detail": str(error.detail),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Virtual machine sync failed.",
                    "errors": [{"detail": str(error.detail)}],
                },
            )
        except Exception as error:
            yield sse_event(
                "error",
                {
                    "step": "virtual-machine",
                    "status": "failed",
                    "error": str(error),
                    "detail": str(error),
                },
            )
            yield sse_event(
                "complete",
                {
                    "ok": False,
                    "message": "Virtual machine sync failed.",
                    "errors": [{"detail": str(error)}],
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
