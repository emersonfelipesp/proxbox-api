"""Ceph v2 desired-state orchestration API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ProviderName = Literal["proxmox", "dashboard", "rgw_admin", "rbd", "prometheus", "external"]
ValidationSeverity = Literal["info", "warning", "error"]
OperationStatus = Literal["pending", "running", "completed", "failed", "blocked", "cancelled"]


_BUNDLE_KIND_KEYS = {
    "pools": "pool",
    "pool": "pool",
    "osds": "osd",
    "osd": "osd",
    "filesystems": "filesystem",
    "filesystems_cephfs": "filesystem",
    "rbd_images": "rbd_image",
    "rbd": "rbd_image",
    "rgw_buckets": "rgw_bucket",
    "buckets": "rgw_bucket",
    "crush_rules": "crush_rule",
    "crush": "crush_rule",
    "keys": "key",
    "users": "user",
}


class DesiredObject(BaseModel):
    """One NetBox desired-state object for Ceph orchestration."""

    model_config = ConfigDict(extra="allow")

    kind: str = Field(..., min_length=1)
    target_ref: str | None = None
    name: str | None = None
    action: str = "ensure"
    payload: dict[str, Any] = Field(default_factory=dict)
    provider: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _coerce_common_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        if values.get("target_ref") is None:
            values["target_ref"] = values.get("ref") or values.get("slug") or values.get("id")
        if values.get("name") is None and isinstance(values.get("payload"), dict):
            values["name"] = values["payload"].get("name")
        if values.get("target_ref") is None:
            values["target_ref"] = values.get("name")
        if not isinstance(values.get("payload"), dict):
            spec = values.get("spec")
            values["payload"] = dict(spec) if isinstance(spec, dict) else {}
        if values.get("target_ref") is None and isinstance(values.get("payload"), dict):
            payload = values["payload"]
            values["target_ref"] = (
                payload.get("target_ref") or payload.get("ref") or payload.get("name")
            )
        return values


class DesiredStateBundle(BaseModel):
    """A desired-state bundle sent by NetBox."""

    model_config = ConfigDict(extra="allow")

    objects: list[DesiredObject] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _coerce_bundle(cls, data: Any) -> Any:
        if data is None:
            return {}
        if isinstance(data, list):
            return {"objects": data}
        if not isinstance(data, dict):
            return data
        values = dict(data)
        if "objects" in values:
            return values

        objects: list[dict[str, Any]] = []
        for key, kind in _BUNDLE_KIND_KEYS.items():
            raw_items = values.get(key)
            if not isinstance(raw_items, list):
                continue
            for raw_item in raw_items:
                if isinstance(raw_item, dict):
                    item = dict(raw_item)
                    item.setdefault("kind", kind)
                    item.setdefault("payload", raw_item)
                    objects.append(item)
        if objects:
            values["objects"] = objects
        return values


class ValidationResult(BaseModel):
    """One validation message for a desired object, bundle, or provider operation."""

    severity: ValidationSeverity
    code: str
    message: str
    target: str | None = None


class ValidationResponse(BaseModel):
    valid: bool
    results: list[ValidationResult] = Field(default_factory=list)


class ProviderOperation(BaseModel):
    """One intended provider operation in a deterministic Ceph v2 plan."""

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    provider: str = "proxmox"
    kind: str = Field(..., min_length=1)
    target_ref: str = ""
    action: str = "ensure"
    is_destructive: bool = False
    supported: bool = True
    blocked_reason: str | None = None
    before_summary: dict[str, Any] = Field(default_factory=dict)
    after_summary: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _coerce_operation(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        if values.get("target_ref") is None:
            values["target_ref"] = values.get("ref") or values.get("target") or values.get("name")
        if "before_summary" not in values and isinstance(values.get("before"), dict):
            values["before_summary"] = values["before"]
        if "after_summary" not in values:
            after = values.get("after") or values.get("payload") or values.get("spec")
            if isinstance(after, dict):
                values["after_summary"] = after
        return values


def _coerce_netbox_operation_payload(values: dict[str, Any]) -> dict[str, Any]:
    """Adapt the ``netbox-ceph`` ``CephOperation`` HTTP shape into a bundle.

    The ``netbox-ceph`` orchestrator posts a single operation shaped like
    ``{operation_type, target_kind, target_ref, desired, provider_kind,
    is_destructive, confirmed, ...}``. Map it to a one-object
    :class:`DesiredStateBundle` (plus a ``provider`` hint) so the plan engine can
    build a real :class:`ProviderOperation` from it. Without this, ``desired``
    (a params dict) was treated as a bundle and produced zero objects.
    """
    if "target_kind" not in values:
        return values
    if values.get("desired_state") is not None or values.get("operations"):
        return values
    desired = values.get("desired")
    obj: dict[str, Any] = {
        "kind": values.get("target_kind"),
        "action": values.get("operation_type") or "ensure",
        "target_ref": values.get("target_ref"),
        "payload": dict(desired) if isinstance(desired, dict) else {},
    }
    provider_kind = values.get("provider_kind")
    if provider_kind:
        obj["provider"] = provider_kind
        values.setdefault("provider", provider_kind)
    values["desired_state"] = {"objects": [obj]}
    return values


def _coerce_operations_and_desired(values: dict[str, Any]) -> dict[str, Any]:
    """Shared plan/apply coercion: inline operation, desired_state, branch id."""
    if "operation" in values and "operations" not in values:
        values["operations"] = [values["operation"]]
    elif (
        "operations" not in values
        and "kind" in values
        and ("action" in values or "target_ref" in values or "ref" in values)
    ):
        values["operations"] = [values]

    if "desired_state" not in values:
        if "desired" in values:
            values["desired_state"] = values["desired"]
        elif "desired_objects" in values:
            values["desired_state"] = {"objects": values["desired_objects"]}
        elif "objects" in values or any(key in values for key in _BUNDLE_KIND_KEYS):
            values["desired_state"] = values

    if values.get("source_branch_schema_id") is None:
        values["source_branch_schema_id"] = values.get("netbox_branch_schema_id")
    return values


class PlanRequest(BaseModel):
    """Build a Ceph v2 plan from NetBox desired state and provider state."""

    model_config = ConfigDict(extra="allow")

    provider: str = "proxmox"
    desired_state: DesiredStateBundle = Field(default_factory=DesiredStateBundle)
    operations: list[ProviderOperation] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    actor: str | None = None
    netbox_branch_schema_id: str | None = None
    source_branch_schema_id: str | None = None
    request_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_request(cls, data: Any) -> Any:
        if data is None:
            return {}
        if not isinstance(data, dict):
            return data
        values = _coerce_netbox_operation_payload(dict(data))
        return _coerce_operations_and_desired(values)

    @property
    def branch_schema_id(self) -> str | None:
        return self.netbox_branch_schema_id or self.source_branch_schema_id


class PlanResponse(BaseModel):
    """Deterministic Ceph v2 plan response."""

    id: str
    provider: str
    netbox_branch_schema_id: str | None = None
    source_branch_schema_id: str | None = None
    operations: list[ProviderOperation] = Field(default_factory=list)
    validations: list[ValidationResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocked_actions: list[ProviderOperation] = Field(default_factory=list)
    created_at: datetime
    live_state_summary: dict[str, Any] = Field(default_factory=dict)
    request_summary: dict[str, Any] = Field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return not any(item.severity == "error" for item in self.validations)


class ApplyRequest(BaseModel):
    """Apply an existing plan or an inline operation payload."""

    model_config = ConfigDict(extra="allow")

    provider: str = "proxmox"
    plan_id: str | None = None
    desired_state: DesiredStateBundle | None = None
    operations: list[ProviderOperation] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    actor: str | None = None
    netbox_branch_schema_id: str | None = None
    source_branch_schema_id: str | None = None
    confirm_destructive: bool = False
    confirmation_token: str | None = None
    request_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_apply(cls, data: Any) -> Any:
        if data is None:
            return {}
        if not isinstance(data, dict):
            return data
        values = _coerce_netbox_operation_payload(dict(data))
        if "confirm_destructive" not in values and "confirmed" in values:
            values["confirm_destructive"] = bool(values.get("confirmed"))
        return _coerce_operations_and_desired(values)

    @property
    def branch_schema_id(self) -> str | None:
        return self.netbox_branch_schema_id or self.source_branch_schema_id


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider: str = "proxmox"
    scope: dict[str, Any] = Field(default_factory=dict)
    actor: str | None = None
    netbox_branch_schema_id: str | None = None
    source_branch_schema_id: str | None = None

    @property
    def branch_schema_id(self) -> str | None:
        return self.netbox_branch_schema_id or self.source_branch_schema_id


class OperationRun(BaseModel):
    """Persisted Ceph v2 operation run."""

    id: str
    plan_id: str | None = None
    status: OperationStatus
    actor: str | None = None
    source_branch_schema_id: str | None = None
    provider: str
    request_summary: dict[str, Any] = Field(default_factory=dict)
    provider_task_refs: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    result_summary: dict[str, Any] = Field(default_factory=dict)


class ProviderCapabilities(BaseModel):
    """Capability flags for one Ceph provider adapter."""

    provider: str
    supported: bool
    read_state: bool = False
    diff: bool = False
    plan: bool = False
    apply: bool = False
    reconcile: bool = False
    metrics: bool = False
    operation_kinds: dict[str, bool] = Field(default_factory=dict)
    destructive_operations: bool = False
    notes: list[str] = Field(default_factory=list)


class CapabilitiesResponse(BaseModel):
    providers: list[ProviderCapabilities]


CephHealthStatus = Literal["HEALTH_OK", "HEALTH_WARN", "HEALTH_ERR", "unknown"]


class CephMetricSnapshot(BaseModel):
    """Bounded, latest-only Ceph metric snapshot normalized from Prometheus.

    Deliberately stores a single current snapshot (not a time series): the
    fields needed for health, capacity, and drift/safety decisions. ``unknown``
    health and ``None`` metric values mean "no data" rather than zero.
    """

    cluster_health: CephHealthStatus = "unknown"
    captured_at: datetime
    source_url: str | None = None
    # capacity
    bytes_total: int | None = None
    bytes_used: int | None = None
    bytes_avail: int | None = None
    percent_used: float | None = None
    # daemons
    osd_up: int | None = None
    osd_in: int | None = None
    osd_total: int | None = None
    mon_quorum: int | None = None
    mgr_active: int | None = None
    # placement groups
    pgs_total: int | None = None
    pg_states: dict[str, int] = Field(default_factory=dict)
    degraded_pgs: int | None = None
    misplaced_pgs: int | None = None
    recovering_pgs: int | None = None
    # performance
    iops_read: float | None = None
    iops_write: float | None = None
    throughput_read_bps: float | None = None
    throughput_write_bps: float | None = None
    # pools / rgw / cephfs counters
    pools: int | None = None
    warnings: list[str] = Field(default_factory=list)

    @property
    def is_degraded(self) -> bool:
        """True when health is non-OK or recovery/backfill is in flight."""
        if self.cluster_health in ("HEALTH_WARN", "HEALTH_ERR"):
            return True
        for value in (self.degraded_pgs, self.misplaced_pgs, self.recovering_pgs):
            if value:
                return True
        return False


class MetricsResponse(BaseModel):
    provider: str
    scope: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    snapshot: CephMetricSnapshot | None = None
    warnings: list[str] = Field(default_factory=list)


class SSEEvent(BaseModel):
    """Ceph v2 operation-progress event payload."""

    event: str
    operation_id: str
    status: OperationStatus
    message: str
    sequence: int
    timestamp: datetime
    data: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "ApplyRequest",
    "CapabilitiesResponse",
    "CephHealthStatus",
    "CephMetricSnapshot",
    "DesiredObject",
    "DesiredStateBundle",
    "MetricsResponse",
    "OperationRun",
    "PlanRequest",
    "PlanResponse",
    "ProviderCapabilities",
    "ProviderName",
    "ProviderOperation",
    "ReconcileRequest",
    "SSEEvent",
    "ValidationResponse",
    "ValidationResult",
]
