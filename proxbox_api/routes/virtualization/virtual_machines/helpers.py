"""Concurrency helpers for virtual machine sync routes."""

import os


def resolve_vm_sync_concurrency() -> int:
    """Get max concurrency for VM sync operations (NetBox API writes)."""
    raw_value = os.environ.get("PROXBOX_VM_SYNC_MAX_CONCURRENCY", "").strip()
    if not raw_value:
        return 4
    try:
        value = int(raw_value)
    except ValueError:
        return 4
    return max(1, value)


def resolve_netbox_write_concurrency() -> int:
    """Get max concurrency for NetBox API write operations (creates/updates)."""
    raw_value = os.environ.get("PROXBOX_NETBOX_WRITE_CONCURRENCY", "").strip()
    if not raw_value:
        return 8
    try:
        value = int(raw_value)
    except ValueError:
        return 8
    return max(1, value)


def resolve_proxmox_fetch_concurrency() -> int:
    """Get max concurrency for Proxmox API fetch operations (reads)."""
    raw_value = os.environ.get("PROXBOX_PROXMOX_FETCH_CONCURRENCY", "").strip()
    if not raw_value:
        return 8
    try:
        value = int(raw_value)
    except ValueError:
        return 8
    return max(1, value)
