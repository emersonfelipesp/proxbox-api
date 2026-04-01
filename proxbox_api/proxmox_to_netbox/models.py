"""Pydantic v2 models for Proxmox input and NetBox VM payload output."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from proxbox_api.proxmox_to_netbox.schemas.disks import ProxmoxDiskEntry


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _mb_from_bytes(value: object) -> int:
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        return 0
    if as_int <= 0:
        return 0
    return as_int // 1_000_000


def _relation_id(value: object) -> object:
    if isinstance(value, dict):
        return value.get("id")
    return value


def _status_value(value: object) -> object:
    if isinstance(value, dict):
        return value.get("value") or value.get("label")
    return value


def _choice_value(value: object) -> object:
    if isinstance(value, dict):
        return value.get("value") or value.get("label")
    return value


def _task_action_label(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "Task"
    lowered = text.lower()
    for prefix in ("qm", "lxc", "vz"):
        if lowered.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.replace("_", " ").replace("-", " ").strip()
    return text.title() or "Task"


def _task_datetime(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.isdigit():
            try:
                return datetime.fromtimestamp(int(stripped), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
    return value


def _normalized_tag_list(value: object) -> list[dict[str, object]]:
    if value is None:
        return []
    normalized: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, dict):
            normalized.append(item)
        else:
            text = str(item or "").strip()
            if text:
                normalized.append({"slug": text, "name": text})
    normalized.sort(key=lambda tag: str(tag.get("slug") or tag.get("name") or ""))
    return normalized


class NetBoxTagRef(BaseModel):
    """Normalized nested NetBox tag payload used in create and diff operations."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str | None = None
    slug: str
    color: str | None = None

    @field_validator("name", "slug", "color", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if value is None:
            return value
        text = str(value).strip()
        return text or None

    @model_validator(mode="after")
    def default_name_from_slug(self):
        if not self.name and self.slug:
            self.name = self.slug
        return self


class NetBoxNamedSlugTaggedState(BaseModel):
    """Shared normalized schema for named NetBox objects with slug and tags."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    slug: str
    tags: list[NetBoxTagRef] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[dict[str, object]]:
        return _normalized_tag_list(value)


class NetBoxClusterTypeSyncState(NetBoxNamedSlugTaggedState):
    description: str | None = None


class NetBoxManufacturerSyncState(NetBoxNamedSlugTaggedState):
    pass


class NetBoxDeviceRoleSyncState(NetBoxNamedSlugTaggedState):
    color: str
    description: str | None = None
    vm_role: bool | None = None


class NetBoxSiteSyncState(NetBoxNamedSlugTaggedState):
    status: str = "active"

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> str:
        text = str(_status_value(value) or "active").strip().lower()
        return text or "active"


class NetBoxClusterSyncState(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    type: int | None = None
    description: str | None = None
    tags: list[NetBoxTagRef] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[dict[str, object]]:
        return _normalized_tag_list(value)


class NetBoxDeviceTypeSyncState(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    model: str
    slug: str
    manufacturer: int | None = None
    tags: list[NetBoxTagRef] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator("manufacturer", mode="before")
    @classmethod
    def normalize_manufacturer(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[dict[str, object]]:
        return _normalized_tag_list(value)


class NetBoxCustomFieldSyncState(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    type: str
    label: str
    description: str | None = None
    ui_visible: str = "always"
    ui_editable: str = "hidden"
    weight: int = 100
    filter_logic: str = "loose"
    search_weight: int = 1000
    group_name: str | None = None
    object_types: list[str] = Field(default_factory=list)

    @field_validator(
        "name",
        "type",
        "label",
        "description",
        "ui_visible",
        "ui_editable",
        "filter_logic",
        "group_name",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if value is None:
            return value
        text = str(value).strip()
        return text or None

    @field_validator("weight", "search_weight", mode="before")
    @classmethod
    def normalize_int(cls, value: object) -> int:
        return int(value or 0)

    @field_validator("object_types", mode="before")
    @classmethod
    def normalize_object_types(cls, value: object) -> list[str]:
        if value is None:
            return []
        items = [str(item).strip() for item in value if str(item).strip()]
        return sorted(dict.fromkeys(items))


class NetBoxInterfaceSyncState(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    device: int
    name: str
    status: str = "active"
    type: str
    untagged_vlan: int | None = None
    mode: str | None = None
    tags: list[NetBoxTagRef] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator("device", "untagged_vlan", mode="before")
    @classmethod
    def normalize_device(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> str:
        text = str(_status_value(value) or "active").strip().lower()
        return text or "active"

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, value: object) -> object:
        normalized = _choice_value(value)
        if normalized in (None, ""):
            return None
        return str(normalized).strip() or None

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[dict[str, object]]:
        return _normalized_tag_list(value)


class NetBoxVirtualMachineInterfaceSyncState(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    virtual_machine: int
    name: str
    enabled: bool | None = None
    bridge: int | None = None
    mac_address: str | None = None
    type: str | None = None
    description: str | None = None
    untagged_vlan: int | None = None
    mode: str | None = None
    tags: list[NetBoxTagRef] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator("virtual_machine", "bridge", "untagged_vlan", mode="before")
    @classmethod
    def normalize_relations(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, value: object) -> object:
        normalized = _choice_value(value)
        if normalized in (None, ""):
            return None
        return str(normalized).strip() or None

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[dict[str, object]]:
        return _normalized_tag_list(value)


class NetBoxIpAddressSyncState(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    address: str
    assigned_object_type: str | None = None
    assigned_object_id: int | None = None
    status: str = "active"
    tags: list[NetBoxTagRef] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator("assigned_object_id", mode="before")
    @classmethod
    def normalize_assigned_object_id(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> str:
        text = str(_status_value(value) or "active").strip().lower()
        return text or "active"

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[dict[str, object]]:
        return NetBoxNamedSlugTaggedState.normalize_tags(value)


class NetBoxVlanSyncState(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    vid: int
    name: str
    status: str = "active"
    tags: list[NetBoxTagRef] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> str:
        text = str(_status_value(value) or "active").strip().lower()
        return text or "active"

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[dict[str, object]]:
        return _normalized_tag_list(value)


class NetBoxBackupSyncState(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    storage: int | None = None
    virtual_machine: int
    subtype: str | None = None
    creation_time: str | None = None
    size: int | None = None
    verification_state: str | None = None
    verification_upid: str | None = None
    volume_id: str
    notes: str | None = None
    vmid: str | int | None = None
    format: str | None = None
    tags: list[NetBoxTagRef] = Field(default_factory=list)

    @field_validator("virtual_machine", mode="before")
    @classmethod
    def normalize_virtual_machine(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("storage", mode="before")
    @classmethod
    def normalize_storage(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[dict[str, object]]:
        return _normalized_tag_list(value)


class NetBoxSnapshotSyncState(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    virtual_machine: int
    proxmox_storage: int | None = None
    name: str
    description: str | None = None
    vmid: int
    node: str
    snaptime: str | None = None
    parent: str | None = None
    subtype: str | None = None
    status: str = "active"

    @field_validator("virtual_machine", mode="before")
    @classmethod
    def normalize_virtual_machine(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("proxmox_storage", mode="before")
    @classmethod
    def normalize_proxmox_storage(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> str:
        text = str(value or "active").strip().lower()
        return text if text in ("active", "stale") else "active"


class NetBoxTaskHistorySyncState(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    virtual_machine: int
    vm_type: str = "unknown"
    upid: str
    node: str
    pid: int | None = None
    pstart: int | None = None
    task_id: str | None = None
    task_type: str
    username: str
    start_time: str
    end_time: str | None = None
    description: str | None = None
    status: str | None = None
    task_state: str | None = None
    exitstatus: str | None = None
    tags: list[NetBoxTagRef] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator(
        "virtual_machine",
        "pid",
        "pstart",
        mode="before",
    )
    @classmethod
    def normalize_relations(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator(
        "vm_type",
        "upid",
        "node",
        "task_id",
        "task_type",
        "username",
        "status",
        "task_state",
        "exitstatus",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if value is None:
            return value
        text = str(value).strip()
        return text or None

    @field_validator("start_time", "end_time", mode="before")
    @classmethod
    def normalize_datetimes(cls, value: object) -> object:
        normalized = _task_datetime(value)
        if normalized is None:
            return None
        if isinstance(normalized, datetime):
            return normalized.isoformat()
        return str(normalized)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[dict[str, object]]:
        return _normalized_tag_list(value)

    @model_validator(mode="after")
    def normalize_display_fields(self):
        if self.vm_type:
            self.vm_type = str(self.vm_type).strip().lower() or "unknown"
        if not self.description:
            vm_prefix = "CT" if self.vm_type == "lxc" else "VM"
            action = _task_action_label(self.task_type)
            task_id = str(self.task_id or "").strip()
            if task_id:
                self.description = f"{vm_prefix} {task_id} - {action}"
            else:
                self.description = action
        if not self.status:
            self.status = self.exitstatus or self.task_state or "unknown"
        else:
            self.status = (
                str(self.status).strip() or self.exitstatus or self.task_state or "unknown"
            )
        return self


class NetBoxVirtualDiskSyncState(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    virtual_machine: int
    name: str
    size: int
    storage: int | None = None
    description: str | None = None
    tags: list[NetBoxTagRef] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator("virtual_machine", mode="before")
    @classmethod
    def normalize_virtual_machine(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("storage", mode="before")
    @classmethod
    def normalize_storage(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[dict[str, object]]:
        return _normalized_tag_list(value)


class NetBoxStorageSyncState(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    cluster: int
    name: str
    storage_type: str | None = None
    content: str | None = None
    path: str | None = None
    nodes: str | None = None
    shared: bool = False
    enabled: bool = True
    backups: list[int] = Field(default_factory=list)
    tags: list[NetBoxTagRef] = Field(default_factory=list)

    @field_validator("cluster", mode="before")
    @classmethod
    def normalize_cluster(cls, value: object) -> int:
        """Ensure cluster is an integer ID."""
        relation_id = _relation_id(value)
        if relation_id is None:
            raise ValueError("cluster is required")
        try:
            return int(relation_id)
        except (TypeError, ValueError) as e:
            raise ValueError(f"cluster must be a valid integer ID: {e}")

    @field_validator("backups", mode="before")
    @classmethod
    def normalize_backups(cls, value: object) -> list[int]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple, set)):
            value = [value]
        normalized: list[int] = []
        for item in value:
            relation_id = _relation_id(item)
            if relation_id in (None, ""):
                continue
            try:
                normalized.append(int(relation_id))
            except (TypeError, ValueError):
                continue
        return sorted(dict.fromkeys(normalized))

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[dict[str, object]]:
        return _normalized_tag_list(value)


class ProxmoxVmResourceInput(BaseModel):
    """Raw Proxmox VM resource payload from cluster resources endpoint."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    vmid: int
    name: str
    node: str
    status: str = "stopped"
    type: Literal["qemu", "lxc", "unknown"] = "unknown"
    maxcpu: int | None = None
    maxmem: int | None = None
    maxdisk: int | None = None

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, value: object) -> str:
        text = str(value or "unknown").strip().lower()
        if text in {"qemu", "lxc"}:
            return text
        return "unknown"

    @computed_field(return_type=int)
    @property
    def memory_mb(self) -> int:
        return _mb_from_bytes(self.maxmem)

    @computed_field(return_type=int)
    @property
    def disk_mb(self) -> int:
        return _mb_from_bytes(self.maxdisk)


