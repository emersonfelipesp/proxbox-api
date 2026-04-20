"""TypedDict definitions for structured data types across the application."""

from __future__ import annotations

import ipaddress
from typing import Any, NotRequired, TypedDict


class ProxboxSettingsDict(TypedDict):
    """Settings structure for Proxbox configuration from NetBox plugin."""

    backend_log_file_path: str
    ssrf_protection_enabled: bool
    allow_private_ips: bool
    allowed_ip_ranges: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    blocked_ip_ranges: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
    encryption_key: str
    use_guest_agent_interface_name: bool
    proxbox_fetch_max_concurrency: int
    ignore_ipv6_link_local_addresses: bool
    primary_ip_preference: str
    netbox_max_concurrent: int
    netbox_max_retries: int
    netbox_retry_delay: float
    netbox_get_cache_ttl: float
    bulk_batch_size: int
    bulk_batch_delay_ms: int
    vm_sync_max_concurrency: int
    custom_fields_request_delay: float
    proxmox_timeout: NotRequired[int]
    proxmox_max_retries: NotRequired[int]
    proxmox_retry_backoff: NotRequired[float]


class SyncResultDict(TypedDict):
    """Result structure for synchronization operations."""

    success: bool
    created: int
    updated: int
    deleted: int
    failed: int
    errors: list[str]
    warnings: NotRequired[list[str]]


class DevicePayloadDict(TypedDict, total=False):
    """Payload structure for device creation/update in NetBox."""

    name: str
    device_type: int
    device_role: int
    site: int
    status: str
    serial: NotRequired[str]
    asset_tag: NotRequired[str]
    comments: NotRequired[str]
    tags: NotRequired[list[dict[str, Any]]]


class VMPayloadDict(TypedDict, total=False):
    """Payload structure for virtual machine creation/update in NetBox."""

    name: str
    status: str
    cluster: int
    vcpus: int
    memory: int
    disk: NotRequired[int]
    comments: NotRequired[str]
    tags: NotRequired[list[dict[str, Any]]]


class InterfacePayloadDict(TypedDict, total=False):
    """Payload structure for network interface creation/update."""

    name: str
    type: str
    device: NotRequired[int]
    virtual_machine: NotRequired[int]
    enabled: bool
    mtu: NotRequired[int]
    mac_address: NotRequired[str]
    tags: NotRequired[list[dict[str, Any]]]


class IPAddressPayloadDict(TypedDict, total=False):
    """Payload structure for IP address creation/update."""

    address: str
    status: str
    interface: NotRequired[int]
    virtual_machine_interface: NotRequired[int]
    description: NotRequired[str]
    tags: NotRequired[list[dict[str, Any]]]


class CacheEntryDict(TypedDict, total=False):
    """Structure for cache entries with TTL support."""

    value: Any
    ttl: NotRequired[float]
    created_at: float


class StoragePayloadDict(TypedDict, total=False):
    """Payload structure for storage resource creation/update."""

    name: str
    type: str
    site: int
    description: NotRequired[str]
    comments: NotRequired[str]
    tags: NotRequired[list[dict[str, Any]]]


class ProxmoxDeviceDict(TypedDict, total=False):
    """Structure for Proxmox device/node data."""

    node: str
    status: str
    uptime: int
    cpu: float
    maxcpu: int
    memory: int
    maxmemory: int
    disk: int
    maxdisk: int


class ProxmoxVMDict(TypedDict, total=False):
    """Structure for Proxmox virtual machine data."""

    vmid: int
    name: str
    node: str
    status: str
    uptime: int
    netin: int
    netout: int
    diskread: int
    diskwrite: int
    cpu: float
    maxcpu: int
    memory: int
    maxmemory: int


class SyncPhaseResultDict(TypedDict):
    """Result of a single sync phase operation."""

    phase: str
    success: bool
    items_processed: int
    items_created: int
    items_updated: int
    items_failed: int
    errors: list[str]
    duration_seconds: float


__all__ = [
    "ProxboxSettingsDict",
    "SyncResultDict",
    "DevicePayloadDict",
    "VMPayloadDict",
    "InterfacePayloadDict",
    "IPAddressPayloadDict",
    "CacheEntryDict",
    "StoragePayloadDict",
    "ProxmoxDeviceDict",
    "ProxmoxVMDict",
    "SyncPhaseResultDict",
]
