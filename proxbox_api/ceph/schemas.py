"""Public response shapes for the read-only ``/ceph/*`` routes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

CephSyncResource = Literal[
    "status",
    "daemons",
    "osds",
    "pools",
    "filesystems",
    "crush",
    "flags",
    "full",
]


class CephStatusItem(BaseModel):
    """One Proxmox endpoint's Ceph reachability/health snapshot."""

    name: str
    host: str | None = None
    port: int | None = None
    reachable: bool
    health: str | dict[str, Any] | None = None
    fsid: str | None = None
    reason: str | None = None


class CephStatusResponse(BaseModel):
    items: list[CephStatusItem]


class CephSyncSummary(BaseModel):
    """Per-session per-resource sync stats returned by ``/ceph/sync/*``."""

    name: str
    host: str | None = None
    resource: CephSyncResource
    fetched: int = Field(default=0, ge=0)
    written: int = Field(default=0, ge=0)
    errors: list[str] = Field(default_factory=list)
    nodes: list[str] = Field(default_factory=list)
    netbox_branch_schema_id: str | None = None


class CephSyncResponse(BaseModel):
    items: list[CephSyncSummary]
    raw: dict[str, Any] | None = None


__all__ = [
    "CephStatusItem",
    "CephStatusResponse",
    "CephSyncResource",
    "CephSyncResponse",
    "CephSyncSummary",
]
