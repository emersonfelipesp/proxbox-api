"""Proxmox-to-NetBox normalization and schema-driven mapping package."""

from proxbox_api.proxmox_to_netbox.models import ProxmoxToNetBoxVirtualMachine
from proxbox_api.proxmox_to_netbox.normalize import build_virtual_machine_transform
from proxbox_api.proxmox_to_netbox.schemas import (
    ProxmoxDiskEntry,
    parse_disk_entry,
    parse_vm_config_disks,
    size_str_to_mb,
)

__all__ = [
    "ProxmoxDiskEntry",
    "ProxmoxToNetBoxVirtualMachine",
    "build_virtual_machine_transform",
    "parse_disk_entry",
    "parse_vm_config_disks",
    "size_str_to_mb",
]
