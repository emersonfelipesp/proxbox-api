"""Pydantic schemas for Proxmox sessions and resource payloads."""

from __future__ import annotations

from pydantic import RootModel, field_validator

from proxbox_api.enum.proxmox import NodeStatus, ResourceType
from proxbox_api.schemas._base import ProxboxBaseModel, ProxboxLenientModel


def _normalize_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _normalize_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


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

    @field_validator("name", "ip_address", "domain", "user", "password", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> str | None:
        return _normalize_text(value)

    @field_validator("http_port", mode="before")
    @classmethod
    def normalize_http_port(cls, value: object) -> int | None:
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
    cgroup_mode: int | None = None
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
    status: str | None = None
    storage: str | None = None
    type: ResourceType | str
    uptime: int | None = None
    vmid: int | None = None

    @field_validator(
        "cgroup_mode",
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
        "status",
        "storage",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value: object) -> str | None:
        return _normalize_text(value)

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
