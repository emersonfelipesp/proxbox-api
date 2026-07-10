"""Compatibility exports for VM interface stream handlers.

The public VM interface stream routes are registered from ``read_vm.py``.
This module intentionally does not define duplicate route decorators.
"""

from __future__ import annotations

from fastapi import APIRouter

from proxbox_api.routes.virtualization.virtual_machines.read_vm import (
    create_virtual_machines_interfaces_stream as create_vm_interfaces_stream,
)
from proxbox_api.routes.virtualization.virtual_machines.read_vm import (
    create_virtual_machines_ip_address_stream as create_vm_ip_addresses_stream,
)

router = APIRouter()

__all__ = (
    "create_vm_interfaces_stream",
    "create_vm_ip_addresses_stream",
    "router",
)
