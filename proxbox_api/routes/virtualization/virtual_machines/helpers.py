"""Concurrency helpers for virtual machine sync routes."""

from proxbox_api.runtime_settings import get_int


def resolve_vm_sync_concurrency() -> int:
    """Max concurrency for VM sync operations."""
    return get_int(
        settings_key="vm_sync_max_concurrency",
        env="PROXBOX_VM_SYNC_MAX_CONCURRENCY",
        default=4,
        minimum=1,
    )


def resolve_netbox_write_concurrency() -> int:
    """Max concurrency for NetBox API write operations (creates/updates)."""
    return get_int(
        settings_key="netbox_write_concurrency",
        env="PROXBOX_NETBOX_WRITE_CONCURRENCY",
        default=8,
        minimum=1,
    )


def resolve_proxmox_fetch_concurrency() -> int:
    """Max concurrency for Proxmox API fetch operations (reads)."""
    return get_int(
        settings_key="proxmox_fetch_concurrency",
        env="PROXBOX_PROXMOX_FETCH_CONCURRENCY",
        default=8,
        minimum=1,
    )
