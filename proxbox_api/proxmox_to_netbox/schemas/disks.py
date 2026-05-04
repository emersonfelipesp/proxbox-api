"""Pydantic schemas for Proxmox disk parsing and normalization."""

from __future__ import annotations

import re

from pydantic import ConfigDict, computed_field, field_validator

from proxbox_api.schemas._base import ProxboxBaseModel

DISK_KEY_PATTERN = re.compile(r"^(scsi|ide|sata|virtio|mp)\d+$|^rootfs$")
UNUSED_DISK_PATTERN = re.compile(r"^unused\d+$")


def size_str_to_mb(size_str: str) -> int:
    """Convert Proxmox size string (e.g., '32G', '512M', '1T') to MB."""
    if not size_str:
        return 0
    size_str = size_str.strip().upper()
    match = re.match(r"^(\d+(?:\.\d+)?)\s*(B|K|M|G|T)?$", size_str)
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2) or "B"
    multipliers = {
        "B": 1,
        "K": 1024 / 1024,
        "M": 1,
        "G": 1024,
        "T": 1024 * 1024,
    }
    return int(value * multipliers.get(unit, 1))


def parse_disk_entry(key: str, raw_value: str) -> ProxmoxDiskEntry | None:
    """Parse a single disk entry from Proxmox VM config.

    Parses entries like:
        scsi0: local-lvm:vm-100-disk-0,size=32G,format=qcow2,aio=native
        ide0: local:vm-100-disk-1,size=64G

    Args:
        key: Disk key (e.g., 'scsi0', 'ide0')
        raw_value: Raw disk configuration string

    Returns:
        ProxmoxDiskEntry if valid, None if skipped (unused or invalid)
    """
    if not isinstance(raw_value, str):
        return None

    if UNUSED_DISK_PATTERN.match(key):
        return None

    if not DISK_KEY_PATTERN.match(key):
        return None

    parts = raw_value.split(",")
    storage_info = parts[0] if parts else ""

    disk_info: dict[str, object] = {"name": key, "storage": storage_info}

    for part in parts[1:]:
        if "=" in part:
            k, v = part.split("=", 1)
            disk_info[k.strip()] = v.strip()

    size_mb = size_str_to_mb(disk_info.get("size", "0"))
    if size_mb <= 0:
        return None

    storage_value = str(disk_info.get("storage") or "")
    storage_name = storage_value.split(":")[0] if storage_value else ""
    format_val = disk_info.get("format")

    description_parts = []
    if storage_name:
        description_parts.append(f"Storage: {storage_name}")
    if format_val:
        description_parts.append(f"Format: {format_val}")
    description = " | ".join(description_parts) if description_parts else None

    return ProxmoxDiskEntry(
        name=key,
        raw_value=raw_value,
        size=size_mb,
        storage=storage_info,
        storage_name=storage_name or None,
        format=format_val,
        description=description,
    )


def parse_vm_config_disks(vm_config: dict[str, object]) -> list[ProxmoxDiskEntry]:
    """Parse all disk entries from Proxmox VM config.

    Args:
        vm_config: Proxmox VM config dictionary

    Returns:
        Sorted list of ProxmoxDiskEntry objects for active disks
    """
    disks = []
    for key, value in vm_config.items():
        entry = parse_disk_entry(key, value)
        if entry is not None:
            disks.append(entry)

    disks.sort(key=lambda d: d.name)
    return disks


class ProxmoxDiskEntry(ProxboxBaseModel):
    """Parsed disk entry from Proxmox VM config (e.g., scsi0, ide0, sata0).

    All parsing and normalization happens in this schema - no logic in normalize.py.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    raw_value: str
    size: int = 0
    storage: str | None = None
    storage_name: str | None = None
    format: str | None = None
    description: str | None = None

    @field_validator("name", "raw_value", mode="before")
    @classmethod
    def validate_strings(cls, v: object) -> str:
        return str(v)

    @field_validator("size", mode="before")
    @classmethod
    def parse_size(cls, v: object) -> int:
        if isinstance(v, int):
            return v
        return size_str_to_mb(str(v))

    @computed_field(return_type=int)
    @property
    def size_mb(self) -> int:
        """Alias for size to provide consistent naming."""
        return self.size
