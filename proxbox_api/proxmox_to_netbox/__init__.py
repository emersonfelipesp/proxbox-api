"""Proxmox-to-NetBox normalization and schema-driven mapping package."""

from proxbox_api.proxmox_to_netbox.models import ProxmoxToNetBoxVirtualMachine
from proxbox_api.proxmox_to_netbox.normalize import build_virtual_machine_transform

__all__ = [
    "ProxmoxToNetBoxVirtualMachine",
    "build_virtual_machine_transform",
]
