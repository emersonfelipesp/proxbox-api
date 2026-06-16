"""Virtualization schema models and VM configuration validator."""

import re

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

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


# Numbered device-bus prefixes from the Proxmox VE API spec (proxmox-sdk source of truth).
# Each entry represents a family indexed by an integer suffix (e.g. net0, net1, sata3 …).
# Single-instance fields (efidisk0, tpmstate0, audio0 …) are declared as explicit model
# fields below and are therefore NOT included in this pattern.
_DYNAMIC_KEY_RE = re.compile(
    r"^(scsi|net|ide|sata|virtio|unused|smbios|hostpci|usb|serial|parallel|numa|ipconfig|virtiofs)\d+$"
)


class VMConfig(BaseModel):
    """Proxmox VM/CT configuration response.

    Covers both QEMU and LXC config payloads returned by
    ``GET /proxmox/{node}/{type}/{vmid}/config``.

    Static fields are derived from ``GetNodesNodeQemuVmidConfigResponse`` in
    proxmox-sdk, which is the generated OpenAPI source of truth for every field
    Proxmox VE can return.  Numbered device-bus fields (net[n], scsi[n],
    sata[n] …) land in ``model_extra`` via the dynamic whitelist regex and are
    parsed by the ``disks`` / ``networks`` computed fields.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # ------------------------------------------------------------------ #
    # Shared QEMU + LXC
    # ------------------------------------------------------------------ #
    parent: str | None = None
    digest: str | None = None
    description: str | None = None
    name: str | None = None
    tags: str | None = None
    onboot: bool | None = None
    boot: str | None = None
    arch: str | None = None
    ostype: str | None = None
    searchdomain: str | None = None
    nameserver: str | None = None
    sshkeys: str | None = None
    ciuser: str | None = None
    ipconfig0: str | None = None

    # ------------------------------------------------------------------ #
    # CPU / memory
    # ------------------------------------------------------------------ #
    cores: int | None = None
    sockets: int | None = None
    numa: bool | None = None
    cpu: str | None = None  # QEMU emulated CPU type, e.g. "Cascadelake-Server-noTSX"
    cpulimit: float | None = None  # fractional limit, e.g. 2.5
    cpuunits: int | None = None
    vcpus: int | None = None
    memory: int | None = None  # keep int for backward compat; LXC and older QEMU return integers
    balloon: int | None = None
    shares: int | None = None
    hugepages: str | None = None
    keephugepages: bool | None = None
    affinity: str | None = None

    # ------------------------------------------------------------------ #
    # Machine / BIOS / boot
    # ------------------------------------------------------------------ #
    bios: str | None = None  # "seabios" (default) or "ovmf" (UEFI/EFI)
    machine: str | None = None  # e.g. "pc-i440fx-9.0", "q35"
    acpi: bool | None = None
    kvm: bool | None = None
    localtime: bool | None = None
    tdf: bool | None = None
    tablet: bool | None = None
    keyboard: str | None = None
    vga: str | None = None
    audio0: str | None = None
    spice_enhancements: str | None = None
    smbios1: str | None = None
    vmgenid: str | None = None
    vmstate: str | None = None
    vmstatestorage: str | None = None

    # ------------------------------------------------------------------ #
    # Storage
    # ------------------------------------------------------------------ #
    scsihw: str | None = None
    efidisk0: str | None = None  # EFI variable disk; always efidisk0
    tpmstate0: str | None = None  # TPM state disk; always tpmstate0
    bootdisk: str | None = None
    cdrom: str | None = None

    # ------------------------------------------------------------------ #
    # Network / agent
    # ------------------------------------------------------------------ #
    agent: bool | None = None

    # ------------------------------------------------------------------ #
    # Lifecycle / state
    # ------------------------------------------------------------------ #
    autostart: bool | None = None
    reboot: bool | None = None
    freeze: bool | None = None
    protection: bool | None = None
    template: bool | None = None
    lock: str | None = None
    startup: str | None = None
    startdate: str | None = None
    watchdog: str | None = None
    hookscript: str | None = None

    # ------------------------------------------------------------------ #
    # Hotplug / hardware
    # ------------------------------------------------------------------ #
    hotplug: str | None = None
    rng0: str | None = None
    ivshmem: str | None = None

    # ------------------------------------------------------------------ #
    # Migration / snapshots
    # ------------------------------------------------------------------ #
    migrate_downtime: float | None = None
    migrate_speed: int | None = None
    snaptime: int | None = None
    smp: int | None = None

    # ------------------------------------------------------------------ #
    # Cloud-init
    # ------------------------------------------------------------------ #
    cicustom: str | None = None
    cipassword: str | None = None
    citype: str | None = None
    ciupgrade: bool | None = None

    # ------------------------------------------------------------------ #
    # Runtime / snapshot metadata (read-only, included by Proxmox)
    # ------------------------------------------------------------------ #
    meta: str | None = None
    runningcpu: str | None = None
    runningmachine: str | None = None
    args: str | None = None

    # ------------------------------------------------------------------ #
    # Advanced / vendor-specific (Proxmox API uses dash-names for these)
    # ------------------------------------------------------------------ #
    allow_ksm: str | None = Field(None, alias="allow-ksm")
    amd_sev: str | None = Field(None, alias="amd-sev")
    intel_tdx: str | None = Field(None, alias="intel-tdx")
    running_nets_host_mtu: str | None = Field(None, alias="running-nets-host-mtu")

    # ------------------------------------------------------------------ #
    # LXC-only (kept for the shared QEMU+LXC config route)
    # ------------------------------------------------------------------ #
    swap: int | None = None
    rootfs: str | None = None
    unprivileged: bool | None = None
    nesting: bool | None = None
    hostname: str | None = None
    features: str | None = None

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #

    @field_validator(
        "numa",
        "onboot",
        "agent",
        "unprivileged",
        "nesting",
        "acpi",
        "autostart",
        "ciupgrade",
        "freeze",
        "keephugepages",
        "kvm",
        "localtime",
        "protection",
        "reboot",
        "tablet",
        "tdf",
        "template",
        mode="before",
    )
    @classmethod
    def _coerce_bool_fields(cls, value: object) -> bool | None:
        return normalize_bool(value)

    @model_validator(mode="before")
    @classmethod
    def validate_dynamic_keys(cls, values: object) -> object:
        """Reject keys that are not known static fields or numbered device buses.

        Accepted keys:
        - Every Python field name declared on this model.
        - Every ``Field(alias=...)`` value (e.g. ``allow-ksm``, ``amd-sev``).
        - Any key matching ``_DYNAMIC_KEY_RE`` (net[n], scsi[n], sata[n] …).
        """
        if isinstance(values, dict):
            # Build accepted-key set: Python field names + their declared aliases.
            known_static: set[str] = set(cls.model_fields)
            for fi in cls.model_fields.values():
                if fi.alias:
                    known_static.add(fi.alias)

            for key in values.keys():
                if not _DYNAMIC_KEY_RE.match(key) and key not in known_static:
                    raise ValueError(f"Invalid key: {key}")
        return values

    # ------------------------------------------------------------------ #
    # Computed fields
    # ------------------------------------------------------------------ #

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
