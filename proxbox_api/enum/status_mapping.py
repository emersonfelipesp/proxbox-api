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

        Also accepts NetBox's own **nested choice shape**
        (``{"value": "offline", "label": "Offline"}``), because this helper is
        applied to both the Proxmox-derived desired status *and* the existing
        NetBox record's status when building the reconciliation diff. The
        existing record is loaded over raw REST, where a choice field arrives as
        that object rather than a bare string. Without unwrapping,
        ``str({...}).lower()`` matched no key and every existing record silently
        read back as ``active`` — so a VM whose status genuinely changed to
        ``active`` produced no diff and never updated
        (netbox-proxbox issue #617).

        Unknown values still default to ``active``.
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
        if isinstance(raw, dict):
            raw = raw.get("value", raw.get("label"))
        if isinstance(raw, cls):
            # Already mapped. Without this, str() on the member resolves to
            # Enum.__str__ ("ProxmoxToNetBoxVMStatus.offline"), matches no key,
            # and silently returns the `active` default -- so a stopped VM is
            # recorded as running. Normalizing twice must be a no-op.
            return raw
        if isinstance(raw, Enum):
            raw = raw.value
        text = str(raw or "active").strip().lower()
        return _mapping.get(text, cls.active)


class NetBoxInterfaceType(str, Enum):
    """NetBox interface type values, with mapping from Proxmox interface type strings.

    Every member must be a value NetBox accepts for ``dcim.Interface.type``.
    There was a ``loopback`` member, and NetBox has no such interface type, so
    it would have been rejected as an invalid choice the moment its mapping key
    matched. It is gone.

    Open vSwitch bridges, bonds, and internal ports have direct NetBox virtual
    counterparts. ``OVSPort`` deliberately remains ``other`` because it denotes
    a bridge member rather than a virtual interface. Node sync guards legacy
    cabled or ``mark_connected`` rows before applying these widened mappings.
    """

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
            "bridge": cls.bridge,
            "bond": cls.lag,
            "vlan": cls.virtual,
            "ovsbridge": cls.bridge,
            "ovsbond": cls.lag,
            "ovsintport": cls.virtual,
        }
        if isinstance(raw, cls):
            # Same idempotence requirement as the status mapping above.
            return raw
        if isinstance(raw, Enum):
            raw = raw.value
        text = str(raw or "").strip().lower()
        return _mapping.get(text, cls.other)
