"""Pydantic v2 models for Proxmox input and NetBox VM payload output."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from urllib.parse import unquote

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from proxbox_api.enum.status_mapping import ProxmoxToNetBoxVMStatus
from proxbox_api.proxmox_to_netbox.schemas.disks import ProxmoxDiskEntry

CLOUDINIT_SSHKEYS_MAX_BYTES = 10 * 1024
CLOUDINIT_SSHKEYS_TRUNCATION_SENTINEL = "\n# truncated by proxbox-api at 10KB"


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


_TRUE_TOKENS = frozenset({"1", "true", "yes", "on"})


def _parse_proxmox_kv_flag(value: object) -> bool:
    """Parse a Proxmox VE flag of the form ``<1|0>[,k=v,...]`` or
    ``enabled=<1|0>[,k=v,...]`` into a bool.

    Proxmox returns several config fields (notably ``agent``) as a comma-joined
    string with optional key=value tail entries, e.g. ``"1,fstrim_cloned_disks=1"``
    or ``"enabled=1,fstrim_cloned_disks=1"``. The plain ``_as_bool`` helper only
    matches whole-string tokens and returns False for both forms, which silently
    disables guest-agent fetches for users who turned the agent on through the
    PVE web UI with default options.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if not isinstance(value, str):
        return False
    text = value.strip().lower()
    if not text:
        return False
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        return False
    head = parts[0]
    if "=" not in head:
        return head in _TRUE_TOKENS
    for part in parts:
        if "=" not in part:
            continue
        key, _, raw = part.partition("=")
        if key.strip() == "enabled":
            return raw.strip() in _TRUE_TOKENS
    return False


def parse_proxmox_tags(raw: str | None) -> list[str]:
    """Parse a Proxmox ``tags`` field into a normalized tag-name list.

    Proxmox stores VM/LXC tags as a single ``;``-separated string
    (e.g. ``"critical;end-user-impact;production"``). This helper splits,
    trims, lowercases, and dedupes preserving input order.
    """
    if not raw or not isinstance(raw, str):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for chunk in raw.split(";"):
        name = chunk.strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _mb_from_bytes(value: object) -> int:
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        return 0
    if as_int <= 0:
        return 0
    # NetBox VM disk must match virtual disk aggregate, which is parsed in MiB.
    return as_int // (1024 * 1024)


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


