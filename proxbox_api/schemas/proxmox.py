"""Pydantic schemas for Proxmox sessions and resource payloads."""

from __future__ import annotations

from pydantic import BaseModel, Field, RootModel, field_validator

from proxbox_api.enum.proxmox import CgroupMode, NodeStatus, ProxmoxVMStatus, ResourceType
from proxbox_api.schemas._base import ProxboxBaseModel, ProxboxLenientModel
from proxbox_api.schemas._coerce import normalize_bool, normalize_int, normalize_text

# Keep module-level aliases so any existing imports of these helpers still work.
_normalize_text = normalize_text
_normalize_bool = normalize_bool  # type: ignore[assignment]
_normalize_int = normalize_int


class ProxmoxTokenSchema(ProxboxBaseModel):
    name: str | None = None
    value: str | None = None

    @field_validator("name", "value", mode="before")
    @classmethod
    def normalize_token_text(cls, value: object) -> str | None:
        return _normalize_text(value)


class ProxmoxSessionSchema(ProxboxBaseModel):
    name: str | None = None
    ip_address: str | None = None
    domain: str | None = None
    http_port: int | None = None
    user: str | None = None
    password: str | None = None
    token: ProxmoxTokenSchema | None = None
    ssl: bool = False
    timeout: int | None = Field(default=None, ge=1, le=3600)
    connect_timeout: int | None = Field(default=None, ge=1, le=3600)
    max_retries: int | None = Field(default=None, ge=0, le=100)
    retry_backoff: float | None = Field(default=None, ge=0.0, le=300.0)
    site_id: int | None = None
    site_slug: str | None = None
    site_name: str | None = None
    tenant_id: int | None = None
    tenant_slug: str | None = None
    tenant_name: str | None = None

    @field_validator(
        "name",
        "ip_address",
        "domain",
        "user",
        "password",
        "site_slug",
        "site_name",
        "tenant_slug",
        "tenant_name",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value: object) -> str | None:
        return _normalize_text(value)

    @field_validator("http_port", "site_id", "tenant_id", mode="before")
    @classmethod
    def normalize_optional_int(cls, value: object) -> int | None:
        return _normalize_int(value)

    @field_validator("ssl", mode="before")
    @classmethod
    def normalize_ssl(cls, value: object) -> bool:
        return _normalize_bool(value)


ProxmoxMultiClusterConfig = RootModel[list[ProxmoxSessionSchema]]


#
# /cluster
#
class Resources(ProxboxLenientModel):
    cgroup_mode: CgroupMode | int | None = None
    content: str | None = None
    cpu: float | None = None
    disk: int | None = None
    hastate: str | None = None
    id: str
    level: str | None = None
    maxcpu: float | None = None
    maxdisk: int | None = None
    maxmem: int | None = None
    mem: int | None = None
    name: str | None = None
    node: str | None = None
    plugintype: str | None = None
    pool: str | None = None
    status: ProxmoxVMStatus | str | None = None
    storage: str | None = None
    type: ResourceType | str
    uptime: int | None = None
    vmid: int | None = None

    @field_validator(
        "disk",
        "maxdisk",
        "maxmem",
        "mem",
        "uptime",
        "vmid",
        mode="before",
    )
    @classmethod
    def normalize_optional_int(cls, value: object) -> int | None:
        return _normalize_int(value)

    @field_validator("cgroup_mode", mode="before")
    @classmethod
    def normalize_cgroup_mode(cls, value: object) -> CgroupMode | int | None:
        if value in (None, ""):
            return None
        try:
            return CgroupMode(int(value))
        except (ValueError, TypeError):
            raw = _normalize_int(value)
            return raw

    @field_validator("cpu", "maxcpu", mode="before")
    @classmethod
    def normalize_optional_float(cls, value: object) -> float | None:
        if value in (None, ""):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    @field_validator(
        "content",
        "hastate",
        "level",
        "name",
        "node",
        "plugintype",
        "pool",
        "storage",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value: object) -> str | None:
        return _normalize_text(value)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> ProxmoxVMStatus | str | None:
        text = _normalize_text(value)
        if text is None:
            return None
        try:
            return ProxmoxVMStatus(text)
        except ValueError:
            return text

    @field_validator("type", mode="before")
    @classmethod
    def normalize_type(cls, value: object) -> ResourceType | str:
        text = _normalize_text(value)
        return text or "unknown"


ResourcesList = RootModel[list[Resources]]
ClusterResourcesList = RootModel[list[dict[str, ResourcesList]]]


#
# /nodes
#


class Node(ProxboxLenientModel):
    node: str
    status: NodeStatus | str
    cpu: float | None = None
    level: str | None = None
    maxcpu: int | None = None
    maxmem: int | None = None
    mem: int | None = None
    ssl_fingerprint: str | None = None
    uptime: int | None = None

    @field_validator("node", "level", "ssl_fingerprint", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> str | None:
        return _normalize_text(value)

    @field_validator("cpu", "maxcpu", "maxmem", "mem", "uptime", mode="before")
    @classmethod
    def normalize_optional_numbers(cls, value: object) -> int | float | None:
        normalized_int = _normalize_int(value)
        if normalized_int is not None:
            return normalized_int
        if value in (None, ""):
            return None
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    @field_validator("status", mode="before")
    @classmethod
    def normalize_status(cls, value: object) -> NodeStatus | str:
        text = _normalize_text(value)
        return text or "unknown"


NodeList = RootModel[list[Node]]
ResponseNodeList = RootModel[list[dict[str, ResourcesList]]]


class BaseClusterStatusSchema(BaseModel):
    id: str
    name: str
    type: str


class ClusterNodeStatusSchema(BaseClusterStatusSchema):
    ip: str
    level: str | None = None
    local: bool
    nodeid: int
    online: bool


class ClusterStatusSchema(BaseClusterStatusSchema):
    nodes: int
    quorate: bool
    version: int
    mode: str
    site_id: int | None = None
    site_slug: str | None = None
    site_name: str | None = None
    tenant_id: int | None = None
    tenant_slug: str | None = None
    tenant_name: str | None = None
    node_list: list[ClusterNodeStatusSchema] | None = None


ClusterStatusSchemaList = list[ClusterStatusSchema]
