from pydantic import BaseModel, model_validator
from typing import Dict, Any, List
import re

class VMConfig(BaseModel):
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
    numa: int | None = None
    sockets: int | None = None
    cpulimit: int | None = None
    onboot: int | None = None
    cpuunits: int | None = None
    agent: int | None = None
    tags: str | None = None
    rootfs: str | None = None
    unprivileged: int | None = None
    nesting: int | None = None
    nameserver: str | None = None
    arch: str | None = None
    hostname: str | None = None
    features: str | None = None
    
    @model_validator(mode="before")
    @classmethod
    def validate_dynamic_keys(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        # Validate dynamic keys (e.g. scsi0, net0, etc.).
        if values:
            for key in values.keys():
                if not re.match(r'^(scsi|net|ide|unused|smbios)\d+$', key) and key not in cls.model_fields:
                    raise ValueError(f"Invalid key: {key}")
            return values

    class Config:
        extra = 'allow'


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
    format: str
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
    status: str
    node: str
    cluster: str
    os: str
    description: str
    uptime: str
    created: str
    cpu: CPU
    memory: Memory
    disks: List[Disk]
    networks: List[Network]
    snapshots: List[Snapshot]
    backups: List[Backup]