def _content_type_value(value: object) -> object:
    if isinstance(value, dict):
        app_label = value.get("app_label") or value.get("app")
        model = value.get("model")
        if app_label and model:
            return f"{app_label}.{model}"
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
        elif hasattr(item, "serialize"):
            normalized.append(item.serialize())
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
    tenant: int | None = None
    scope_type: str | None = None
    scope_id: int | None = None
    description: str | None = None
    tags: list[NetBoxTagRef] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator("type", "tenant", "scope_id", mode="before")
    @classmethod
    def normalize_relations(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("scope_type", mode="before")
    @classmethod
    def normalize_scope_type(cls, value: object) -> object:
        normalized = _content_type_value(value)
        if normalized in (None, ""):
            return None
        return str(normalized).strip() or None

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
    related_object_type: str | None = None

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
    bridge: int | None = None
    untagged_vlan: int | None = None
    mode: str | None = None
    tags: list[NetBoxTagRef] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator("device", "bridge", "untagged_vlan", mode="before")
    @classmethod
    def normalize_device(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> str:
        text = str(_status_value(value) or "active").strip().lower()
        return text or "active"

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, value: object) -> str:
        text = str(_choice_value(value) or "").strip().lower()
        return text or "other"

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
    """Desired state for `/api/virtualization/interfaces/` rows.

    Note: `mac_address` is intentionally **not** declared here. NetBox 4.5/4.6
    treat it as a read-only computed echo of `primary_mac_address`; writing it
    is silently dropped, so including it in the desired payload would produce a
    perpetual no-op diff. MAC reconciliation lives in
    `proxbox_api.services.sync.mac_address` and runs as a per-interface post-step.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    virtual_machine: int
    name: str
    enabled: bool | None = None
    type: str | None = None
    description: str | None = None
    bridge: int | None = None
    untagged_vlan: int | None = None
    mode: str | None = None
    tags: list[NetBoxTagRef] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator("virtual_machine", "untagged_vlan", "bridge", mode="before")
    @classmethod
    def normalize_relations(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, value: object) -> object:
        normalized = _choice_value(value)
        if normalized in (None, ""):
            return None
        return str(normalized).strip().lower() or None

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


class NetBoxMACAddressSyncState(BaseModel):
    """Desired state of a `dcim.MACAddress` row in NetBox.

    NetBox 4.2+ stores MACs as first-class objects under `/api/dcim/mac-addresses/`
    with a GFK to the assigned interface (DCIM or VMInterface). The legacy
    inline `mac_address` string on `VMInterface` is `read_only=True` at NetBox
    4.5/4.6 — writes to it are silently dropped, so the only way to record a
    MAC is the model below plus a `primary_mac_address` FK PATCH on the
    interface.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mac_address: str
    assigned_object_type: str
    assigned_object_id: int
    tags: list[NetBoxTagRef] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator("mac_address", mode="before")
    @classmethod
    def normalize_mac(cls, value: object) -> str:
        text = str(value or "").strip().replace("-", ":").upper()
        # NetBox canonical form is colon-separated uppercase hex.
        return text

    @field_validator("assigned_object_id", mode="before")
    @classmethod
    def normalize_assigned_object_id(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[dict[str, object]]:
        return NetBoxNamedSlugTaggedState.normalize_tags(value)


class NetBoxIpAddressSyncState(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    address: str
    assigned_object_type: str | None = None
    assigned_object_id: int | None = None
    status: str = "active"
    dns_name: str = ""
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

    @field_validator("dns_name", mode="before")
    @classmethod
    def normalize_dns_name(cls, value: object) -> str:
        if value in (None, ""):
            return ""
        text = str(value).strip().rstrip(".").lower()
        if not text or text == "localhost" or text.startswith("localhost."):
            return ""
        return text[:255]

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[dict[str, object]]:
        return NetBoxNamedSlugTaggedState.normalize_tags(value)


class NetBoxVlanSyncState(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    vid: int
    name: str
    status: str = "active"
    site: int | None = None
    tenant: int | None = None
    tags: list[NetBoxTagRef] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator("site", "tenant", mode="before")
    @classmethod
    def normalize_relations(cls, value: object) -> object:
        return _relation_id(value)

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

    proxmox_storage: int | None = None
    storage: str | None = None
    virtual_machine: int
    subtype: str | None = None
    creation_time: str | None = None
    size: int | None = None
    used: int | None = None
    encrypted: str | None = None
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

    @field_validator("proxmox_storage", mode="before")
    @classmethod
    def normalize_proxmox_storage(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[dict[str, object]]:
        return _normalized_tag_list(value)

    @field_validator("subtype", "format", "verification_state", mode="before")
    @classmethod
    def normalize_choice_fields(cls, value: object) -> object:
        return _choice_value(value)


class NetBoxCloudInitSyncState(BaseModel):
    """Pinned schema for the ``/api/plugins/proxbox/vm-cloudinit/`` payload."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    virtual_machine: int
    ciuser: str = ""
    sshkeys: str = ""
    ipconfig0: str = ""
    sshkeys_truncated: bool = False

    @field_validator("virtual_machine", mode="before")
    @classmethod
    def normalize_virtual_machine(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("ciuser", "sshkeys", "ipconfig0", mode="before")
    @classmethod
    def coerce_string(cls, value: object) -> str:
        return "" if value is None else str(value)


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
        mode="before",
    )
    @classmethod
    def normalize_relations(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("pstart", mode="before")
    @classmethod
    def normalize_pstart(cls, value: object) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

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

    @field_validator("status", "exitstatus", mode="before")
    @classmethod
    def truncate_long_text(cls, value: object) -> object:
        if isinstance(value, str) and len(value) > 2048:
            return value[:2048]
        return value

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
    # Remote-host fields
    server: str | None = None
    port: int | None = None
    username: str | None = None
    # NFS / CIFS
    export: str | None = None
    share: str | None = None
    # Ceph / RBD
    pool: str | None = None
    monhost: str | None = None
    namespace: str | None = None
    # PBS
    datastore: str | None = None
    subdir: str | None = None
    # Filesystem
    mountpoint: str | None = None
    is_mountpoint: str | None = None
    preallocation: str | None = None
    format: str | None = None
    # Retention / backup
    prune_backups: str | None = Field(None, alias="prune-backups")
    max_protected_backups: int | None = Field(None, alias="max-protected-backups")
    # Full raw config
    raw_config: dict[str, object] = Field(default_factory=dict)
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


class NetBoxReplicationSyncState(BaseModel):
    """Validated NetBox replication sync body/state used for create and diff operations."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    endpoint: int | None = None
    virtual_machine: int
    proxmox_node: int | None = None
    replication_id: str
    guest: int
    target: str
    job_type: str = "local"
    schedule: str = "*/15"
    rate: float | None = None
    comment: str | None = None
    disable: bool = False
    source: str | None = None
    jobnum: int
    remove_job: str | None = None
    status: str = "active"
    raw_config: dict[str, object] = Field(default_factory=dict)
    tags: list[NetBoxTagRef] = Field(default_factory=list)
    custom_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator("endpoint", "virtual_machine", "proxmox_node", mode="before")
    @classmethod
    def normalize_relations(cls, value: object) -> object:
        return _relation_id(value)

    @field_validator("guest", "jobnum", mode="before")
    @classmethod
    def normalize_ints(cls, value: object) -> object:
        relation_id = _relation_id(value)
        if relation_id in (None, ""):
            return relation_id
        try:
            return int(relation_id)
        except (TypeError, ValueError):
            return relation_id

    @field_validator("rate", mode="before")
    @classmethod
    def normalize_rate(cls, value: object) -> object:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return value

    @field_validator(
        "job_type",
        "schedule",
        "comment",
        "source",
        "remove_job",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: object) -> object:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("disable", mode="before")
    @classmethod
    def normalize_disable(cls, value: object) -> bool:
        return _as_bool(value)

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: object) -> list[dict[str, object]]:
        return _normalized_tag_list(value)

    @model_validator(mode="after")
    def validate_required_relations(self):
        if self.virtual_machine <= 0:
            raise ValueError("virtual_machine must be a positive NetBox object id")
        if self.proxmox_node is not None and self.proxmox_node <= 0:
            raise ValueError("proxmox_node must be positive when provided")
        if self.guest <= 0:
            raise ValueError("guest must be a positive VM ID")
        if self.jobnum <= 0:
            raise ValueError("jobnum must be a positive job identifier")
        return self


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
    ciuser: str | None = None
    sshkeys: str | None = None
    ipconfig0: str | None = None
    sshkeys_truncated: bool = False
    tags: str | None = None

    @field_validator("sshkeys", mode="before")
    @classmethod
    def decode_and_truncate_sshkeys(cls, value: object) -> str | None:
        """Decode Proxmox's URL-encoded ``sshkeys`` (newlines as ``%0A``).

        Proxmox stores the blob URL-encoded; if nothing decodes it before
        the row is written, NetBox ends up with a single-line ``%0A``-laced
        string. Truncate decoded payloads to 10 KB plus a sentinel suffix.
        """
        if value is None:
            return None
        text = value if isinstance(value, str) else str(value)
        decoded = unquote(text)
        encoded = decoded.encode("utf-8")
        if len(encoded) <= CLOUDINIT_SSHKEYS_MAX_BYTES:
            return decoded
        truncated = encoded[:CLOUDINIT_SSHKEYS_MAX_BYTES].decode("utf-8", "ignore")
        return truncated + CLOUDINIT_SSHKEYS_TRUNCATION_SENTINEL

    @model_validator(mode="after")
    def _mark_sshkeys_truncated(self) -> ProxmoxVmConfigInput:
        if self.sshkeys and CLOUDINIT_SSHKEYS_TRUNCATION_SENTINEL in self.sshkeys:
            self.sshkeys_truncated = True
        return self

    @computed_field(return_type=bool)
    @property
    def start_at_boot(self) -> bool:
        return _as_bool(self.onboot)

    @computed_field(return_type=list[str])
    @property
    def proxmox_tags(self) -> list[str]:
        return parse_proxmox_tags(self.tags)

    @computed_field(return_type=bool)
    @property
    def qemu_agent_enabled(self) -> bool:
        return _parse_proxmox_kv_flag(self.agent)

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

    def cloudinit_payload(self) -> dict[str, object] | None:
        """Return the cloud-init reflection payload, or ``None`` if absent."""
        has_data = bool(self.ciuser or self.sshkeys or self.ipconfig0)
        if not has_data:
            return None
        return {
            "ciuser": self.ciuser or "",
            "sshkeys": self.sshkeys or "",
            "ipconfig0": self.ipconfig0 or "",
            "sshkeys_truncated": self.sshkeys_truncated,
        }


class NetBoxVirtualMachineCreateBody(BaseModel):
    """Validated NetBox create body for virtualization virtual machine endpoint."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    status: str
    cluster: int | None = None
    device: int | None = None
    site: int | None = None
    # Issue #365: tenant assignment is owned by the netbox-proxbox plugin via
    # name-regex mapping; proxbox-api never sends tenant on the create body.
    virtual_machine_type: int | None = None
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
        return ProxmoxToNetBoxVMStatus.from_proxmox(value or "active")

    @field_validator(
        "cluster",
        "device",
        "site",
        "virtual_machine_type",
        "role",
        mode="before",
    )
    @classmethod
    def normalize_relations(cls, value: object) -> object:
        return _relation_id(value)

    @model_validator(mode="after")
    def validate_required_relations(self):
        if self.cluster is not None and self.cluster <= 0:
            raise ValueError("cluster must be a positive NetBox object id")
        if self.device is not None and self.device <= 0:
            raise ValueError("device must be positive when provided")
        if self.site is not None and self.site <= 0:
            raise ValueError("site must be positive when provided")
        if self.virtual_machine_type is not None and self.virtual_machine_type <= 0:
            raise ValueError("virtual_machine_type must be positive when provided")
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


class NetBoxVirtualMachineTypeSyncState(BaseModel):
    """Validated NetBox sync body for virtualization virtual-machine-types endpoint (NetBox v4.6+)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    slug: str
    description: str | None = None
    tags: list[NetBoxTagRef] = Field(default_factory=list)


class ProxmoxToNetBoxVirtualMachine(BaseModel):
    """Schema-driven transform object for Proxmox input to NetBox VM create payload."""

    model_config = ConfigDict(extra="forbid")

    resource: ProxmoxVmResourceInput
    config: ProxmoxVmConfigInput = Field(default_factory=ProxmoxVmConfigInput)
    cluster_id: int
    device_id: int | None = None
    site_id: int | None = None
    tenant_id: int | None = None
    role_id: int | None = None
    virtual_machine_type_id: int | None = None
    tag_ids: list[int] = Field(default_factory=list)
    last_updated: datetime | None = None
    cluster_name: str | None = None
    proxmox_url: str | None = None

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
            "proxmox_node": self.resource.node,
            "proxmox_status": self.resource.status,
        }
        if self.cluster_name:
            fields["proxmox_cluster"] = self.cluster_name
        if self.proxmox_url:
            fields["proxmox_link"] = f"{self.proxmox_url}/#v1:0:={vm_type}/{self.resource.vmid}"
        if self.last_updated:
            fields["proxmox_last_updated"] = self.last_updated.isoformat()
        return fields

    @computed_field(return_type=int)
    @property
    def disk_mb(self) -> int:
        """VM disk in MiB; always equals the aggregate of parsed VM config disks.

        We deliberately do NOT fall back to ``self.resource.disk_mb`` (the
        cluster ``maxdisk``) when no disks parsed: NetBox 4.5+ enforces that
        ``virtualmachine.disk`` matches the sum of its attached
        ``virtual-disks`` on update, and virtual-disks are POSTed from the
        same parsed list. Falling back to ``maxdisk`` while POSTing zero
        virtual-disks (the all-passthrough case) makes the next sync fail
        with::

            {"disk":["The specified disk size must match the aggregate size
              of assigned virtual disks."]}

        Returning ``0`` instead is safe because NetBox's aggregate validator
        short-circuits when ``total_disk`` is falsy.
        """
        disks = self.config.disks
        if not disks:
            return 0
        return sum(max(int(getattr(disk, "size", 0) or 0), 0) for disk in disks)

    def _resolve_description_metadata(
        self,
        *,
        overwrite_flags: object | None,
    ) -> tuple[dict[str, int], str]:
        """Parse and apply opt-in ``netbox-metadata`` JSON from the VM description.

        Returns ``(applied_known_overrides, description_text)``. Logs applied,
        gated, and unknown keys so operators can see why an override didn't
        land without scanning the full sync output.
        """
        from proxbox_api.logger import logger
        from proxbox_api.proxmox_to_netbox.description_metadata import (
            filter_metadata_by_overwrite_flags,
            parse_netbox_metadata,
            strip_netbox_metadata,
        )

        default_description = f"Synced from Proxmox node {self.resource.node}"
        raw_description = (
            self.config.model_extra.get("description")
            if isinstance(self.config.model_extra, dict)
            else None
        )
        if not isinstance(raw_description, str):
            return {}, default_description

        parsed = parse_netbox_metadata(raw_description)
        applied, dropped = filter_metadata_by_overwrite_flags(
            parsed, overwrite_flags, object_kind="vm"
        )
        known_fields = set(NetBoxVirtualMachineCreateBody.model_fields)
        applied_known = {k: v for k, v in applied.items() if k in known_fields}
        unknown = sorted(set(applied) - known_fields)
        stripped = strip_netbox_metadata(raw_description)
        description_text = stripped or default_description

        if applied_known:
            logger.info(
                "netbox-metadata applied to vm %s: %s",
                self.resource.name,
                sorted(applied_known),
            )
        if unknown:
            logger.warning(
                "netbox-metadata unknown keys dropped on vm %s: %s",
                self.resource.name,
                unknown,
            )
        if dropped:
            logger.info(
                "netbox-metadata keys gated off by overwrite flags on vm %s: %s",
                self.resource.name,
                dropped,
            )
        return applied_known, description_text

    def as_netbox_create_body(
        self,
        *,
        parse_description_metadata: bool = False,
        overwrite_flags: object | None = None,
    ) -> NetBoxVirtualMachineCreateBody:
        """Return validated NetBox virtual machine create body.

        When ``parse_description_metadata`` is true, the Proxmox VM
        ``description`` is scanned for a ``netbox-metadata`` JSON block whose
        positive-integer PK values override the resolved foreign keys on the
        create body (see issue #366). Keys whose matching ``overwrite_vm_<key>``
        flag is off are dropped; unknown keys are dropped with a warning log
        because ``NetBoxVirtualMachineCreateBody`` is ``extra="forbid"``. The
        block is stripped from the description that is actually written to
        NetBox so the JSON noise does not leak into the rendered UI.
        """

        # NetBox stores ``virtualmachine.disk`` in a 32-bit ``PositiveIntegerField``
        # (max 2_147_483_647). Clamp defensively so a future bytes-vs-MiB
        # regression cannot produce ``Ensure this value is less than or equal
        # to the allowed maximum`` errors mid-sync.
        _NETBOX_DISK_MAX = 2_147_483_647
        disk_value = self.disk_mb
        if disk_value > _NETBOX_DISK_MAX:
            from proxbox_api.logger import logger

            logger.warning(
                "VM %s disk_mb=%d exceeds NetBox max %d; clamping",
                self.resource.name,
                disk_value,
                _NETBOX_DISK_MAX,
            )
            disk_value = _NETBOX_DISK_MAX

        if parse_description_metadata:
            applied_known, description_text = self._resolve_description_metadata(
                overwrite_flags=overwrite_flags,
            )
        else:
            applied_known = {}
            description_text = f"Synced from Proxmox node {self.resource.node}"

        body_kwargs: dict[str, object] = dict(
            name=self.resource.name,
            status=self.resource.status,
            cluster=self.cluster_id,
            device=self.device_id,
            site=self.site_id,
            virtual_machine_type=self.virtual_machine_type_id,
            role=self.role_id,
            vcpus=int(self.resource.maxcpu or 0),
            memory=self.resource.memory_mb,
            disk=disk_value,
            tags=[tag for tag in self.tag_ids if int(tag) > 0],
            custom_fields=self.vm_custom_fields,
            description=description_text,
        )
        body_kwargs.update(applied_known)
        return NetBoxVirtualMachineCreateBody(**body_kwargs)
