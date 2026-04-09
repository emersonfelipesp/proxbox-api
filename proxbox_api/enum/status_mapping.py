"""Centralized Proxmox-to-NetBox status and type mappings.

These enums replace the inline mapping dicts that were previously duplicated
across proxmox_to_netbox/models.py, services/sync/individual/vm_sync.py,
netbox_compat.py, services/sync/network.py, and routes/dcim/__init__.py.
"""

from __future__ import annotations

from enum import Enum


class ProxmoxToNetBoxVMStatus(str, Enum):
    """NetBox VirtualMachine status values, with mapping from Proxmox raw statuses."""

    active = "active"
    offline = "offline"
    planned = "planned"

    @classmethod
    def from_proxmox(cls, raw: object) -> "ProxmoxToNetBoxVMStatus":
        """Return the NetBox status that corresponds to a Proxmox VM status string.

        Unknown values default to ``active``.
        """
        _mapping = {
            "running": cls.active,
            "online": cls.active,
            "active": cls.active,
            "stopped": cls.offline,
            "paused": cls.offline,
            "offline": cls.offline,
            "planned": cls.planned,
        }
        text = str(raw or "active").strip().lower()
        return _mapping.get(text, cls.active)


class NetBoxInterfaceType(str, Enum):
    """NetBox interface type values, with mapping from Proxmox interface type strings."""

    loopback = "loopback"
    bridge = "bridge"
    lag = "lag"
    virtual = "virtual"
    other = "other"

    @classmethod
    def from_proxmox(cls, raw: object) -> "NetBoxInterfaceType":
        """Return the NetBox interface type that corresponds to a Proxmox interface type string.

        Unknown values default to ``other``.
        """
        _mapping = {
            "lo": cls.loopback,
            "bridge": cls.bridge,
            "bond": cls.lag,
            "vlan": cls.virtual,
        }
        text = str(raw or "").strip().lower()
        return _mapping.get(text, cls.other)
