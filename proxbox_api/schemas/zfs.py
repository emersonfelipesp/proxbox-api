"""Typed ZFS storage inventory contracts exposed to netbox-proxbox."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from proxbox_api.schemas._base import ProxboxBaseModel

ZfsTierName = Literal["proxmox_api", "influxdb", "ssh_cli"]
ZfsAttemptStatus = Literal["success", "failed", "skipped"]


class ZfsDataSourceAttempt(ProxboxBaseModel):
    """One attempted ZFS data-source tier."""

    tier: ZfsTierName
    status: ZfsAttemptStatus
    reason: str | None = None


class ZfsVdevNode(ProxboxBaseModel):
    """One node in the Proxmox ZFS vdev tree."""

    name: str | None = None
    state: str | None = None
    read: int | None = None
    write: int | None = None
    cksum: int | None = None
    msg: str | None = None
    children: list["ZfsVdevNode"] = Field(default_factory=list)


class ZfsPoolSummary(ProxboxBaseModel):
    """Capacity and health summary for one ZFS pool on one Proxmox node."""

    cluster_name: str
    node: str
    name: str
    size: int | None = None
    alloc: int | None = None
    free: int | None = None
    frag: int | None = None
    dedup: float | None = None
    health: str | None = None
    source: ZfsTierName = "proxmox_api"


class ZfsPoolDetail(ZfsPoolSummary):
    """Detailed ZFS pool state, scrub status, and vdev topology."""

    state: str | None = None
    status: str | None = None
    action: str | None = None
    scan: str | None = None
    errors: str | None = None
    children: list[ZfsVdevNode] = Field(default_factory=list)


class ZfsPoolsResponse(ProxboxBaseModel):
    """Aggregated ZFS pool summaries and tier-selection metadata."""

    source: ZfsTierName | None = None
    attempted_sources: list[ZfsDataSourceAttempt] = Field(default_factory=list)
    pools: list[ZfsPoolSummary] = Field(default_factory=list)


class ZfsPoolDetailResponse(ProxboxBaseModel):
    """Aggregated ZFS pool details and tier-selection metadata."""

    source: ZfsTierName | None = None
    attempted_sources: list[ZfsDataSourceAttempt] = Field(default_factory=list)
    pools: list[ZfsPoolDetail] = Field(default_factory=list)
