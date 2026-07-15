"""NetBox custom-field inventory and reconciliation helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from proxbox_api import netbox_rest
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import ReconcileResult
from proxbox_api.proxmox_to_netbox.models import NetBoxCustomFieldSyncState
from proxbox_api.runtime_settings import get_float
from proxbox_api.utils.retry import is_netbox_overwhelmed_error

CUSTOM_FIELD_INVENTORY: tuple[dict[str, object], ...] = (
    {
        "object_types": ["virtualization.virtualmachine"],
        "type": "integer",
        "name": "proxmox_vm_id",
        "label": "VM ID",
        "description": "Proxmox Virtual Machine or Container ID",
        "ui_visible": "always",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["virtualization.virtualmachine"],
        "type": "text",
        "name": "proxmox_vm_type",
        "label": "VM Type",
        "description": "Proxmox VM type (qemu or lxc)",
        "ui_visible": "always",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["virtualization.virtualmachine"],
        "type": "boolean",
        "name": "proxmox_start_at_boot",
        "label": "Start at Boot",
        "description": "Proxmox Start at Boot Option",
        "ui_visible": "always",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["virtualization.virtualmachine"],
        "type": "boolean",
        "name": "proxmox_unprivileged_container",
        "label": "Unprivileged Container",
        "description": "Proxmox Unprivileged Container",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["virtualization.virtualmachine"],
        "type": "boolean",
        "name": "proxmox_qemu_agent",
        "label": "QEMU Guest Agent",
        "description": "Proxmox QEMU Guest Agent",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["virtualization.virtualmachine"],
        "type": "text",
        "name": "proxmox_search_domain",
        "label": "Search Domain",
        "description": "Proxmox Search Domain",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "virtualization.virtualmachine",
        ],
        "type": "url",
        "name": "proxmox_link",
        "label": "Proxmox Link",
        "description": "Link to Proxmox web interface",
        "ui_visible": "always",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "virtualization.virtualmachine",
        ],
        "type": "text",
        "name": "proxmox_node",
        "label": "Proxmox Node",
        "description": "Proxmox node hosting this device/VM",
        "ui_visible": "always",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "virtualization.virtualmachine",
        ],
        "type": "text",
        "name": "proxmox_cluster",
        "label": "Proxmox Cluster",
        "description": "Proxmox cluster name",
        "ui_visible": "always",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["virtualization.virtualmachine"],
        "type": "text",
        "name": "proxmox_status",
        "label": "Proxmox Status",
        "description": "Current status in Proxmox",
        "ui_visible": "always",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["virtualization.virtualmachine"],
        "type": "integer",
        "name": "proxmox_uptime",
        "label": "Uptime (seconds)",
        "description": "VM uptime in seconds",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "virtualization.virtualmachine",
        ],
        "type": "text",
        "name": "proxmox_tags",
        "label": "Proxmox Tags",
        "description": "Comma-separated tags from Proxmox",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "virtualization.virtualmachine",
        ],
        "type": "text",
        "name": "proxmox_os",
        "label": "Operating System",
        "description": "Operating system from Proxmox",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "virtualization.virtualmachine",
        ],
        "type": "text",
        "name": "proxmox_storage",
        "label": "Storage",
        "description": "Storage disk size",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "virtualization.virtualmachine",
        ],
        "type": "text",
        "name": "proxmox_disk",
        "label": "Disk (GB)",
        "description": "Total disk size in GB",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "virtualization.virtualmachine",
        ],
        "type": "text",
        "name": "proxmox_interfaces",
        "label": "Network Interfaces",
        "description": "Network interface count",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["ipam.ipaddress"],
        "type": "text",
        "name": "proxmox_interface",
        "label": "Proxmox Interface",
        "description": "Proxmox network interface name",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "virtualization.virtualmachine",
        ],
        "type": "text",
        "name": "proxmox_vmid",
        "label": "Proxmox VMID",
        "description": "VM ID for reference",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["ipam.ipaddress"],
        "type": "text",
        "name": "proxmox_mac",
        "label": "Proxmox MAC",
        "description": "MAC address from Proxmox",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "virtualization.virtualmachine",
        ],
        "type": "text",
        "name": "proxmox_notes",
        "label": "Proxmox Notes",
        "description": "Notes from Proxmox",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["ipam.vlan"],
        "type": "integer",
        "name": "proxmox_vlan_id",
        "label": "Proxmox VLAN ID",
        "description": "VLAN ID from Proxmox",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "virtualization.cluster",
            "virtualization.clustergroup",
        ],
        "type": "text",
        "name": "proxmox_cluster_name",
        "label": "Cluster Name",
        "description": "Cluster name from Proxmox",
        "ui_visible": "always",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "virtualization.cluster",
            "virtualization.clustergroup",
        ],
        "type": "text",
        "name": "proxmox_cluster_status",
        "label": "Cluster Status",
        "description": "Cluster status from Proxmox",
        "ui_visible": "always",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "virtualization.virtualmachine",
        ],
        "type": "text",
        "name": "proxmox_tcp_states",
        "label": "TCP States",
        "description": "TCP connection states",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "virtualization.virtualmachine",
        ],
        "type": "text",
        "name": "proxmox_cpu_type",
        "label": "CPU Type",
        "description": "CPU type from Proxmox",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["ipam.ipaddress"],
        "type": "text",
        "name": "proxmox_ip_addresses",
        "label": "IP Addresses",
        "description": "All IP addresses from Proxmox",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "virtualization.cluster",
        ],
        "type": "integer",
        "name": "proxmox_cluster_id",
        "label": "Cluster ID",
        "description": "Proxmox cluster ID",
        "ui_visible": "always",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "virtualization.virtualmachine",
        ],
        "type": "text",
        "name": "proxmox_storage_ids",
        "label": "Storage IDs",
        "description": "Comma-separated storage IDs",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "virtualization.virtualmachine",
        ],
        "type": "text",
        "name": "proxmox_storage_names",
        "label": "Storage Names",
        "description": "Comma-separated storage names",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "virtualization.virtualmachine",
        ],
        "type": "text",
        "name": "proxmox_device_names",
        "label": "Device Names",
        "description": "Comma-separated device names",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["virtualization.virtualmachine"],
        "type": "integer",
        "name": "proxmox_migration_duration",
        "label": "Migration Duration",
        "description": "Migration duration in seconds",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["virtualization.virtualmachine"],
        "type": "text",
        "name": "proxmox_migration_type",
        "label": "Migration Type",
        "description": "Migration type (live / offline)",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["virtualization.virtualdisk"],
        "type": "object",
        "name": "proxbox_storage_id",
        "label": "Proxbox Storage",
        "related_object_type": "netbox_proxbox.proxmoxstorage",
        "description": "Proxmox storage hosting this virtual disk",
        "ui_visible": "always",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["virtualization.vminterface"],
        "type": "object",
        "name": "proxbox_bridge",
        "label": "Proxbox Bridge",
        "related_object_type": "dcim.interface",
        "description": "Node-level bridge interface (vmbr) used by this VM interface",
        "ui_visible": "always",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "dcim.devicerole",
            "dcim.devicetype",
            "dcim.interface",
            "dcim.manufacturer",
            "dcim.site",
            "ipam.ipaddress",
            "ipam.vlan",
            "virtualization.cluster",
            "virtualization.clustertype",
            "virtualization.virtualdisk",
            "virtualization.virtualmachine",
            "virtualization.vminterface",
        ],
        "type": "datetime",
        "name": "proxmox_last_updated",
        "label": "Last Updated",
        "description": "Proxmox Plugin last modified this object",
        "ui_visible": "always",
        "ui_editable": "hidden",
        "weight": 200,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": [
            "dcim.device",
            "virtualization.cluster",
            "virtualization.virtualmachine",
        ],
        "type": "text",
        "name": "proxbox_last_run_id",
        "label": "Last Run ID",
        "description": "UUID of the most recent Proxbox sync run that touched this object.",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 250,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxbox",
    },
    {
        "object_types": ["dcim.device"],
        "type": "text",
        "name": "hardware_chassis_serial",
        "label": "Chassis Serial",
        "description": "Chassis serial reported by dmidecode -t 3",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 300,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["dcim.device"],
        "type": "text",
        "name": "hardware_chassis_manufacturer",
        "label": "Chassis Manufacturer",
        "description": "Chassis manufacturer reported by dmidecode -t 3",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 300,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["dcim.device"],
        "type": "text",
        "name": "hardware_chassis_product",
        "label": "Chassis Product",
        "description": "System product name reported by dmidecode -t 1",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 300,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["dcim.interface"],
        "type": "integer",
        "name": "nic_speed_gbps",
        "label": "NIC Speed (Gbps)",
        "description": "Negotiated link speed reported by ethtool, in Gbps",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 300,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["dcim.interface"],
        "type": "text",
        "name": "nic_duplex",
        "label": "NIC Duplex",
        "description": "Duplex mode reported by ethtool",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 300,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["dcim.interface"],
        "type": "boolean",
        "name": "nic_link",
        "label": "NIC Link Up",
        "description": "Link-detected status reported by ethtool",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 300,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
    {
        "object_types": ["virtualization.virtualmachine"],
        "type": "integer",
        "name": "proxmox_endpoint_id",
        "label": "Proxmox Endpoint ID",
        "description": "proxbox-api ProxmoxEndpoint database ID for console access",
        "ui_visible": "if-set",
        "ui_editable": "hidden",
        "weight": 100,
        "filter_logic": "loose",
        "search_weight": 1000,
        "group_name": "Proxmox",
    },
)

_CUSTOM_FIELDS_CACHE: tuple[dict[str, object], ...] | None = None
_CUSTOM_FIELDS_LOCK = asyncio.Lock()


def _copy_payload(payload: Mapping[str, object]) -> dict[str, object]:
    copied: dict[str, object] = {}
    for key, value in payload.items():
        if isinstance(value, list):
            copied[key] = list(value)
        elif isinstance(value, dict):
            copied[key] = dict(value)
        else:
            copied[key] = value
    return copied


def invalidate_custom_fields_cache() -> None:
    """Drop the process-local custom-field reconcile cache."""
    global _CUSTOM_FIELDS_CACHE
    _CUSTOM_FIELDS_CACHE = None


def cached_custom_fields() -> list[dict[str, object]] | None:
    """Return a defensive copy of the custom-field cache, if present."""
    if _CUSTOM_FIELDS_CACHE is None:
        return None
    return [_copy_payload(field) for field in _CUSTOM_FIELDS_CACHE]


def custom_field_inventory() -> tuple[dict[str, object], ...]:
    """Return the canonical custom-field inventory object."""
    return CUSTOM_FIELD_INVENTORY


def _coerce_object_type_entry(item: object) -> str | None:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        if "app_label" in item and "model" in item:
            return f"{item['app_label']}.{item['model']}"
        name = item.get("name")
        if isinstance(name, str):
            return name
    return None


def _normalize_current_object_types(raw_current: object) -> list[str]:
    if not isinstance(raw_current, list):
        return []
    normalized: list[str] = []
    for item in raw_current:
        coerced = _coerce_object_type_entry(item)
        if coerced is not None:
            normalized.append(coerced)
    return normalized


async def _fetch_existing_custom_field(netbox_session: object, name: str) -> object | None:
    try:
        return await netbox_rest.rest_first_async(
            netbox_session,
            "/api/extras/custom-fields/",
            query={"name": name, "limit": 2},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not pre-fetch custom field '%s' for object_types union: %s",
            name,
            exc,
        )
        return None


async def _union_object_types_with_current(
    netbox_session: object,
    custom_field: dict[str, object],
) -> None:
    """Mutate ``custom_field['object_types']`` to be current plus desired.

    Operators sometimes expand a custom field's scope manually in NetBox.
    Pre-merging makes the desired payload a superset of the current record so
    reconcile only adds object types and never removes operator additions.
    """
    desired = custom_field.get("object_types")
    name = custom_field.get("name")
    if not isinstance(desired, list) or not name:
        return
    existing = await _fetch_existing_custom_field(netbox_session, str(name))
    if existing is None:
        return
    current = _normalize_current_object_types(existing.serialize().get("object_types"))
    union = list(dict.fromkeys([*desired, *current]))
    operator_added = [item for item in union if item not in desired]
    if operator_added:
        logger.info(
            "Preserving operator-added object_types for custom field '%s': %s",
            name,
            ", ".join(operator_added),
        )
    custom_field["object_types"] = union


def _resolve_custom_field_delay() -> float:
    return get_float(
        settings_key="custom_fields_request_delay",
        env="PROXBOX_CUSTOM_FIELDS_REQUEST_DELAY",
        default=0.0,
        minimum=0.0,
    )


async def custom_field_payload_for_reconcile(
    netbox_session: object,
    custom_field: Mapping[str, object],
) -> dict[str, object]:
    """Return a mutable reconcile payload with operator-added scopes merged."""
    payload = _copy_payload(custom_field)
    await _union_object_types_with_current(netbox_session, payload)
    return payload


def _current_custom_field_normalizer(record: dict[str, object]) -> dict[str, object]:
    return {
        "name": record.get("name"),
        "type": (
            record.get("type", {}).get("value")
            if isinstance(record.get("type"), dict)
            else record.get("type")
        ),
        "label": record.get("label"),
        "description": record.get("description"),
        "ui_visible": (
            record.get("ui_visible", {}).get("value")
            if isinstance(record.get("ui_visible"), dict)
            else record.get("ui_visible")
        ),
        "ui_editable": (
            record.get("ui_editable", {}).get("value")
            if isinstance(record.get("ui_editable"), dict)
            else record.get("ui_editable")
        ),
        "weight": record.get("weight"),
        "filter_logic": (
            record.get("filter_logic", {}).get("value")
            if isinstance(record.get("filter_logic"), dict)
            else record.get("filter_logic")
        ),
        "search_weight": record.get("search_weight"),
        "group_name": record.get("group_name"),
        "object_types": _normalize_current_object_types(record.get("object_types")),
        "related_object_type": record.get("related_object_type"),
    }


async def reconcile_custom_field_with_status(
    netbox_session: object,
    custom_field: Mapping[str, object],
) -> ReconcileResult:
    """Reconcile one declared NetBox custom field and return its status."""
    payload = await custom_field_payload_for_reconcile(netbox_session, custom_field)
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise ProxboxException(
            message="Invalid NetBox custom field declaration.",
            detail={"reason": "invalid_custom_field_inventory", "field": payload},
        )
    return await netbox_rest.rest_reconcile_async_with_status(
        netbox_session,
        "/api/extras/custom-fields/",
        lookup={"name": name},
        payload=payload,
        schema=NetBoxCustomFieldSyncState,
        current_normalizer=_current_custom_field_normalizer,
    )


async def _reconcile_custom_fields_uncached(netbox_session: object) -> list[dict[str, object]]:
    fields: list[dict[str, object]] = []
    had_failures = False
    overloaded = False
    failed_fields: list[dict[str, str]] = []
    request_delay = _resolve_custom_field_delay()

    for custom_field in CUSTOM_FIELD_INVENTORY:
        try:
            result = await reconcile_custom_field_with_status(netbox_session, custom_field)
            fields.append(result.record.serialize())
        except ProxboxException as exc:
            had_failures = True
            failed_fields.append(
                {
                    "name": str(custom_field.get("name", "unknown")),
                    "error": str(exc.message),
                }
            )
            overloaded = overloaded or is_netbox_overwhelmed_error(exc)
            logger.warning(
                "Failed to create/update custom field '%s': %s - %s",
                custom_field.get("name", "unknown"),
                exc.message,
                exc.detail,
            )
            if overloaded:
                break
        except Exception as exc:  # noqa: BLE001
            had_failures = True
            failed_fields.append(
                {
                    "name": str(custom_field.get("name", "unknown")),
                    "error": str(exc),
                }
            )
            overloaded = overloaded or is_netbox_overwhelmed_error(exc)
            logger.warning(
                "Failed to create/update custom field '%s': %s",
                custom_field.get("name", "unknown"),
                str(exc),
            )
            if overloaded:
                break

        if request_delay > 0:
            await asyncio.sleep(request_delay)

    if had_failures:
        if overloaded:
            raise ProxboxException(
                message="NetBox is overwhelmed. Please retry this action in a few moments.",
                detail={
                    "reason": "netbox_overwhelmed",
                    "created_count": len(fields),
                    "failed_fields": failed_fields,
                },
            )

        raise ProxboxException(
            message="Failed to create all NetBox custom fields.",
            detail={
                "reason": "custom_field_sync_failed",
                "created_count": len(fields),
                "failed_fields": failed_fields,
            },
        )

    return fields


async def reconcile_custom_fields(
    netbox_session: object,
    *,
    force: bool = False,
) -> list[dict[str, object]]:
    """Reconcile all declared custom fields, optionally bypassing the cache."""
    global _CUSTOM_FIELDS_CACHE

    if not force:
        cached = cached_custom_fields()
        if cached is not None:
            return cached

    async with _CUSTOM_FIELDS_LOCK:
        if force:
            invalidate_custom_fields_cache()
        else:
            cached = cached_custom_fields()
            if cached is not None:
                return cached

        fields = await _reconcile_custom_fields_uncached(netbox_session)
        _CUSTOM_FIELDS_CACHE = tuple(_copy_payload(field) for field in fields)
        return [_copy_payload(field) for field in fields]
