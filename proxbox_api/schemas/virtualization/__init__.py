"""Virtualization schema models and VM configuration validator."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, computed_field, field_validator, model_validator

from proxbox_api.enum.proxmox import DiskFormat, ProxmoxVMStatus
from proxbox_api.proxmox_to_netbox.schemas.disks import ProxmoxDiskEntry, parse_vm_config_disks
from proxbox_api.schemas._coerce import normalize_bool


def _parse_key_value_string(value: object) -> dict[str, str]:
    if not isinstance(value, str):
        return {}
    parts = [part.strip() for part in value.split(",") if part.strip()]
    parsed: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        key = key.strip()
        raw = raw.strip()
        if key:
            parsed[key] = raw
    return parsed


class VMConfig(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    parent: str | None = None
    digest: str | None = None
    swap: int | None = None
    searchdomain: str | None = None
    boot: str | None = None
    name: str | None = None
    cores: int | None = None
    scsihw: str | None = None
    vmgenid: str | None = None
    memory: int | None = None
    description: str | None = None
    ostype: str | None = None
    numa: bool | None = None
    sockets: int | None = None
    cpulimit: int | None = None
    onboot: bool | None = None
    cpuunits: int | None = None
    agent: bool | None = None
    tags: str | None = None
    rootfs: str | None = None
    unprivileged: bool | None = None
    nesting: bool | None = None
    nameserver: str | None = None
    arch: str | None = None
    hostname: str | None = None
    features: str | None = None
    ciuser: str | None = None
    sshkeys: str | None = None
    ipconfig0: str | None = None

    @field_validator("numa", "onboot", "agent", "unprivileged", "nesting", mode="before")
    @classmethod
    def _coerce_bool_fields(cls, value: object) -> bool | None:
        return normalize_bool(value)

    @model_validator(mode="before")
    @classmethod
    def validate_dynamic_keys(cls, values: object) -> object:
        # Validate dynamic keys (e.g. scsi0, net0, etc.).
        if isinstance(values, dict):
            for key in values.keys():
                if (
                    not re.match(r"^(scsi|net|ide|unused|smbios)\d+$", key)
                    and key not in cls.model_fields
                ):
                    raise ValueError(f"Invalid key: {key}")
        return values

    @computed_field(return_type=list[ProxmoxDiskEntry])
    @property
    def disks(self) -> list[ProxmoxDiskEntry]:
        """Parsed disk entries from raw VM config."""

        return parse_vm_config_disks(self.model_extra or {})

    @computed_field(return_type=list[dict[str, object]])
    @property
    def networks(self) -> list[dict[str, object]]:
        """Parsed network configuration entries from raw VM config."""

        networks: list[dict[str, object]] = []
        index = 0
        while True:
            key = f"net{index}"
            raw_value = (self.model_extra or {}).get(key)
            if raw_value is None:
                break
            parsed = _parse_key_value_string(raw_value)
            if parsed:
                networks.append({key: parsed})
            index += 1
        return networks


class CPU(BaseModel):
    cores: int
    sockets: int
    type: str
    usage: int


class Memory(BaseModel):
    total: int
    used: int
    usage: int


class Disk(BaseModel):
    id: str
    storage: str
    size: int
    used: int
    usage: int
    format: DiskFormat | str
    path: str


class Network(BaseModel):
    id: str
    model: str
    bridge: str
    mac: str
    ip: str
    netmask: str
    gateway: str


class Snapshot(BaseModel):
    id: str
    name: str
    created: str
    description: str


class Backup(BaseModel):
    id: str
    storage: str
    created: str
    size: int
    status: str


class VirtualMachineSummary(BaseModel):
    id: str
    name: str
    status: ProxmoxVMStatus | str
    node: str
    cluster: str
    os: str
    description: str
    uptime: str
    created: str
    cpu: CPU
    memory: Memory
    disks: list[Disk]
    networks: list[Network]
    snapshots: list[Snapshot]
    backups: list[Backup]
