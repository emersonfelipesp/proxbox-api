"""Ceph v2 desired-state orchestration API schemas."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ProviderName = Literal["proxmox", "dashboard", "rgw_admin", "rbd", "prometheus", "external"]
ValidationSeverity = Literal["info", "warning", "error"]
OperationStatus = Literal[
    "pending",
    "running",
    "dispatching",
    "completed",
    "failed",
    "blocked",
    "cancelled",
    "outcome_unknown",
]


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

_OPAQUE_CREDENTIAL_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")
_SECRET_FIELD_FRAGMENTS = (
    "access_key",
    "api_key",
    "authentication",
    "authorization",
    "cookie",
    "credential",
    "passphrase",
    "password",
    "passwd",
    "private_key",
    "rgw_access_key",
    "secret",
    "token",
)
_SECRET_FIELD_NAMES = {"auth", "key", "keys", "pwd", "set_cookie"}
_TARGET_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?:^|[?&\s;,])(?:api[_-]?key|(?:api|access)[_-]?tokens?|client[_-]?secrets?|auth(?:entication|orization)?|cookie|"
    r"credentials?|keys?|pass(?:phrase|word|wd)?|pwd|secret|tokens?|"
    r"(?:rgw[_-]?)?access[_-]?key|private[_-]?key)\s*[:=]",
    re.IGNORECASE,
)


def normalized_field_name(value: object) -> str:
    """Canonicalize snake/kebab/spaced/camel-case field names alike."""

    raw = str(value).strip()
    camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw)
    acronym_split = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", camel_split)
    return re.sub(r"[^a-z0-9]+", "_", acronym_split.lower()).strip("_")


def is_secret_field_name(value: object) -> bool:
    """Return whether an untrusted mapping key denotes secret material."""

    normalized = normalized_field_name(value)
    return normalized in _SECRET_FIELD_NAMES or any(
        fragment in normalized for fragment in _SECRET_FIELD_FRAGMENTS
    )


def validate_credential_ref(value: object) -> str:
    """Validate a credential pointer as an opaque identifier, never a secret/URL."""

    ref = str(value or "").strip()
    if not ref or not _OPAQUE_CREDENTIAL_REF_RE.fullmatch(ref) or "://" in ref or "@" in ref:
        raise ValueError(
            "credential_ref must be an opaque 1-255 character identifier, not a URL or secret"
        )
    return ref


def sanitize_operation_value(value: Any) -> Any:
    """Remove secret values from operation-owned nested mappings.

    ``credential_ref`` is the only credential-shaped field retained and is
    validated as a safe opaque pointer. All other credential/key/token aliases
    keep their key for diagnostics but never retain their value.
    """

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            normalized = normalized_field_name(name)
            if normalized == "credential_ref":
                safe[name] = validate_credential_ref(item)
            elif is_secret_field_name(name):
                safe[name] = "[REDACTED]"
            else:
                safe[name] = sanitize_operation_value(item)
        return safe
    if isinstance(value, list | tuple | set):
        return [sanitize_operation_value(item) for item in value]
    return value


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

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    provider: str = "proxmox"
    kind: str = Field(..., min_length=1)
    target_ref: str = ""
    action: str = "ensure"
    node: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
    )
    is_destructive: bool = False
    supported: bool = True
    blocked_reason: str | None = None
    before_summary: dict[str, Any] = Field(default_factory=dict)
    after_summary: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _coerce_operation(cls, data: Any) -> Any:  # noqa: C901 - compatibility canonicalizer
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
        after_summary = values.get("after_summary")
        metadata = values.get("metadata")
        if values.get("node") is None:
            for candidate in (after_summary, metadata):
                if isinstance(candidate, dict) and candidate.get("node") not in (None, ""):
                    values["node"] = candidate["node"]
                    break
        # ``node`` used to arrive inside the untyped payload. Canonicalize that
        # compatibility shape into the immutable top-level plan binding so it
        # is validated and never silently discarded by the writer.
        if isinstance(after_summary, dict) and "node" in after_summary:
            values["after_summary"] = {
                key: item for key, item in after_summary.items() if key != "node"
            }
        for key in ("before_summary", "after_summary", "metadata"):
            raw = values.get(key)
            if isinstance(raw, dict):
                values[key] = sanitize_operation_value(raw)
        return values

    @field_validator("target_ref")
    @classmethod
    def _reject_secret_target_ref(cls, value: str) -> str:
        cleaned = value.strip()
        lowered = cleaned.casefold()
        if (
            "@" in cleaned
            or "bearer " in lowered
            or any(f"{name}=" in lowered for name in _SECRET_FIELD_FRAGMENTS)
            or _TARGET_SECRET_ASSIGNMENT_RE.search(cleaned)
        ):
            raise ValueError("target_ref must not contain credentials or secret material")
        return cleaned


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
    endpoint_id: int | None = Field(default=None, gt=0)
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
    endpoint_id: int | None = None
    endpoint_config_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    requester: str | None = None
    digest: str = ""
    netbox_branch_schema_id: str | None = None
    source_branch_schema_id: str | None = None
    operations: list[ProviderOperation] = Field(default_factory=list)
    validations: list[ValidationResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocked_actions: list[ProviderOperation] = Field(default_factory=list)
    created_at: datetime
    expires_at: datetime
    live_state_summary: dict[str, Any] = Field(default_factory=dict)
    request_summary: dict[str, Any] = Field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return not any(item.severity == "error" for item in self.validations)


class ApplyRequest(BaseModel):
    """Apply one immutable persisted plan with a one-time approval token.

    Legacy inline fields remain parseable for a stable 409 migration response,
    but routes reject them as apply authority.
    """

    model_config = ConfigDict(extra="allow")

    provider: str = "proxmox"
    plan_id: str | None = None
    endpoint_id: int | None = Field(default=None, gt=0)
    approval_token: str | None = None
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


class ApprovalRequest(BaseModel):
    """Bind an approval request to the endpoint persisted in a canonical plan."""

    endpoint_id: int | None = Field(default=None, gt=0)


class ApprovalResponse(BaseModel):
    """One-time approval credential; ``token`` is returned only at creation."""

    id: str
    plan_id: str
    plan_digest: str
    endpoint_id: int | None = None
    endpoint_config_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    requester: str
    approver: str
    token: str
    expires_at: datetime


class ApprovalStatusResponse(BaseModel):
    """Safe durable approval metadata; never includes raw token or token hash."""

    id: str
    plan_id: str
    plan_digest: str
    endpoint_id: int | None = None
    endpoint_config_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    requester: str
    approver: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    consumed_by: str | None = None
    operation_run_id: str | None = None


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    provider: str = "proxmox"
    endpoint_id: int | None = Field(default=None, gt=0)
    scope: dict[str, Any] = Field(default_factory=dict)
    actor: str | None = None
    netbox_branch_schema_id: str | None = None
    source_branch_schema_id: str | None = None

    @property
    def branch_schema_id(self) -> str | None:
        return self.netbox_branch_schema_id or self.source_branch_schema_id


class OperationEvent(BaseModel):
    """One ordered, append-only dispatch or provider-task transition."""

    sequence: int
    operation_index: int | None = None
    operation_id: str | None = None
    event: str
    status: OperationStatus
    code: str
    message: str
    kind: str | None = None
    action: str | None = None
    target_ref: str | None = None
    provider_task_ref: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class OperationRun(BaseModel):
    """Persisted Ceph v2 operation run."""

    id: str
    plan_id: str | None = None
    endpoint_id: int | None = None
    endpoint_config_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    plan_digest: str | None = None
    requester: str | None = None
    approver: str | None = None
    approval_id: str | None = None
    status: OperationStatus
    actor: str | None = None
    source_branch_schema_id: str | None = None
    provider: str
    request_summary: dict[str, Any] = Field(default_factory=dict)
    provider_task_refs: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    lease_expires_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    result_summary: dict[str, Any] = Field(default_factory=dict)
    events: list[OperationEvent] = Field(default_factory=list)


class ProviderCapabilities(BaseModel):
    """Capability flags for one Ceph provider adapter."""

    provider: str
    endpoint_id: int | None = None
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
    "ApprovalRequest",
    "ApprovalResponse",
    "ApprovalStatusResponse",
    "ApplyRequest",
    "CapabilitiesResponse",
    "CephHealthStatus",
    "CephMetricSnapshot",
    "DesiredObject",
    "DesiredStateBundle",
    "MetricsResponse",
    "OperationRun",
    "OperationEvent",
    "PlanRequest",
    "PlanResponse",
    "ProviderCapabilities",
    "ProviderName",
    "ProviderOperation",
    "ReconcileRequest",
    "SSEEvent",
    "ValidationResponse",
    "ValidationResult",
    "is_secret_field_name",
    "normalized_field_name",
    "sanitize_operation_value",
    "validate_credential_ref",
]
