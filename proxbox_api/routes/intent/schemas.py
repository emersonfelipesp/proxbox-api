"""Pydantic schemas for the NetBox→Proxmox intent endpoints.

Sub-PR D (#381) introduces the ``/intent/plan`` request/response shapes.
Sub-PRs F (CREATE) and G (UPDATE) extend ``IntentDiff`` with the actual
payload fields and add ``ApplyResult``/``ApplyResponse``. Sub-PR I adds
the deletion-request payloads. All extensions land in this module so the
plugin and backend share one source of truth.

Per the workspace rule, this module does NOT use
``from __future__ import annotations`` — Pydantic v2 needs the runtime
types resolvable for the JSON-schema generator.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DiffOp = Literal["create", "update", "delete"]
VMKind = Literal["virtualmachine", "lxc"]
Verdict = Literal["permitted", "blocked", "warning"]


class IntentDiff(BaseModel):
    """One classified ChangeDiff row from a netbox-branching branch.

    The plugin builds this from ``branch.changediff_set``; the backend
    treats it as opaque except for the fields it needs to validate
    (``vmid``, ``proxmox_node``, ``proxmox_storage``, ``proxmox_iso``,
    ``proxmox_template_vmid``, plus cloud-init fields from Sub-PR K).
    """

    model_config = ConfigDict(extra="allow")

    op: DiffOp = Field(..., description="Diff classification: create, update, or delete.")
    kind: VMKind = Field(..., description="NetBox object kind being mutated.")
    netbox_id: int | None = Field(
        default=None,
        description=(
            "NetBox PK of the object the diff targets. None for CREATE diffs "
            "(the row does not exist yet in main)."
        ),
    )
    name: str | None = Field(default=None, description="Object name from the diff.")
    proxmox_node: str | None = Field(
        default=None,
        description="Target Proxmox node from the operator-set CF.",
    )
    proxmox_storage: str | None = Field(
        default=None,
        description="Target Proxmox storage pool from the operator-set CF.",
    )
    proxmox_iso: str | None = Field(default=None, description="Optional install ISO volume id.")
    proxmox_template_vmid: int | None = Field(
        default=None,
        description="Optional clone source VMID (CREATE path).",
    )
    desired_vmid: int | None = Field(
        default=None,
        description="Operator-requested VMID; None means auto-allocate.",
    )


class PlanRequest(BaseModel):
    """Payload posted by netbox-proxbox's merge_validator.

    ``endpoint_id`` resolves the target ProxmoxEndpoint; ``branch_id``
    is the netbox-branching branch PK (for journal correlation only —
    the backend never reads NetBox state to plan).
    """

    model_config = ConfigDict(extra="forbid")

    endpoint_id: int = Field(..., description="ProxmoxEndpoint primary key.")
    branch_id: int | None = Field(default=None, description="netbox-branching branch PK.")
    actor: str | None = Field(default=None, description="NetBox username that triggered the merge.")
    diffs: list[IntentDiff] = Field(
        default_factory=list,
        description="Classified ChangeDiff rows to validate.",
    )


class PlanVerdict(BaseModel):
    """One per-diff verdict in the plan response."""

    model_config = ConfigDict(extra="forbid")

    netbox_id: int | None = Field(
        default=None,
        description="NetBox PK echoed from the request diff. None for CREATE.",
    )
    op: DiffOp = Field(..., description="Diff op being validated.")
    verdict: Verdict = Field(..., description="permitted | blocked | warning.")
    reason: str | None = Field(default=None, description="Short machine-friendly reason code.")
    message: str = Field(..., description="Human-readable explanation rendered in the UI.")


class PlanResponse(BaseModel):
    """Response from ``POST /intent/plan``.

    ``permitted`` is the conjunction of all per-diff verdicts: True only
    when every entry is permitted or warning. Blocked diffs flip it to
    False and netbox-branching's merge UI will refuse the merge.
    """

    model_config = ConfigDict(extra="forbid")

    permitted: bool = Field(..., description="False if any diff is blocked.")
    verdicts: list[PlanVerdict] = Field(default_factory=list)
    summary: str = Field(..., description="One-line summary rendered as the merge button hint.")


class VMIntentPayload(BaseModel):
    vmid: int
    node: str
    name: str
    cores: int | None = None
    memory_mib: int | None = None
    storage: str | None = None
    disks: list[dict] = Field(default_factory=list)
    nics: list[dict] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    cloud_init: dict | None = None
    template_vmid: int | None = None


class LXCIntentPayload(BaseModel):
    vmid: int
    node: str
    hostname: str
    cores: int | None = None
    memory_mib: int | None = None
    storage: str | None = None
    disks: list[dict] = Field(default_factory=list)
    nics: list[dict] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    ostemplate: str | None = None
    password: str | None = None


class ApplyDiff(BaseModel):
    op: Literal["create", "update", "delete"]
    kind: Literal["qemu", "lxc"]
    netbox_id: int | None = None
    payload: VMIntentPayload | LXCIntentPayload


class ApplyRequest(BaseModel):
    branch_id: int | None = None
    actor: str | None = None
    run_uuid: str
    diffs: list[ApplyDiff]


class ApplyResultItem(BaseModel):
    netbox_id: int | None = None
    vmid: int
    op: str
    kind: str
    status: Literal["succeeded", "failed", "skipped", "not_implemented"]
    message: str = ""
    proxmox_upid: str | None = None


class ApplyResponse(BaseModel):
    run_uuid: str
    overall: Literal["succeeded", "failed", "partial", "no_op"]
    results: list[ApplyResultItem]