class ProxmoxVmConfigInput(BaseModel):
    """Raw Proxmox VM config payload from `/nodes/{node}/{type}/{vmid}/config`."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    onboot: int | str | bool | None = None
    agent: int | str | bool | None = None
    unprivileged: int | str | bool | None = None
    searchdomain: str | None = None

    @computed_field(return_type=bool)
    @property
    def start_at_boot(self) -> bool:
        return _as_bool(self.onboot)

    @computed_field(return_type=bool)
    @property
    def qemu_agent_enabled(self) -> bool:
        return _as_bool(self.agent)

    @computed_field(return_type=bool)
    @property
    def unprivileged_container(self) -> bool:
        return _as_bool(self.unprivileged)

    @computed_field(return_type=list[ProxmoxDiskEntry])
    @property
    def disks(self) -> list[ProxmoxDiskEntry]:
        """Parse disk entries from VM config into ProxmoxDiskEntry objects."""
        from proxbox_api.proxmox_to_netbox.schemas.disks import parse_vm_config_disks

        return parse_vm_config_disks(self.model_extra or {})


class NetBoxVirtualMachineCreateBody(BaseModel):
    """Validated NetBox create body for virtualization virtual machine endpoint."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    status: str
    cluster: int
    device: int | None = None
    role: int | None = None
    vcpus: int = 0
    memory: int = 0
    disk: int = 0
    tags: list[int] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)
    description: str | None = None

    @field_validator("vcpus", "memory", "disk", mode="before")
    @classmethod
    def coerce_nullable_vm_ints(cls, value: object) -> int:
        if value is None:
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> str:
        mapping = {
            "running": "active",
            "online": "active",
            "active": "active",
            "stopped": "offline",
            "paused": "offline",
            "offline": "offline",
            "planned": "planned",
        }
        text = str(value or "active").strip().lower()
        return mapping.get(text, "active")

    @field_validator("cluster", "device", "role", mode="before")
    @classmethod
    def normalize_relations(cls, value: object) -> object:
        return _relation_id(value)

    @model_validator(mode="after")
    def validate_required_relations(self):
        if self.cluster <= 0:
            raise ValueError("cluster must be a positive NetBox object id")
        if self.device is not None and self.device <= 0:
            raise ValueError("device must be positive when provided")
        if self.role is not None and self.role <= 0:
            raise ValueError("role must be positive when provided")
        return self

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[int]:
        if value is None:
            return []
        normalized: list[int] = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("id")
            try:
                normalized.append(int(item))
            except (TypeError, ValueError):
                continue
        return sorted(set(normalized))


