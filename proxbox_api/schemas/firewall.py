"""Pydantic schemas for Proxmox firewall write routes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FirewallZone = Literal[
    "datacenter",
    "node",
    "vm_qemu",
    "vm_lxc",
    "security_group",
    "vnet",
]
FirewallVmType = Literal["qemu", "lxc"]


class FirewallWriteModel(BaseModel):
    """Base model that accepts either Python or Proxmox-style field names."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    def pve_payload(self, *, exclude: set[str] | None = None) -> dict[str, Any]:
        """Return a Proxmox API payload with unset/None fields removed."""
        payload = self.model_dump(
            mode="python",
            by_alias=True,
            exclude_none=True,
            exclude=exclude or set(),
        )
        return {key: value for key, value in payload.items() if value is not None}


class FirewallRuleWrite(FirewallWriteModel):
    """Payload for creating or updating a firewall rule."""

    pos: int | None = None
    type: str | None = None
    action: str | None = None
    enable: bool | int | None = None
    macro: str | None = None
    iface: str | None = None
    source: str | None = None
    dest: str | None = None
    proto: str | None = None
    dport: str | None = None
    sport: str | None = None
    log: str | None = None
    icmp_type: str | None = Field(default=None, alias="icmp-type")
    comment: str | None = None
    digest: str | None = None


class FirewallSecurityGroupWrite(FirewallWriteModel):
    """Payload for creating a datacenter security group."""

    group: str | None = None
    name: str | None = None
    comment: str | None = None

    def pve_payload(self, *, exclude: set[str] | None = None) -> dict[str, Any]:
        payload = super().pve_payload(exclude=exclude)
        if "group" not in payload and payload.get("name"):
            payload["group"] = payload.pop("name")
        else:
            payload.pop("name", None)
        return payload


class FirewallIPSetWrite(FirewallWriteModel):
    """Payload for creating a firewall IP set."""

    name: str
    comment: str | None = None


class FirewallIPSetEntryWrite(FirewallWriteModel):
    """Payload for creating or updating an IP set entry."""

    cidr: str | None = None
    comment: str | None = None
    nomatch: bool | int | None = None


class FirewallAliasWrite(FirewallWriteModel):
    """Payload for creating or updating a firewall alias."""

    name: str | None = None
    cidr: str
    comment: str | None = None
    digest: str | None = None


class FirewallOptionsWrite(FirewallWriteModel):
    """Payload for updating firewall options."""

    enable: bool | int | None = None
    policy_in: str | None = None
    policy_out: str | None = None
    log_ratelimit: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    def pve_payload(self, *, exclude: set[str] | None = None) -> dict[str, Any]:
        payload = super().pve_payload(exclude=(exclude or set()) | {"options"})
        payload.update(self.options)
        return {key: value for key, value in payload.items() if value is not None}


class FirewallWriteResponse(BaseModel):
    """Common response for firewall mutation routes."""

    status: Literal["pushed", "deleted", "skipped"]
    endpoint_id: int | None = None
    cluster_name: str | None = None
    actor: str
    path: str
    reason: str | None = None
    detail: str | None = None
    proxmox_task_id: str | None = None
    response: Any = None