class NetBoxDeviceSyncState(BaseModel):
    """Validated NetBox device sync body/state used for create and diff operations."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    status: str = "active"
    cluster: int | None = None
    device_type: int | None = None
    role: int | None = None
    site: int | None = None
    description: str | None = None
    tags: list[NetBoxTagRef] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> str:
        text = str(_status_value(value) or "active").strip().lower()
        return text or "active"

    @field_validator("cluster", "device_type", "role", "site", mode="before")
    @classmethod
    def normalize_relations(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[dict[str, object]]:
        return _normalized_tag_list(value)

    @model_validator(mode="after")
    def validate_required_relations(self):
        for field_name in ("cluster", "device_type", "role", "site"):
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"{field_name} must be positive when provided")
        return self


class ProxmoxToNetBoxVirtualMachine(BaseModel):
    """Schema-driven transform object for Proxmox input to NetBox VM create payload."""

    model_config = ConfigDict(extra="forbid")

    resource: ProxmoxVmResourceInput
    config: ProxmoxVmConfigInput = Field(default_factory=ProxmoxVmConfigInput)
    cluster_id: int
    device_id: int | None = None
    role_id: int | None = None
    tag_ids: list[int] = Field(default_factory=list)
    last_updated: datetime | None = None

    @computed_field(return_type=dict)
    @property
    def vm_custom_fields(self) -> dict[str, object]:
        vm_type = self.resource.type if self.resource.type in {"qemu", "lxc"} else "unknown"
        fields = {
            "proxmox_vm_id": self.resource.vmid,
            "proxmox_vm_type": vm_type,
            "proxmox_start_at_boot": self.config.start_at_boot,
            "proxmox_unprivileged_container": self.config.unprivileged_container,
            "proxmox_qemu_agent": self.config.qemu_agent_enabled,
            "proxmox_search_domain": self.config.searchdomain,
        }
        if self.last_updated:
            fields["proxmox_last_updated"] = self.last_updated.isoformat()
        return fields

    def as_netbox_create_body(self) -> NetBoxVirtualMachineCreateBody:
        """Return validated NetBox virtual machine create body."""

        return NetBoxVirtualMachineCreateBody(
            name=self.resource.name,
            status=self.resource.status,
            cluster=self.cluster_id,
            device=self.device_id,
            role=self.role_id,
            vcpus=int(self.resource.maxcpu or 0),
            memory=self.resource.memory_mb,
            disk=self.resource.disk_mb,
            tags=[tag for tag in self.tag_ids if int(tag) > 0],
            custom_fields=self.vm_custom_fields,
            description=f"Synced from Proxmox node {self.resource.node}",
        )
