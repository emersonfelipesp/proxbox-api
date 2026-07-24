"""Ceph v2 plan/apply engine."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ValidationError
from sqlalchemy import and_, or_
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from proxbox_api.ceph.v2_providers.base import (
    CephCapabilityUnsupported,
    CephProviderAdapter,
    CephProviderBoundaryError,
    CephWriteGateDenied,
    ceph_write_execution_enabled,
)
from proxbox_api.ceph.v2_schemas import (
    ApplyRequest,
    ApprovalResponse,
    CephMetricSnapshot,
    DesiredObject,
    DesiredStateBundle,
    OperationEvent,
    OperationRun,
    OperationStatus,
    PlanRequest,
    PlanResponse,
    ProviderCapabilities,
    ProviderOperation,
    ReconcileRequest,
    ValidationResponse,
    ValidationResult,
    is_secret_field_name,
    normalized_field_name,
    sanitize_operation_value,
    validate_credential_ref,
)
from proxbox_api.database import (
    CephApprovalRecord,
    CephOperationEventRecord,
    CephOperationRunRecord,
    CephPlanRecord,
    CephProviderTaskClaimRecord,
)
from proxbox_api.database_protocols import DatabaseSessionProtocol
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

PLAN_TTL_SECONDS = 15 * 60
APPROVAL_TTL_SECONDS = 10 * 60
_DEFAULT_RUN_LEASE_SECONDS = 6 * 60
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@",
    re.IGNORECASE,
)
_QUERY_SECRET_RE = re.compile(
    r"(?P<prefix>[?&](?:api[_-]?key|(?:api|access)[_-]?token|client[_-]?secret|auth(?:entication|orization)?|cookie|credential|key|pass(?:phrase|word|wd)?|pwd|secret|token|(?:rgw[_-]?)?access[_-]?key|private[_-]?key)=)[^&\s]+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[^\s,;]+", re.IGNORECASE)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>\b(?:api[_-]?key|(?:api|access)[_-]?tokens?|client[_-]?secrets?|auth(?:entication|orization)?|cookie|credentials?|keys?|pass(?:phrase|word|wd)?|pwd|secret|tokens?|(?:rgw[_-]?)?access[_-]?key|private[_-]?key)\s*[:=]\s*)"
    r"(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
_ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:/-]{0,127}$")
_PROXMOX_UPID_RE = re.compile(
    r"^UPID:"
    r"(?P<node>[A-Za-z0-9][A-Za-z0-9._-]{0,127}):"
    r"[0-9A-Fa-f]{1,16}:[0-9A-Fa-f]{1,16}:[0-9A-Fa-f]{1,16}:"
    r"[A-Za-z0-9][^:\s]{0,127}:"
    r"[^:\s]{0,255}:"
    r"[A-Za-z0-9][^:\s]{0,127}:"
    r"[^\r\n]{0,255}$"
)
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_DESTRUCTIVE_ACTIONS = {"delete", "destroy", "purge", "remove", "zap"}
_DESTRUCTIVE_KIND_ACTIONS = {
    "osd": _DESTRUCTIVE_ACTIONS,
    "pool": _DESTRUCTIVE_ACTIONS,
    "rbd": _DESTRUCTIVE_ACTIONS,
    "rbd_image": _DESTRUCTIVE_ACTIONS,
    "rgw_bucket": _DESTRUCTIVE_ACTIONS,
    "crush": _DESTRUCTIVE_ACTIONS,
    "crush_rule": _DESTRUCTIVE_ACTIONS,
    "key": _DESTRUCTIVE_ACTIONS,
    "user": _DESTRUCTIVE_ACTIONS,
}


class CephPlanNotFound(KeyError):
    """Raised when a plan id is not present in durable storage."""


class CephPlanIntegrityError(RuntimeError):
    """Raised when a persisted plan no longer matches its canonical digest."""


class CephPlanExpired(RuntimeError):
    """Raised when a persisted plan is outside its short validity window."""


class CephApprovalError(RuntimeError):
    """Route-facing approval error with a stable machine-readable reason."""

    def __init__(
        self,
        status_code: int,
        reason: str,
        detail: str,
        *,
        recovery: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.reason = reason
        self.detail = detail
        self.recovery = recovery or {}


class CephApplyError(RuntimeError):
    """Route-facing apply error with a persisted run when one was created."""

    def __init__(
        self,
        status_code: int,
        detail: dict[str, Any],
        run: OperationRun | None = None,
    ) -> None:
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail
        self.run = run


class _CephRunLeaseLost(RuntimeError):
    """The active worker no longer owns a live durable run lease."""

    def __init__(self, run_id: object) -> None:
        super().__init__(run_id)
        self.run_id = run_id


_TaskResultT = TypeVar("_TaskResultT")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt_from_ts(value: float) -> datetime:
    return datetime.fromtimestamp(value, timezone.utc)


def _redact_secret_text(value: str) -> str:
    redacted = _URL_USERINFO_RE.sub(r"\g<scheme>[REDACTED]@", value)
    redacted = _QUERY_SECRET_RE.sub(r"\g<prefix>[REDACTED]", redacted)
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    return _SECRET_ASSIGNMENT_RE.sub(r"\g<prefix>[REDACTED]", redacted)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(item) for item in value]
    try:
        json.dumps(value)
    except TypeError:
        return _redact_secret_text(str(value))
    return value


def normalize_actor(value: str | None) -> str:
    """Normalize a human/service identity and reject empty header values."""

    actor = (value or "").strip()
    if not actor:
        raise CephApprovalError(
            400,
            "actor_required",
            "A non-empty X-Proxbox-Actor header is required.",
        )
    if not _ACTOR_RE.fullmatch(actor) or "://" in actor or _SECRET_ASSIGNMENT_RE.search(actor):
        raise CephApprovalError(
            400,
            "actor_invalid",
            "X-Proxbox-Actor must be a safe opaque identity of at most 128 characters.",
        )
    return actor


def canonical_plan_digest(plan: PlanResponse) -> str:
    """Hash every persisted plan field except the digest itself."""

    payload = plan.model_dump(mode="json", exclude={"digest"})
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


def redact_secrets(value: Any) -> Any:
    """Return a JSON-safe copy with secret-bearing fields redacted."""
    if isinstance(value, BaseException):
        return f"{type(value).__name__}: [REDACTED]"
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_string = str(key)
            normalized = normalized_field_name(key_string)
            if normalized.endswith("credential_ref"):
                try:
                    redacted[key_string] = validate_credential_ref(item)
                except ValueError:
                    redacted[key_string] = "[INVALID_CREDENTIAL_REF]"
            elif is_secret_field_name(key_string):
                redacted[key_string] = "[REDACTED]"
            else:
                redacted[key_string] = redact_secrets(item)
        return redacted
    if isinstance(value, list | tuple | set):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _redact_secret_text(value)
    return _jsonable(value)


def _safe_text(value: object | None) -> str | None:
    if value is None:
        return None
    safe = redact_secrets(str(value))
    return str(safe)


def _run_lease_seconds() -> float:
    raw = os.getenv("PROXBOX_CEPH_RUN_LEASE_SECONDS", str(_DEFAULT_RUN_LEASE_SECONDS))
    try:
        parsed = float(raw)
    except ValueError:
        return float(_DEFAULT_RUN_LEASE_SECONDS)
    if not math.isfinite(parsed):
        return float(_DEFAULT_RUN_LEASE_SECONDS)
    return min(3600.0, max(1.0, parsed))


def _lease_expiry(status: str, now: float) -> float | None:
    return now + _run_lease_seconds() if status in {"running", "dispatching"} else None


def _lease_owner(status: str) -> str | None:
    """Issue an unexposed worker nonce only for nonterminal leased states."""

    return secrets.token_hex(32) if status in {"running", "dispatching"} else None


def _is_canonical_uuid(value: object | None) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


async def persist_plan(
    session: DatabaseSessionProtocol,
    plan: PlanResponse,
) -> PlanResponse:
    """Persist one canonical plan; apply routes never trust the process cache."""

    plan = PlanResponse.model_validate(redact_secrets(plan))
    requester = normalize_actor(plan.requester)
    plan.requester = requester
    if plan.provider == "proxmox" and not plan.endpoint_config_revision:
        raise CephPlanIntegrityError(
            "Proxmox plans require a stable server-keyed endpoint configuration revision."
        )
    expected_digest = canonical_plan_digest(plan)
    if plan.digest and plan.digest != expected_digest:
        raise CephPlanIntegrityError("Plan digest changed before persistence.")
    plan.digest = expected_digest
    record = CephPlanRecord(
        id=plan.id,
        provider=plan.provider,
        endpoint_id=plan.endpoint_id,
        endpoint_config_revision=plan.endpoint_config_revision,
        requester=requester,
        source_branch_schema_id=plan.source_branch_schema_id,
        digest=plan.digest,
        plan_payload=plan.model_dump(mode="json"),
        created_at=plan.created_at.timestamp(),
        expires_at=plan.expires_at.timestamp(),
    )
    session.add(record)
    await _maybe_await(session.commit())
    return plan


async def load_persisted_plan(
    session: DatabaseSessionProtocol,
    plan_id: str,
    *,
    require_current: bool = True,
) -> PlanResponse:
    """Load and cryptographically verify the durable plan snapshot."""

    record = await _maybe_await(session.get(CephPlanRecord, plan_id))
    if record is None:
        raise CephPlanNotFound(plan_id)
    try:
        plan = PlanResponse.model_validate(record.plan_payload)
    except ValidationError as exc:
        raise CephPlanIntegrityError("Persisted plan payload is invalid.") from exc
    expected = canonical_plan_digest(plan)
    if (
        plan.id != record.id
        or not plan.digest
        or plan.digest != record.digest
        or not secrets.compare_digest(plan.digest, expected)
        or plan.endpoint_id != record.endpoint_id
        or plan.endpoint_config_revision != record.endpoint_config_revision
        or plan.requester != record.requester
        or plan.provider != record.provider
        or plan.source_branch_schema_id != record.source_branch_schema_id
        or plan.created_at.timestamp() != record.created_at
        or plan.expires_at.timestamp() != record.expires_at
    ):
        raise CephPlanIntegrityError("Persisted plan digest or identity does not match.")
    if plan.provider == "proxmox" and not plan.endpoint_config_revision:
        raise CephPlanIntegrityError(
            "Persisted Proxmox plan has no endpoint configuration revision."
        )
    if require_current and record.expires_at <= time.time():
        raise CephPlanExpired(plan_id)
    return plan


def validation_results_for_request(request: PlanRequest) -> list[ValidationResult]:
    results: list[ValidationResult] = []
    for desired in request.desired_state.objects:
        target = desired.target_ref or desired.name
        if not target:
            results.append(
                ValidationResult(
                    severity="error",
                    code="target_ref_required",
                    message="Desired Ceph objects require target_ref, ref, name, or payload.name.",
                    target=desired.kind,
                )
            )
    for operation in request.operations:
        if not operation.target_ref:
            results.append(
                ValidationResult(
                    severity="error",
                    code="target_ref_required",
                    message="Provider operations require target_ref, ref, target, or name.",
                    target=operation.kind,
                )
            )
    return results


def metric_safety_validations(
    snapshot: CephMetricSnapshot | None,
    operations: list[ProviderOperation],
) -> list[ValidationResult]:
    """Warn when destructive ops are planned while the cluster is degraded.

    Consumes a Prometheus-derived metric snapshot (#94). When health is non-OK
    or recovery/backfill is in flight, each destructive operation gets a
    ``warning`` (surfaced, not a hard block — every mutation already requires
    independent approval) so operators can proceed deliberately.
    """

    if snapshot is None or not snapshot.is_degraded:
        return []
    reason = f"cluster health is {snapshot.cluster_health}"
    if snapshot.recovering_pgs or snapshot.degraded_pgs or snapshot.misplaced_pgs:
        reason += (
            f" (recovering={snapshot.recovering_pgs or 0}, "
            f"degraded={snapshot.degraded_pgs or 0}, "
            f"misplaced={snapshot.misplaced_pgs or 0})"
        )
    results: list[ValidationResult] = []
    for operation in operations:
        if operation.is_destructive:
            results.append(
                ValidationResult(
                    severity="warning",
                    code="cluster_degraded",
                    message=(
                        f"Destructive operation while {reason}; proceed only with "
                        "a fresh independent approval."
                    ),
                    target=operation.target_ref or operation.kind,
                )
            )
    return results


def validate_payload(payload: dict[str, Any]) -> ValidationResponse:
    try:
        if "kind" in payload and "desired_state" not in payload and "objects" not in payload:
            desired = DesiredObject.model_validate(payload)
            request = PlanRequest(desired_state=DesiredStateBundle(objects=[desired]))
        else:
            request = PlanRequest.model_validate(payload)
    except ValidationError as exc:
        response = ValidationResponse(
            valid=False,
            results=[
                ValidationResult(
                    severity="error",
                    code="schema_validation_failed",
                    message="Request schema validation failed.",
                    # Pydantic locations for ``extra_forbidden`` end with the
                    # attacker-controlled JSON key. Do not turn that key into
                    # reflected API metadata.
                    target=(
                        "request"
                        if error.get("type") == "extra_forbidden"
                        else ".".join(
                            str(part)
                            for part in error.get("loc", ())
                            if isinstance(part, int)
                            or (
                                isinstance(part, str)
                                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", part)
                            )
                        )
                        or None
                    ),
                )
                for error in exc.errors()
            ],
        )
        return ValidationResponse.model_validate(redact_secrets(response))
    results = validation_results_for_request(request)
    response = ValidationResponse(
        valid=not any(result.severity == "error" for result in results), results=results
    )
    return ValidationResponse.model_validate(redact_secrets(response))


def _stable_operation_id(operation: ProviderOperation) -> str:
    payload = operation.model_dump(
        mode="json",
        exclude_none=True,
        exclude={"id", "supported", "blocked_reason"},
    )
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return str(uuid5(NAMESPACE_URL, serialized))


def _normalize_operation(
    operation: ProviderOperation,
    capabilities: ProviderCapabilities,
) -> ProviderOperation:
    op = operation.model_copy(deep=True)
    op.provider = capabilities.provider
    op.kind = op.kind.strip().lower().replace("-", "_")
    op.action = op.action.strip().lower() or "ensure"
    op.before_summary = redact_secrets(op.before_summary)
    op.after_summary = redact_secrets(op.after_summary)
    op.metadata = sanitize_operation_value(op.metadata)
    op.is_destructive = op.is_destructive or _is_destructive(op)
    op.id = op.id or _stable_operation_id(op)

    if not op.target_ref:
        op.supported = False
        op.blocked_reason = "target_ref is required for Ceph provider operations."
    elif not capabilities.supported:
        op.supported = False
        op.blocked_reason = f"provider {capabilities.provider!r} is not implemented."
    elif op.supported and not _provider_supports_operation(capabilities, op):
        op.supported = False
        op.blocked_reason = (
            f"provider {capabilities.provider!r} does not support {op.action} for {op.kind}."
        )
    elif not op.supported and not op.blocked_reason:
        op.blocked_reason = "provider operation is marked unsupported."
    return ProviderOperation.model_validate(redact_secrets(op))


def _provider_supports_operation(
    capabilities: ProviderCapabilities,
    operation: ProviderOperation,
) -> bool:
    key = f"{operation.kind}:{operation.action}"
    return capabilities.operation_kinds.get(key) is True


def _is_destructive(operation: ProviderOperation) -> bool:
    action = operation.action.strip().lower()
    kind = operation.kind.strip().lower().replace("-", "_")
    if action in _DESTRUCTIVE_ACTIONS:
        return True
    return action in _DESTRUCTIVE_KIND_ACTIONS.get(kind, set())


def _operation_priority(operation: ProviderOperation) -> tuple[int, str, str, str]:
    action = operation.action
    priority = {
        "noop": 0,
        "create": 10,
        "ensure": 20,
        "update": 30,
        "set": 30,
        "delete": 90,
        "destroy": 90,
        "purge": 90,
        "remove": 90,
        "zap": 90,
    }.get(action, 50)
    return (priority, operation.kind, operation.target_ref, operation.id or "")


def _generic_operations_from_desired(
    request: PlanRequest,
    provider: str,
) -> list[ProviderOperation]:
    operations: list[ProviderOperation] = []
    for desired in request.desired_state.objects:
        target_ref = desired.target_ref or desired.name or ""
        operations.append(
            ProviderOperation(
                provider=provider,
                kind=desired.kind,
                target_ref=str(target_ref),
                action=desired.action,
                after_summary=desired.payload,
            )
        )
    return operations


async def _raw_operations(
    request: PlanRequest,
    adapter: CephProviderAdapter,
    capabilities: ProviderCapabilities,
) -> tuple[list[ProviderOperation], dict[str, Any]]:
    if request.operations:
        return request.operations, {}

    if not capabilities.supported:
        return _generic_operations_from_desired(request, capabilities.provider), {}

    live = await adapter.read_state(request.scope or request.desired_state.scope)
    operations = await adapter.diff(request.desired_state, live)
    return operations, live


def _snapshot_from_request(
    request: PlanRequest, metric_snapshot: CephMetricSnapshot | None
) -> CephMetricSnapshot | None:
    if metric_snapshot is not None:
        return metric_snapshot
    raw = request.scope.get("metric_snapshot")
    if isinstance(raw, CephMetricSnapshot):
        return raw
    if isinstance(raw, dict):
        try:
            return CephMetricSnapshot.model_validate(raw)
        except ValidationError:
            return None
    return None


async def build_plan(
    request: PlanRequest,
    adapter: CephProviderAdapter,
    *,
    metric_snapshot: CephMetricSnapshot | None = None,
    endpoint_config_revision: str | None = None,
) -> PlanResponse:
    try:
        capabilities = await adapter.capabilities()
    except CephProviderBoundaryError:
        raise
    except Exception:  # noqa: BLE001 - provider details are never surfaced
        raise CephProviderBoundaryError(
            "provider_capabilities_unavailable",
            "Ceph provider capabilities could not be read safely.",
        ) from None
    validations = validation_results_for_request(request)
    warnings = redact_secrets(list(capabilities.notes))

    if any(result.severity == "error" for result in validations):
        operations: list[ProviderOperation] = []
        live: dict[str, Any] = {}
    else:
        try:
            operations, live = await _raw_operations(request, adapter, capabilities)
            if capabilities.supported and capabilities.plan:
                operations = await adapter.plan(operations)
        except CephCapabilityUnsupported:
            warnings.append("provider_capability_unsupported")
            operations = _generic_operations_from_desired(request, capabilities.provider)
            live = {}
        except CephProviderBoundaryError:
            raise
        except Exception:  # noqa: BLE001 - provider details are never surfaced
            raise CephProviderBoundaryError(
                "provider_plan_unavailable",
                "Ceph provider state or plan generation could not be completed safely.",
            ) from None

    normalized = sorted(
        (_normalize_operation(operation, capabilities) for operation in operations),
        key=_operation_priority,
    )
    snapshot = _snapshot_from_request(request, metric_snapshot)
    validations = validations + metric_safety_validations(snapshot, normalized)
    blocked_actions = [operation for operation in normalized if not operation.supported]
    branch_id = request.branch_schema_id
    created_at = utcnow()
    plan = PlanResponse(
        id=str(uuid4()),
        provider=capabilities.provider,
        endpoint_id=request.endpoint_id,
        endpoint_config_revision=endpoint_config_revision,
        requester=request.actor.strip() if request.actor else None,
        netbox_branch_schema_id=branch_id,
        source_branch_schema_id=branch_id,
        operations=normalized,
        validations=validations,
        warnings=warnings,
        blocked_actions=blocked_actions,
        created_at=created_at,
        expires_at=created_at + timedelta(seconds=PLAN_TTL_SECONDS),
        live_state_summary=(
            redact_secrets(live.get("summary", {})) if isinstance(live, dict) else {}
        ),
        request_summary=redact_secrets(request),
    )
    plan.digest = canonical_plan_digest(plan)
    return plan


async def _commit_and_refresh(
    session: DatabaseSessionProtocol,
    record: CephOperationRunRecord,
) -> None:
    await _maybe_await(session.commit())
    await _maybe_await(session.refresh(record))


async def _create_run(
    session: DatabaseSessionProtocol,
    *,
    plan: PlanResponse | None,
    provider: str,
    actor: str | None,
    branch_schema_id: str | None,
    request_summary: dict[str, Any],
    endpoint_id: int | None = None,
    endpoint_config_revision: str | None = None,
    plan_digest: str | None = None,
    requester: str | None = None,
    approver: str | None = None,
    approval_id: str | None = None,
    status: str = "running",
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
    result_summary: dict[str, Any] | None = None,
) -> CephOperationRunRecord:
    now = time.time()
    record = CephOperationRunRecord(
        id=str(uuid4()),
        plan_id=plan.id if plan is not None else None,
        endpoint_id=endpoint_id
        if endpoint_id is not None
        else (plan.endpoint_id if plan else None),
        endpoint_config_revision=(
            endpoint_config_revision
            if endpoint_config_revision is not None
            else (plan.endpoint_config_revision if plan else None)
        ),
        plan_digest=plan_digest if plan_digest is not None else (plan.digest if plan else None),
        requester=requester if requester is not None else (plan.requester if plan else None),
        approver=approver,
        approval_id=approval_id,
        status=status,
        actor=actor,
        source_branch_schema_id=branch_schema_id,
        provider=provider,
        request_summary=redact_secrets(request_summary),
        provider_task_refs=[],
        created_at=now,
        updated_at=now,
        lease_expires_at=_lease_expiry(status, now),
        lease_owner=_lease_owner(status),
        warnings=redact_secrets(warnings or []),
        errors=redact_secrets(errors or []),
        result_summary=redact_secrets(result_summary or {}),
    )
    session.add(record)
    await _commit_and_refresh(session, record)
    return record


async def _next_event_sequence(session: DatabaseSessionProtocol, run_id: str) -> int:
    result = await _maybe_await(
        session.exec(
            select(CephOperationEventRecord)
            .where(CephOperationEventRecord.run_id == run_id)
            .order_by(col(CephOperationEventRecord.sequence).desc())
        )
    )
    latest = result.first()
    return (latest.sequence + 1) if latest is not None else 0


async def _append_event_checkpoint(
    session: DatabaseSessionProtocol,
    run_record: CephOperationRunRecord,
    *,
    event: str,
    status: str,
    code: str,
    message: str,
    operation_index: int | None = None,
    operation: ProviderOperation | None = None,
    provider_task_ref: str | None = None,
    payload: dict[str, Any] | None = None,
    provider_task_refs: list[str] | None = None,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
    result_summary: dict[str, Any] | None = None,
    transaction_records: list[Any] | None = None,
) -> CephOperationRunRecord:
    """Append one event and checkpoint only for the current lease owner.

    Every transition out of ``running``/``dispatching`` is a compare-and-set on
    the unexposed owner nonce and a still-live expiry. A late worker therefore
    cannot overwrite stale-run recovery or another worker's checkpoint.
    """

    now = time.time()
    run_id = run_record.id
    previous_status = run_record.status
    expected_owner = run_record.lease_owner
    next_task_refs = (
        list(run_record.provider_task_refs or [])
        if provider_task_refs is None
        else [safe for item in provider_task_refs if (safe := _safe_text(item)) is not None]
    )
    next_warnings = (
        list(run_record.warnings or []) if warnings is None else redact_secrets(list(warnings))
    )
    next_errors = list(run_record.errors or []) if errors is None else redact_secrets(list(errors))
    next_result = (
        dict(run_record.result_summary or {})
        if result_summary is None
        else redact_secrets(result_summary)
    )
    next_expiry = _lease_expiry(status, now)
    next_owner = expected_owner if status in {"running", "dispatching"} else None

    if previous_status in {"running", "dispatching"}:
        if not expected_owner:
            raise _CephRunLeaseLost(run_id)
        await _maybe_await(session.rollback())
        statement = (
            sa_update(CephOperationRunRecord)
            .where(col(CephOperationRunRecord.id) == run_id)
            .where(col(CephOperationRunRecord.status).in_(("running", "dispatching")))
            .where(col(CephOperationRunRecord.lease_owner) == expected_owner)
            .where(col(CephOperationRunRecord.lease_expires_at) > now)
            .values(
                status=status,
                updated_at=now,
                lease_expires_at=next_expiry,
                lease_owner=next_owner,
                provider_task_refs=next_task_refs,
                warnings=next_warnings,
                errors=next_errors,
                result_summary=next_result,
            )
            .execution_options(synchronize_session=False)
        )
        result = await _maybe_await(session.exec(statement))
        if result.rowcount != 1:
            await _maybe_await(session.rollback())
            raise _CephRunLeaseLost(run_id)
    elif expected_owner is not None:
        # Terminal rows must never retain reusable execution authority.
        raise _CephRunLeaseLost(run_id)

    for transaction_record in transaction_records or []:
        session.add(transaction_record)
    sequence = await _next_event_sequence(session, run_id)
    event_record = CephOperationEventRecord(
        run_id=run_id,
        sequence=sequence,
        operation_index=operation_index,
        operation_id=_safe_text(operation.id) if operation is not None else None,
        event=_safe_text(event) or "event_redacted",
        status=status,
        code=_safe_text(code) or "diagnostic_redacted",
        message=_safe_text(message) or "Diagnostic redacted.",
        kind=_safe_text(operation.kind) if operation is not None else None,
        action=_safe_text(operation.action) if operation is not None else None,
        target_ref=_safe_text(operation.target_ref) if operation is not None else None,
        provider_task_ref=_safe_text(provider_task_ref),
        payload=redact_secrets(payload or {}),
        created_at=now,
    )
    session.add(event_record)
    if previous_status not in {"running", "dispatching"}:
        run_record.status = status
        run_record.updated_at = now
        run_record.lease_expires_at = next_expiry
        run_record.lease_owner = next_owner
        run_record.provider_task_refs = next_task_refs
        run_record.warnings = next_warnings
        run_record.errors = next_errors
        run_record.result_summary = next_result
        session.add(run_record)
    await _maybe_await(session.commit())
    await _maybe_await(session.refresh(run_record))
    return run_record


def record_to_operation_event(record: CephOperationEventRecord) -> OperationEvent:
    return OperationEvent(
        sequence=record.sequence,
        operation_index=record.operation_index,
        operation_id=_safe_text(record.operation_id),
        event=_safe_text(record.event) or "event_redacted",
        status=cast("OperationStatus", record.status),
        code=_safe_text(record.code) or "diagnostic_redacted",
        message=_safe_text(record.message) or "Diagnostic redacted.",
        kind=_safe_text(record.kind),
        action=_safe_text(record.action),
        target_ref=_safe_text(record.target_ref),
        provider_task_ref=_safe_text(record.provider_task_ref),
        payload=redact_secrets(record.payload or {}),
        created_at=_dt_from_ts(record.created_at),
    )


async def operation_run_with_events(
    session: DatabaseSessionProtocol,
    record: CephOperationRunRecord,
) -> OperationRun:
    result = await _maybe_await(
        session.exec(
            select(CephOperationEventRecord)
            .where(CephOperationEventRecord.run_id == record.id)
            .order_by(col(CephOperationEventRecord.sequence))
        )
    )
    return record_to_operation_run(
        record,
        events=[record_to_operation_event(item) for item in result.all()],
    )


async def _renew_run_lease(
    session: DatabaseSessionProtocol,
    record: CephOperationRunRecord,
) -> bool:
    """Renew an in-flight run only while its existing lease is still live.

    A worker may never reclaim an expired lease. Status/SSE recovery is allowed
    to turn that run into ``outcome_unknown`` exactly once, and a late provider
    response must not resurrect it as completed.
    """

    # Drop the stale read snapshot first, then reload through the async-safe
    # refresh before reading any attribute. ``rollback()`` expires every
    # instance in the session, and a plain attribute read on an expired
    # instance lazy-loads through the sync facade — which raises
    # ``MissingGreenlet`` on an ``AsyncSession`` (a concurrent commit on the
    # shared session triggers the same expiry, so entry-time reads are just as
    # unsafe as post-rollback ones).
    await _maybe_await(session.rollback())
    try:
        await _maybe_await(session.refresh(record))
    except Exception:  # noqa: BLE001 - a vanished run row means the lease is gone
        await _maybe_await(session.rollback())
        return False
    record_id = record.id
    expected_owner = record.lease_owner
    if record.status not in {"running", "dispatching"} or not expected_owner:
        return False
    now = time.time()
    lease_seconds = _run_lease_seconds()
    statement = (
        sa_update(CephOperationRunRecord)
        .where(col(CephOperationRunRecord.id) == record_id)
        .where(col(CephOperationRunRecord.status).in_(("running", "dispatching")))
        .where(col(CephOperationRunRecord.lease_owner) == expected_owner)
        .where(col(CephOperationRunRecord.lease_expires_at) > now)
        .values(
            updated_at=now,
            lease_expires_at=now + lease_seconds,
        )
        .execution_options(synchronize_session=False)
    )
    result = await _maybe_await(session.exec(statement))
    if result.rowcount != 1:
        await _maybe_await(session.rollback())
        return False
    await _maybe_await(session.commit())
    await _maybe_await(session.refresh(record))
    return True


async def recover_stale_operation_run(
    session: DatabaseSessionProtocol,
    record: CephOperationRunRecord,
) -> CephOperationRunRecord:
    """Conservatively transition an expired in-flight lease to outcome_unknown.

    The compare-and-set makes concurrent status readers harmless. The recovery
    event retains provider task references and tells operators to resolve those
    task outcomes before creating any fresh plan; it never retries a mutation.
    """

    if record.status not in {"running", "dispatching"}:
        return record
    now = time.time()
    legacy_cutoff = now - _run_lease_seconds()
    if record.lease_expires_at is not None and record.lease_expires_at > now:
        return record
    if record.lease_expires_at is None and record.updated_at > legacy_cutoff:
        return record

    safe_refs = [
        safe for item in (record.provider_task_refs or []) if (safe := _safe_text(item)) is not None
    ]
    recovery = {
        **redact_secrets(record.result_summary or {}),
        "reason": "run_lease_expired",
        "recovery": {
            "provider_task_refs": safe_refs,
            "action": (
                "Inspect every provider task reference and current Ceph state before "
                "creating a fresh plan. Never replay this consumed approval."
            ),
        },
    }
    errors = [*redact_secrets(record.errors or []), "In-flight run lease expired."]
    # Capture the primary key before the rollback: rollback expires every
    # instance in the session, and a post-rollback ``record.id`` read would
    # lazy-load through the sync facade and raise ``MissingGreenlet`` on an
    # ``AsyncSession`` — the same mechanism fixed in ``_renew_run_lease``.
    record_id = record.id
    await _maybe_await(session.rollback())
    statement = (
        sa_update(CephOperationRunRecord)
        .where(col(CephOperationRunRecord.id) == record_id)
        .where(col(CephOperationRunRecord.status).in_(("running", "dispatching")))
        .where(
            or_(
                col(CephOperationRunRecord.lease_expires_at) <= now,
                and_(
                    col(CephOperationRunRecord.lease_expires_at).is_(None),
                    col(CephOperationRunRecord.updated_at) <= legacy_cutoff,
                ),
            )
        )
        .values(
            status="outcome_unknown",
            updated_at=now,
            lease_expires_at=None,
            lease_owner=None,
            errors=errors,
            result_summary=recovery,
        )
        .execution_options(synchronize_session=False)
    )
    result = await _maybe_await(session.exec(statement))
    if result.rowcount == 1:
        sequence = await _next_event_sequence(session, record_id)
        session.add(
            CephOperationEventRecord(
                run_id=record_id,
                sequence=sequence,
                event="run_lease_expired",
                status="outcome_unknown",
                code="run_lease_expired",
                message=(
                    "The durable execution lease expired before a terminal run "
                    "checkpoint; provider outcome requires operator recovery."
                ),
                payload=redact_secrets(recovery["recovery"]),
                created_at=now,
            )
        )
        await _maybe_await(session.commit())
    else:
        await _maybe_await(session.rollback())

    refreshed = await _maybe_await(
        session.exec(
            select(CephOperationRunRecord)
            .where(CephOperationRunRecord.id == record_id)
            .execution_options(populate_existing=True)
        )
    )
    return refreshed.one()


async def _operation_after_lease_loss(
    session: DatabaseSessionProtocol,
    run_id: object,
) -> OperationRun:
    """Return a conservative terminal view after a worker loses its lease.

    Takes the scalar run id — never the ORM instance. The instance a losing
    worker holds has typically just been expired by a renewal rollback, and
    reading ``.id`` off it would lazy-load through the sync facade and raise
    ``MissingGreenlet`` on an ``AsyncSession``.
    """

    await _maybe_await(session.rollback())
    result = await _maybe_await(
        session.exec(
            select(CephOperationRunRecord)
            .where(CephOperationRunRecord.id == run_id)
            .execution_options(populate_existing=True)
        )
    )
    current = result.one()
    current = await recover_stale_operation_run(session, current)
    # A live lease belongs to another/current worker and is never terminalized
    # merely because this stale object lost ownership. Only the expired-lease
    # compare-and-set above may produce ``outcome_unknown`` recovery state.
    return await operation_run_with_events(session, current)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def approval_recovery_metadata(
    record: CephApprovalRecord,
    *,
    plan: PlanResponse | None = None,
) -> dict[str, Any]:
    """Return validated, secret-free metadata for approval POST recovery."""

    revision = (record.endpoint_config_revision or "").strip()
    valid_revision = bool(re.fullmatch(r"[0-9a-f]{64}", revision))
    try:
        requester = normalize_actor(record.requester)
        approver = normalize_actor(record.approver)
    except CephApprovalError:
        requester = ""
        approver = ""
    run_binding_valid = (
        record.operation_run_id is None
        and record.consumed_at is None
        and record.consumed_by is None
    ) or (
        _is_canonical_uuid(record.operation_run_id)
        and record.consumed_at is not None
        and record.consumed_by is not None
        and record.consumed_by.casefold() == requester.casefold()
    )
    valid_identity = bool(
        _is_canonical_uuid(record.id)
        and _is_canonical_uuid(record.plan_id)
        and _SHA256_HEX_RE.fullmatch(record.plan_digest or "")
        and _SHA256_HEX_RE.fullmatch(record.token_hash or "")
        and record.endpoint_id is not None
        and record.endpoint_id > 0
        and requester == record.requester
        and approver == record.approver
        and valid_revision
        and record.created_at < record.expires_at
        and run_binding_valid
    )
    if plan is not None:
        valid_identity = valid_identity and all(
            (
                record.plan_id == plan.id,
                secrets.compare_digest(record.plan_digest, plan.digest),
                record.endpoint_id == plan.endpoint_id,
                record.requester == plan.requester,
                secrets.compare_digest(revision, plan.endpoint_config_revision or ""),
            )
        )
    if not valid_identity:
        raise CephApprovalError(
            409,
            "approval_recovery_integrity_failed",
            "The existing approval metadata failed durable binding validation.",
        )
    return redact_secrets(
        {
            "approval_id": record.id,
            "plan_id": record.plan_id,
            "plan_digest": record.plan_digest,
            "endpoint_id": record.endpoint_id,
            "endpoint_config_revision": revision,
            "requester": record.requester,
            "approver": record.approver,
            "operation_run_id": record.operation_run_id,
        }
    )


async def validated_approval_recovery_metadata(
    session: DatabaseSessionProtocol,
    record: CephApprovalRecord,
    *,
    plan: PlanResponse | None = None,
) -> dict[str, Any]:
    """Validate the linked run before returning crash-recovery authority."""

    metadata = approval_recovery_metadata(record, plan=plan)
    if record.operation_run_id is None:
        return metadata
    run = await _maybe_await(session.get(CephOperationRunRecord, record.operation_run_id))
    valid_run = (
        run is not None
        and run.actor is not None
        and all(
            (
                run.id == record.operation_run_id,
                run.plan_id == record.plan_id,
                secrets.compare_digest(run.plan_digest or "", record.plan_digest),
                run.endpoint_id == record.endpoint_id,
                secrets.compare_digest(
                    run.endpoint_config_revision or "",
                    record.endpoint_config_revision or "",
                ),
                run.requester == record.requester,
                run.approver == record.approver,
                run.approval_id == record.id,
                run.actor.casefold() == record.requester.casefold(),
                run.provider == "proxmox",
            )
        )
    )
    if not valid_run:
        raise CephApprovalError(
            409,
            "approval_recovery_integrity_failed",
            "The linked operation run failed durable approval binding validation.",
        )
    return metadata


async def issue_plan_approval(
    session: DatabaseSessionProtocol,
    *,
    plan: PlanResponse,
    endpoint_id: int | None,
    approver: str,
) -> ApprovalResponse:
    """Issue an opaque approval token and persist only its SHA-256 hash."""

    if not ceph_write_execution_enabled():
        raise CephApprovalError(
            503,
            "ceph_write_execution_disabled",
            "Ceph approval requires explicit operator and trusted actor gateway gates.",
        )
    requester = normalize_actor(plan.requester)
    normalized_approver = normalize_actor(approver)
    if requester.casefold() == normalized_approver.casefold():
        raise CephApprovalError(
            409,
            "two_person_approval_required",
            "The plan requester and approver must be different actors.",
        )
    if endpoint_id != plan.endpoint_id:
        raise CephApprovalError(
            409,
            "approval_endpoint_mismatch",
            "The approval endpoint does not match the persisted plan endpoint.",
        )
    if not plan.endpoint_config_revision:
        raise CephApprovalError(
            409,
            "plan_endpoint_revision_missing",
            "The canonical plan has no endpoint configuration revision.",
        )
    if plan.expires_at <= utcnow():
        raise CephApprovalError(410, "plan_expired", "The persisted plan has expired.")
    if not secrets.compare_digest(plan.digest, canonical_plan_digest(plan)):
        raise CephApprovalError(
            409,
            "plan_integrity_failed",
            "The persisted plan no longer matches its canonical digest.",
        )

    existing_result = await _maybe_await(
        session.exec(select(CephApprovalRecord).where(CephApprovalRecord.plan_id == plan.id))
    )
    existing = existing_result.first()
    if existing is not None:
        raise CephApprovalError(
            409,
            "approval_already_issued",
            "This canonical plan already has an approval authority; recover its status by id.",
            recovery=await validated_approval_recovery_metadata(
                session,
                existing,
                plan=plan,
            ),
        )

    token = secrets.token_urlsafe(48)
    created_at = utcnow()
    expires_at = min(
        plan.expires_at,
        created_at + timedelta(seconds=APPROVAL_TTL_SECONDS),
    )
    record = CephApprovalRecord(
        id=str(uuid4()),
        plan_id=plan.id,
        plan_digest=plan.digest,
        endpoint_id=plan.endpoint_id,
        endpoint_config_revision=plan.endpoint_config_revision,
        requester=requester,
        approver=normalized_approver,
        token_hash=_token_hash(token),
        created_at=created_at.timestamp(),
        expires_at=expires_at.timestamp(),
    )
    session.add(record)
    try:
        await _maybe_await(session.commit())
    except IntegrityError as exc:
        await _maybe_await(session.rollback())
        raced_result = await _maybe_await(
            session.exec(select(CephApprovalRecord).where(CephApprovalRecord.plan_id == plan.id))
        )
        raced = raced_result.first()
        if raced is None:
            raise CephApprovalError(
                503,
                "approval_recovery_unavailable",
                "Approval issuance raced, but durable recovery metadata is not yet available.",
            ) from exc
        recovery = await validated_approval_recovery_metadata(
            session,
            raced,
            plan=plan,
        )
        raise CephApprovalError(
            409,
            "approval_already_issued",
            "This canonical plan already has an approval authority; recover its status by id.",
            recovery=recovery,
        ) from exc
    return ApprovalResponse(
        id=record.id,
        plan_id=record.plan_id,
        plan_digest=record.plan_digest,
        endpoint_id=record.endpoint_id,
        endpoint_config_revision=record.endpoint_config_revision,
        requester=record.requester,
        approver=record.approver,
        token=token,
        expires_at=expires_at,
    )


async def _approval_error_for_token(
    session: DatabaseSessionProtocol,
    *,
    token_hash: str,
    plan: PlanResponse,
    actor: str,
) -> CephApprovalError:
    result = await _maybe_await(
        session.exec(
            select(CephApprovalRecord)
            .where(CephApprovalRecord.token_hash == token_hash)
            .execution_options(populate_existing=True)
        )
    )
    record = result.first()
    error = _approval_record_error(record, plan=plan, actor=actor)
    if error is not None and error.reason == "approval_replayed" and record is not None:
        error.recovery = await validated_approval_recovery_metadata(
            session,
            record,
            plan=plan,
        )
    return error or CephApprovalError(
        409,
        "approval_invalid",
        "The approval token cannot be consumed.",
    )


def _approval_record_error(
    record: CephApprovalRecord | None,
    *,
    plan: PlanResponse,
    actor: str,
) -> CephApprovalError | None:
    if record is None:
        return CephApprovalError(409, "approval_invalid", "The approval token is invalid.")
    if record.plan_id != plan.id or record.plan_digest != plan.digest:
        return CephApprovalError(
            409,
            "approval_plan_mismatch",
            "The approval token is not bound to this canonical plan.",
        )
    if record.endpoint_id != plan.endpoint_id:
        return CephApprovalError(
            409,
            "approval_endpoint_mismatch",
            "The approval token is not bound to this endpoint.",
        )
    if not record.endpoint_config_revision or not plan.endpoint_config_revision:
        return CephApprovalError(
            409,
            "approval_endpoint_revision_missing",
            "The approval or plan has no endpoint configuration revision.",
        )
    if not secrets.compare_digest(
        record.endpoint_config_revision,
        plan.endpoint_config_revision,
    ):
        return CephApprovalError(
            409,
            "approval_endpoint_revision_mismatch",
            "The approval token is not bound to this endpoint configuration revision.",
        )
    if record.requester.casefold() != actor.casefold():
        return CephApprovalError(
            403,
            "approval_requester_mismatch",
            "Only the actor who requested the plan may consume its approval.",
        )
    if record.consumed_at is not None:
        return CephApprovalError(
            409,
            "approval_replayed",
            "The approval token has already been consumed; inspect the original operation run.",
        )
    if record.expires_at <= time.time():
        return CephApprovalError(410, "approval_expired", "The approval token has expired.")
    return None


async def prevalidate_plan_approval(
    session: DatabaseSessionProtocol,
    *,
    plan: PlanResponse,
    request: ApplyRequest,
) -> None:
    """Reject invalid/replayed authority before opening a provider session.

    This read is not execution authority: the later conditional update remains
    the single atomic consume gate, so a race between prevalidation and consume
    still has exactly one winner.
    """

    actor = normalize_actor(request.actor)
    token = (request.approval_token or "").strip()
    if not token:
        raise CephApprovalError(
            409,
            "approval_required",
            "An opaque approval_token issued by a different actor is required.",
        )
    if plan.requester is None or actor.casefold() != plan.requester.casefold():
        raise CephApprovalError(
            403,
            "approval_requester_mismatch",
            "The applying actor must match the persisted plan requester.",
        )
    if request.endpoint_id != plan.endpoint_id:
        raise CephApprovalError(
            409,
            "apply_endpoint_mismatch",
            "The apply endpoint does not match the persisted plan endpoint.",
        )
    result = await _maybe_await(
        session.exec(
            select(CephApprovalRecord)
            .where(CephApprovalRecord.token_hash == _token_hash(token))
            .execution_options(populate_existing=True)
        )
    )
    record = result.first()
    error = _approval_record_error(record, plan=plan, actor=actor)
    if error is not None and error.reason == "approval_replayed" and record is not None:
        error.recovery = await validated_approval_recovery_metadata(
            session,
            record,
            plan=plan,
        )
    if error is not None:
        raise error


async def _consume_approval_and_create_run(
    session: DatabaseSessionProtocol,
    *,
    plan: PlanResponse,
    request: ApplyRequest,
) -> CephOperationRunRecord:
    """Atomically consume one bound approval and create its immutable audit run."""

    actor = normalize_actor(request.actor)
    token = (request.approval_token or "").strip()
    if not token:
        raise CephApprovalError(
            409,
            "approval_required",
            "An opaque approval_token issued by a different actor is required.",
        )
    if plan.requester is None or actor.casefold() != plan.requester.casefold():
        raise CephApprovalError(
            403,
            "approval_requester_mismatch",
            "The applying actor must match the persisted plan requester.",
        )
    if request.endpoint_id != plan.endpoint_id:
        raise CephApprovalError(
            409,
            "apply_endpoint_mismatch",
            "The apply endpoint does not match the persisted plan endpoint.",
        )

    hashed = _token_hash(token)
    now = time.time()
    run_id = str(uuid4())

    # Plan and endpoint reads may have opened a SQLite read transaction. End it
    # before the compare-and-set so two callers never need to upgrade the same
    # stale WAL snapshot to a writer. The approval update and audit-run insert
    # remain one transaction and therefore commit or roll back together.
    await _maybe_await(session.rollback())
    statement = (
        sa_update(CephApprovalRecord)
        .where(col(CephApprovalRecord.token_hash) == hashed)
        .where(col(CephApprovalRecord.plan_id) == plan.id)
        .where(col(CephApprovalRecord.plan_digest) == plan.digest)
        .where(col(CephApprovalRecord.endpoint_id) == plan.endpoint_id)
        .where(col(CephApprovalRecord.endpoint_config_revision) == plan.endpoint_config_revision)
        .where(col(CephApprovalRecord.requester) == plan.requester)
        .where(col(CephApprovalRecord.consumed_at).is_(None))
        .where(col(CephApprovalRecord.expires_at) > now)
        .values(consumed_at=now, consumed_by=actor, operation_run_id=run_id)
        .execution_options(synchronize_session=False)
    )
    result = await _maybe_await(session.exec(statement))
    if result.rowcount != 1:
        await _maybe_await(session.rollback())
        raise await _approval_error_for_token(
            session,
            token_hash=hashed,
            plan=plan,
            actor=actor,
        )

    lookup = await _maybe_await(
        session.exec(select(CephApprovalRecord).where(CephApprovalRecord.token_hash == hashed))
    )
    approval = lookup.first()
    if (
        approval is None
        or not approval.approver.strip()
        or approval.approver.casefold() == actor.casefold()
    ):
        await _maybe_await(session.rollback())
        raise CephApprovalError(
            409,
            "two_person_approval_required",
            "The persisted approval must identify a different, non-empty approver.",
        )

    run_record = CephOperationRunRecord(
        id=run_id,
        plan_id=plan.id,
        endpoint_id=plan.endpoint_id,
        endpoint_config_revision=plan.endpoint_config_revision,
        plan_digest=plan.digest,
        requester=plan.requester,
        approver=approval.approver,
        approval_id=approval.id,
        status="running",
        actor=actor,
        source_branch_schema_id=request.branch_schema_id or plan.source_branch_schema_id,
        provider=plan.provider,
        request_summary=redact_secrets(
            {
                "plan_id": plan.id,
                "endpoint_id": plan.endpoint_id,
                "operations": [operation.model_dump(mode="json") for operation in plan.operations],
            }
        ),
        provider_task_refs=[],
        created_at=now,
        updated_at=now,
        lease_expires_at=_lease_expiry("running", now),
        lease_owner=_lease_owner("running"),
        warnings=plan.warnings,
        errors=[],
        result_summary={},
    )
    session.add(run_record)
    session.add(
        CephOperationEventRecord(
            run_id=run_id,
            sequence=0,
            event="approval_consumed",
            status="running",
            code="approval_consumed",
            message="Approval consumed; execution accepted.",
            payload={"approval_id": approval.id, "plan_id": plan.id},
            created_at=now,
        )
    )
    await _commit_and_refresh(session, run_record)
    return run_record


def _task_ref_candidates(result: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    value = result.get("provider_task_ref")
    if value:
        refs.append(str(value))
    values = result.get("provider_task_refs")
    if isinstance(values, list):
        refs.extend(str(item) for item in values if item)
    upid = result.get("upid")
    if upid:
        refs.append(str(upid))
    safe_refs = [safe for item in refs if (safe := _safe_text(item)) is not None]
    return list(dict.fromkeys(safe_refs))


def _task_refs_from_result(result: dict[str, Any], *, provider: str) -> list[str]:
    safe_refs = _task_ref_candidates(result)
    filtered = (
        [ref for ref in safe_refs if _PROXMOX_UPID_RE.fullmatch(ref)]
        if provider == "proxmox"
        else safe_refs
    )
    return filtered


def _proxmox_upid_node(upid: str) -> str | None:
    match = _PROXMOX_UPID_RE.fullmatch(upid)
    return match.group("node") if match is not None else None


async def _raise_with_run(
    session: DatabaseSessionProtocol,
    *,
    plan: PlanResponse,
    request: ApplyRequest,
    status_code: int,
    status: str,
    reason: str,
    errors: list[str],
) -> None:
    run_record = await _create_run(
        session,
        plan=plan,
        provider=plan.provider,
        actor=request.actor,
        branch_schema_id=request.branch_schema_id or plan.source_branch_schema_id,
        request_summary={
            "plan_id": plan.id,
            "reason": reason,
            "operations": [operation.model_dump(mode="json") for operation in plan.operations],
        },
        status=status,
        warnings=plan.warnings,
        errors=errors,
        result_summary={"reason": reason, "applied": 0, "total": len(plan.operations)},
    )
    run_record = await _append_event_checkpoint(
        session,
        run_record,
        event=reason,
        status=status,
        code=reason,
        message="The plan was rejected before provider dispatch.",
        warnings=plan.warnings,
        errors=errors,
        result_summary={"reason": reason, "applied": 0, "total": len(plan.operations)},
    )
    run = await operation_run_with_events(session, run_record)
    raise CephApplyError(
        status_code,
        {
            "reason": reason,
            "detail": errors[0] if errors else reason,
            "operation_run_id": run.id,
            "plan_id": plan.id,
        },
        run,
    )


@dataclass
class _ApplyProgress:
    task_refs: list[str] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    completed_count: int = 0

    def summary(self, total: int, **extra: Any) -> dict[str, Any]:
        return {
            "applied": self.completed_count,
            "total": total,
            "results": self.results,
            **extra,
        }


async def _persist_cancelled_checkpoint(
    session: DatabaseSessionProtocol,
    run_record: CephOperationRunRecord,
    *,
    event: str,
    code: str,
    message: str,
    operation_index: int,
    operation: ProviderOperation,
    plan: PlanResponse,
    progress: _ApplyProgress,
    error: str,
    provider_task_ref: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    checkpoint = asyncio.create_task(
        _append_event_checkpoint(
            session,
            run_record,
            event=event,
            status="outcome_unknown",
            code=code,
            message=message,
            operation_index=operation_index,
            operation=operation,
            provider_task_ref=provider_task_ref,
            payload=payload,
            provider_task_refs=progress.task_refs,
            warnings=plan.warnings,
            errors=[error],
            result_summary=progress.summary(
                len(plan.operations),
                outcome_unknown_operation_id=operation.id,
            ),
        )
    )
    try:
        await _await_task_through_repeated_cancellation(checkpoint)
    except asyncio.CancelledError:
        # The helper only propagates caller cancellation after the checkpoint
        # task has reached a terminal state.  This function is already running
        # while preserving an earlier cancellation, so the later cancellation
        # is intentionally coalesced into that original signal.
        pass
    except Exception:  # noqa: BLE001 - preserve the original cancellation
        pass


async def _await_task_through_repeated_cancellation(
    task: asyncio.Task[_TaskResultT],
) -> _TaskResultT:
    """Finish ``task`` despite repeated caller cancellation, then re-raise.

    ``asyncio.shield`` protects an inner task from one cancellation but does not
    make the outer await cancellation-resistant: another ``Task.cancel()`` can
    interrupt the next await.  Keep re-entering the shield until the inner task
    is done, remember whether the caller was cancelled at least once, and only
    then propagate ``CancelledError``.  ``task.result()`` is deliberately read
    without another suspension point so no cancellation window remains between
    durable completion and result observation.
    """

    cancellation_requested = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done() and task.cancelled():
                raise
            cancellation_requested = True

    result = task.result()
    if cancellation_requested:
        raise asyncio.CancelledError
    return result


async def _await_durable_checkpoint(
    checkpoint: asyncio.Task[CephOperationRunRecord],
) -> CephOperationRunRecord:
    """Finish an already-started checkpoint across repeated cancellation."""

    return await _await_task_through_repeated_cancellation(checkpoint)


async def _append_noop(
    session: DatabaseSessionProtocol,
    run_record: CephOperationRunRecord,
    *,
    operation_index: int,
    operation: ProviderOperation,
    plan: PlanResponse,
    progress: _ApplyProgress,
) -> CephOperationRunRecord:
    result = {
        "operation_id": operation.id,
        "result": "noop",
        "target_ref": operation.target_ref,
        "action": operation.action,
        "kind": operation.kind,
    }
    progress.results.append(result)
    return await _append_event_checkpoint(
        session,
        run_record,
        event="noop",
        status="running",
        code="operation_noop",
        message="No provider mutation was required.",
        operation_index=operation_index,
        operation=operation,
        payload={"result": result},
        provider_task_refs=progress.task_refs,
        warnings=plan.warnings,
        errors=[],
        result_summary=progress.summary(len(plan.operations)),
    )


async def _dispatch_provider_operation(
    session: DatabaseSessionProtocol,
    run_record: CephOperationRunRecord,
    *,
    operation_index: int,
    operation: ProviderOperation,
    plan: PlanResponse,
    adapter: CephProviderAdapter,
    progress: _ApplyProgress,
) -> tuple[
    CephOperationRunRecord,
    dict[str, Any] | None,
    OperationRun | None,
    bool,
]:
    run_record = await _append_event_checkpoint(
        session,
        run_record,
        event="dispatch_intent",
        status="dispatching",
        code="dispatch_intent_persisted",
        message=(
            "Provider dispatch intent persisted before the SDK call; the leased worker "
            "is dispatching until a later checkpoint."
        ),
        operation_index=operation_index,
        operation=operation,
        provider_task_refs=progress.task_refs,
        warnings=plan.warnings,
        errors=[],
        result_summary=progress.summary(
            len(plan.operations),
            dispatching_operation_id=operation.id,
        ),
    )
    try:
        dispatch = asyncio.create_task(
            _apply_with_lease_heartbeat(
                session,
                run_record,
                adapter,
                operation,
            )
        )
        try:
            raw_result = await _await_task_through_repeated_cancellation(dispatch)
            cancellation_deferred = False
        except asyncio.CancelledError:
            # Once dispatch has started, let the guarded SDK/lease task finish.
            # A successful result must reach its task/synchronous evidence
            # checkpoint before the caller's cancellation is honored.
            raw_result = dispatch.result()
            cancellation_deferred = True
    except _CephRunLeaseLost as lease_loss:
        return (
            run_record,
            None,
            await _operation_after_lease_loss(session, lease_loss.run_id),
            False,
        )
    except CephWriteGateDenied as exc:
        errors = [
            f"{operation.action} {operation.kind} {operation.target_ref}: "
            "provider mutation denied by the current endpoint gate."
        ]
        run_record = await _append_event_checkpoint(
            session,
            run_record,
            event="dispatch_denied",
            status="failed",
            code=exc.reason,
            message="The endpoint write gate denied provider dispatch.",
            operation_index=operation_index,
            operation=operation,
            provider_task_refs=progress.task_refs,
            warnings=plan.warnings,
            errors=errors,
            result_summary=progress.summary(len(plan.operations), reason=exc.reason),
        )
        run = await operation_run_with_events(session, run_record)
        raise CephApplyError(
            403,
            {
                "reason": exc.reason,
                "detail": exc.detail,
                "operation_run_id": run.id,
                "plan_id": plan.id,
            },
            run,
        ) from exc
    except asyncio.CancelledError:
        await _persist_cancelled_checkpoint(
            session,
            run_record,
            event="dispatch_cancelled",
            code="provider_dispatch_cancelled",
            message="Dispatch was cancelled after intent; provider outcome is unknown.",
            operation_index=operation_index,
            operation=operation,
            plan=plan,
            progress=progress,
            error="Provider dispatch outcome is unknown.",
        )
        raise
    except Exception:  # noqa: BLE001 - transport details are secret-bearing
        run_record = await _append_event_checkpoint(
            session,
            run_record,
            event="dispatch_outcome_unknown",
            status="outcome_unknown",
            code="provider_dispatch_outcome_unknown",
            message="Provider transport failed after dispatch intent; outcome is unknown.",
            operation_index=operation_index,
            operation=operation,
            provider_task_refs=progress.task_refs,
            warnings=plan.warnings,
            errors=["Provider dispatch outcome is unknown."],
            result_summary=progress.summary(
                len(plan.operations),
                outcome_unknown_operation_id=operation.id,
            ),
        )
        return run_record, None, await operation_run_with_events(session, run_record), False

    safe_result = redact_secrets(raw_result)
    if not isinstance(safe_result, dict):
        safe_result = {"result": safe_result}
    return run_record, safe_result, None, cancellation_deferred


async def _apply_with_lease_heartbeat(  # noqa: C901 - dispatch/heartbeat ownership matrix
    session: DatabaseSessionProtocol,
    run_record: CephOperationRunRecord,
    adapter: CephProviderAdapter,
    operation: ProviderOperation,
) -> dict[str, Any]:
    """Await one SDK dispatch while renewing and verifying worker ownership."""

    interval = min(30.0, max(0.1, _run_lease_seconds() / 3))
    # Capture the primary key before any renewal: a failed renewal has rolled
    # the session back, expiring ``run_record``, and reading ``.id`` off an
    # expired instance would lazy-load through the sync facade and raise
    # ``MissingGreenlet`` instead of the intended lease-lost error.
    run_record_id = run_record.id
    dispatch_task = asyncio.create_task(adapter.apply(operation, confirm_destructive=True))

    async def renew_lease() -> bool:
        lock = getattr(adapter, "database_session_lock", None)
        if lock is None:
            return await _renew_run_lease(session, run_record)
        async with lock:
            return await _renew_run_lease(session, run_record)

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(interval)
            if dispatch_task.done():
                return
            if not await renew_lease():
                raise _CephRunLeaseLost(run_record_id)

    heartbeat_task = asyncio.create_task(heartbeat())
    try:
        done, _pending = await asyncio.wait(
            {dispatch_task, heartbeat_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if heartbeat_task in done:
            heartbeat_error = heartbeat_task.exception()
            if heartbeat_error is not None:
                dispatch_task.cancel()
                await asyncio.gather(dispatch_task, return_exceptions=True)
                raise heartbeat_error
        result = await dispatch_task
        # Do not let the background heartbeat use the same AsyncSession while
        # the foreground performs its final ownership check.
        if not heartbeat_task.done():
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        if not await renew_lease():
            raise _CephRunLeaseLost(run_record_id)
        return result
    except asyncio.CancelledError:
        dispatch_task.cancel()
        await asyncio.gather(dispatch_task, return_exceptions=True)
        raise
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)


async def _wait_for_provider_task(
    session: DatabaseSessionProtocol,
    run_record: CephOperationRunRecord,
    adapter: CephProviderAdapter,
    *,
    node: str,
    upid: str,
) -> dict[str, Any]:
    """Poll a submitted task while retaining one non-reclaimable run lease."""

    # Same expired-instance hazard as _apply_with_lease_heartbeat: a failed
    # renewal has rolled the session back, so read the id before any renewal.
    run_record_id = run_record.id

    async def heartbeat() -> None:
        lock = getattr(adapter, "database_session_lock", None)
        if lock is None:
            renewed = await _renew_run_lease(session, run_record)
        else:
            async with lock:
                renewed = await _renew_run_lease(session, run_record)
        if not renewed:
            raise _CephRunLeaseLost(run_record_id)

    if getattr(adapter, "supports_task_heartbeat", False):
        outcome = await adapter.wait_for_terminal(
            node,
            upid,
            heartbeat=heartbeat,
        )
    else:
        await heartbeat()
        outcome = await adapter.wait_for_terminal(node, upid)
    await heartbeat()
    return outcome


async def _claim_and_checkpoint_provider_task(
    session: DatabaseSessionProtocol,
    run_record: CephOperationRunRecord,
    *,
    operation_index: int,
    operation: ProviderOperation,
    plan: PlanResponse,
    progress: _ApplyProgress,
    safe_result: dict[str, Any],
    upid: str,
) -> CephOperationRunRecord | None:
    """Atomically claim a task reference and persist its submission evidence.

    ``None`` means another durable run already owns this exact provider task.
    Any unrelated integrity failure is re-raised rather than misclassified as a
    duplicate task reference.
    """

    submitted = {**safe_result, "result": "submitted"}
    claim = CephProviderTaskClaimRecord(
        provider=plan.provider,
        endpoint_id=plan.endpoint_id or 0,
        endpoint_config_revision=plan.endpoint_config_revision,
        provider_task_ref=upid,
        run_id=run_record.id,
        operation_index=operation_index,
        operation_id=_safe_text(operation.id),
    )
    try:
        return await _append_event_checkpoint(
            session,
            run_record,
            event="provider_task_submitted",
            status="running",
            code="provider_task_submitted",
            message="Provider accepted the task; terminal status is pending.",
            operation_index=operation_index,
            operation=operation,
            provider_task_ref=upid,
            payload={"result": submitted},
            provider_task_refs=[*progress.task_refs, upid],
            warnings=plan.warnings,
            errors=[],
            result_summary=progress.summary(
                len(plan.operations),
                submitted_operation_id=operation.id,
                results=[*progress.results, submitted],
            ),
            transaction_records=[claim],
        )
    except IntegrityError:
        await _maybe_await(session.rollback())
        existing_result = await _maybe_await(
            session.exec(
                select(CephProviderTaskClaimRecord)
                .where(CephProviderTaskClaimRecord.provider == plan.provider)
                .where(CephProviderTaskClaimRecord.provider_task_ref == upid)
            )
        )
        if existing_result.first() is None:
            raise
        await _maybe_await(session.refresh(run_record))
        return None


async def _claim_submitted_task_cancellation_safe(
    session: DatabaseSessionProtocol,
    run_record: CephOperationRunRecord,
    *,
    operation_index: int,
    operation: ProviderOperation,
    plan: PlanResponse,
    progress: _ApplyProgress,
    safe_result: dict[str, Any],
    upid: str,
) -> tuple[CephOperationRunRecord, bool]:
    """Shield the atomic claim/submission transaction from caller cancellation."""

    checkpoint = asyncio.create_task(
        _claim_and_checkpoint_provider_task(
            session,
            run_record,
            operation_index=operation_index,
            operation=operation,
            plan=plan,
            progress=progress,
            safe_result=safe_result,
            upid=upid,
        )
    )
    try:
        claimed_record = await _await_task_through_repeated_cancellation(checkpoint)
    except asyncio.CancelledError:
        try:
            claimed_record = checkpoint.result()
        except Exception:  # noqa: BLE001 - preserve the caller cancellation
            claimed_record = None
        if claimed_record is not None:
            progress.task_refs.append(upid)
        await _persist_cancelled_checkpoint(
            session,
            claimed_record or run_record,
            event=(
                "provider_task_poll_cancelled"
                if claimed_record is not None
                else "provider_task_claim_cancelled"
            ),
            code=(
                "provider_task_poll_cancelled"
                if claimed_record is not None
                else "provider_task_claim_cancelled"
            ),
            message=(
                "Task processing was cancelled after submission; outcome is unknown."
                if claimed_record is not None
                else "Task claiming was cancelled after dispatch; outcome is unknown."
            ),
            operation_index=operation_index,
            operation=operation,
            plan=plan,
            progress=progress,
            error="Submitted provider task outcome is unknown.",
            provider_task_ref=upid if claimed_record is not None else None,
            payload={"result": {**safe_result, "result": "submitted"}},
        )
        raise
    if claimed_record is None:
        return run_record, False
    progress.task_refs.append(upid)
    return claimed_record, True


async def _resolve_submitted_task(
    session: DatabaseSessionProtocol,
    run_record: CephOperationRunRecord,
    *,
    operation_index: int,
    operation: ProviderOperation,
    plan: PlanResponse,
    adapter: CephProviderAdapter,
    progress: _ApplyProgress,
    safe_result: dict[str, Any],
    upid: str,
    node: str,
) -> tuple[CephOperationRunRecord, OperationRun | None]:
    submitted = {**safe_result, "result": "submitted"}
    if not node:
        outcome = {"state": "outcome_unknown", "code": "provider_task_binding_missing"}
    else:
        try:
            outcome = await _wait_for_provider_task(
                session,
                run_record,
                adapter,
                node=node,
                upid=upid,
            )
        except _CephRunLeaseLost as lease_loss:
            return run_record, await _operation_after_lease_loss(session, lease_loss.run_id)
        except asyncio.CancelledError:
            await _persist_cancelled_checkpoint(
                session,
                run_record,
                event="provider_task_poll_cancelled",
                code="provider_task_poll_cancelled",
                message="Task polling was cancelled after submission; outcome is unknown.",
                operation_index=operation_index,
                operation=operation,
                plan=plan,
                progress=progress,
                error="Submitted provider task outcome is unknown.",
                provider_task_ref=upid,
                payload={"result": submitted},
            )
            raise
        except Exception:  # noqa: BLE001 - transport details are secret-bearing
            outcome = {
                "state": "outcome_unknown",
                "code": "provider_task_status_unavailable",
            }

    if not isinstance(outcome, dict):
        outcome = {
            "state": "outcome_unknown",
            "code": "provider_task_status_invalid",
        }
    state = str(outcome.get("state") or "outcome_unknown").casefold()
    if state not in {"completed", "failed", "outcome_unknown"}:
        state = "outcome_unknown"
        code = "provider_task_status_invalid"
    else:
        code = str(outcome.get("code") or "provider_task_status_unavailable")
    terminal_result = {**submitted, "result": state, "task_state": state}
    progress.results.append(terminal_result)
    if state == "completed":
        progress.completed_count += 1
        checkpoint = asyncio.create_task(
            _append_event_checkpoint(
                session,
                run_record,
                event="provider_task_completed",
                status="running",
                code=code,
                message="The submitted provider task completed successfully.",
                operation_index=operation_index,
                operation=operation,
                provider_task_ref=upid,
                payload={"result": terminal_result},
                provider_task_refs=progress.task_refs,
                warnings=plan.warnings,
                errors=[],
                result_summary=progress.summary(len(plan.operations)),
            )
        )
        try:
            run_record = await _await_durable_checkpoint(checkpoint)
        except asyncio.CancelledError:
            await _persist_cancelled_checkpoint(
                session,
                run_record,
                event="provider_task_completion_cancelled",
                code="provider_task_completion_cancelled",
                message=(
                    "Execution was cancelled after task completion; remaining plan "
                    "outcome is unknown."
                ),
                operation_index=operation_index,
                operation=operation,
                plan=plan,
                progress=progress,
                error="Plan execution was cancelled after provider task completion.",
                provider_task_ref=upid,
                payload={"result": terminal_result},
            )
            raise
        return run_record, None

    terminal_status = "failed" if state == "failed" else "outcome_unknown"
    error = (
        "Submitted provider task failed."
        if terminal_status == "failed"
        else "Submitted provider task outcome is unknown."
    )
    run_record = await _await_durable_checkpoint(
        asyncio.create_task(
            _append_event_checkpoint(
                session,
                run_record,
                event=f"provider_task_{terminal_status}",
                status=terminal_status,
                code=code,
                message=error,
                operation_index=operation_index,
                operation=operation,
                provider_task_ref=upid,
                payload={"result": terminal_result},
                provider_task_refs=progress.task_refs,
                warnings=plan.warnings,
                errors=[error],
                result_summary=progress.summary(len(plan.operations)),
            )
        )
    )
    return run_record, await operation_run_with_events(session, run_record)


async def _append_synchronous_completion(
    session: DatabaseSessionProtocol,
    run_record: CephOperationRunRecord,
    *,
    operation_index: int,
    operation: ProviderOperation,
    plan: PlanResponse,
    progress: _ApplyProgress,
    safe_result: dict[str, Any],
) -> CephOperationRunRecord:
    completed_result = {**safe_result, "result": "completed"}
    progress.results.append(completed_result)
    progress.completed_count += 1
    return await _append_event_checkpoint(
        session,
        run_record,
        event="dispatch_completed",
        status="running",
        code="provider_dispatch_completed",
        message="The synchronous provider operation completed.",
        operation_index=operation_index,
        operation=operation,
        payload={"result": completed_result},
        provider_task_refs=progress.task_refs,
        warnings=plan.warnings,
        errors=[],
        result_summary=progress.summary(len(plan.operations)),
    )


async def _append_synchronous_completion_cancellation_safe(
    session: DatabaseSessionProtocol,
    run_record: CephOperationRunRecord,
    *,
    operation_index: int,
    operation: ProviderOperation,
    plan: PlanResponse,
    progress: _ApplyProgress,
    safe_result: dict[str, Any],
) -> CephOperationRunRecord:
    """Persist a proven synchronous outcome before honoring cancellation."""

    checkpoint = asyncio.create_task(
        _append_synchronous_completion(
            session,
            run_record,
            operation_index=operation_index,
            operation=operation,
            plan=plan,
            progress=progress,
            safe_result=safe_result,
        )
    )
    try:
        return await _await_task_through_repeated_cancellation(checkpoint)
    except asyncio.CancelledError:
        try:
            completed_record = checkpoint.result()
        except Exception:  # noqa: BLE001 - preserve the caller cancellation
            completed_record = run_record
        await _persist_cancelled_checkpoint(
            session,
            completed_record,
            event="synchronous_completion_cancelled",
            code="synchronous_completion_cancelled",
            message=(
                "Execution was cancelled after synchronous provider completion; "
                "remaining plan outcome is unknown."
            ),
            operation_index=operation_index,
            operation=operation,
            plan=plan,
            progress=progress,
            error="Plan execution was cancelled after synchronous completion.",
            payload={"result": {**safe_result, "result": "completed"}},
        )
        raise


async def _task_binding_failure(
    session: DatabaseSessionProtocol,
    run_record: CephOperationRunRecord,
    *,
    operation_index: int,
    operation: ProviderOperation,
    plan: PlanResponse,
    progress: _ApplyProgress,
    safe_result: dict[str, Any],
    code: str,
) -> OperationRun:
    run_record = await _await_durable_checkpoint(
        asyncio.create_task(
            _append_event_checkpoint(
                session,
                run_record,
                event="provider_task_binding_invalid",
                status="outcome_unknown",
                code=code,
                message=(
                    "The provider did not return one new node-consistent task reference; "
                    "mutation outcome is unknown."
                ),
                operation_index=operation_index,
                operation=operation,
                payload={"result": safe_result},
                provider_task_refs=progress.task_refs,
                warnings=plan.warnings,
                errors=["Provider mutation task binding is invalid."],
                result_summary=progress.summary(
                    len(plan.operations),
                    outcome_unknown_operation_id=operation.id,
                ),
            )
        )
    )
    return await operation_run_with_events(session, run_record)


async def _execute_plan_operations(  # noqa: C901 - explicit security checkpoints
    plan: PlanResponse,
    adapter: CephProviderAdapter,
    session: DatabaseSessionProtocol,
    run_record: CephOperationRunRecord,
) -> OperationRun:
    progress = _ApplyProgress()
    for operation_index, operation in enumerate(plan.operations):
        if operation.action == "noop":
            run_record = await _append_noop(
                session,
                run_record,
                operation_index=operation_index,
                operation=operation,
                plan=plan,
                progress=progress,
            )
            continue

        (
            run_record,
            safe_result,
            terminal_run,
            cancellation_deferred,
        ) = await _dispatch_provider_operation(
            session,
            run_record,
            operation_index=operation_index,
            operation=operation,
            plan=plan,
            adapter=adapter,
            progress=progress,
        )
        if terminal_run is not None:
            return terminal_run
        assert safe_result is not None  # nosec B101 - internal typed state invariant

        candidates = _task_ref_candidates(safe_result)
        new_refs = _task_refs_from_result(safe_result, provider=plan.provider)
        declares_sync = getattr(adapter, "declares_synchronous_success", None)
        synchronous_success = bool(
            callable(declares_sync) and declares_sync(operation, safe_result)
        )
        if synchronous_success:
            if candidates or new_refs:
                terminal = await _task_binding_failure(
                    session,
                    run_record,
                    operation_index=operation_index,
                    operation=operation,
                    plan=plan,
                    progress=progress,
                    safe_result=safe_result,
                    code="provider_synchronous_result_ambiguous",
                )
                if cancellation_deferred:
                    raise asyncio.CancelledError
                return terminal
            run_record = await _append_synchronous_completion_cancellation_safe(
                session,
                run_record,
                operation_index=operation_index,
                operation=operation,
                plan=plan,
                progress=progress,
                safe_result=safe_result,
            )
            if cancellation_deferred:
                await _persist_cancelled_checkpoint(
                    session,
                    run_record,
                    event="synchronous_completion_cancelled",
                    code="synchronous_completion_cancelled",
                    message=(
                        "Execution was cancelled after synchronous provider completion; "
                        "remaining plan outcome is unknown."
                    ),
                    operation_index=operation_index,
                    operation=operation,
                    plan=plan,
                    progress=progress,
                    error="Plan execution was cancelled after synchronous completion.",
                    payload={"result": {**safe_result, "result": "completed"}},
                )
                raise asyncio.CancelledError
            continue

        if plan.provider == "proxmox":
            expected_node = operation.node or ""
            result_node = str(safe_result.get("node") or "")
            if len(candidates) != 1 or len(new_refs) != 1:
                terminal = await _task_binding_failure(
                    session,
                    run_record,
                    operation_index=operation_index,
                    operation=operation,
                    plan=plan,
                    progress=progress,
                    safe_result=safe_result,
                    code="provider_task_reference_invalid",
                )
                if cancellation_deferred:
                    raise asyncio.CancelledError
                return terminal
            if new_refs[0] in progress.task_refs:
                terminal = await _task_binding_failure(
                    session,
                    run_record,
                    operation_index=operation_index,
                    operation=operation,
                    plan=plan,
                    progress=progress,
                    safe_result=safe_result,
                    code="provider_task_reference_reused",
                )
                if cancellation_deferred:
                    raise asyncio.CancelledError
                return terminal
            if (
                not expected_node
                or result_node != expected_node
                or _proxmox_upid_node(new_refs[0]) != expected_node
            ):
                terminal = await _task_binding_failure(
                    session,
                    run_record,
                    operation_index=operation_index,
                    operation=operation,
                    plan=plan,
                    progress=progress,
                    safe_result=safe_result,
                    code="provider_task_node_mismatch",
                )
                if cancellation_deferred:
                    raise asyncio.CancelledError
                return terminal
        upid = new_refs[0] if len(new_refs) == 1 else None
        if upid:
            run_record, claimed = await _claim_submitted_task_cancellation_safe(
                session,
                run_record,
                operation_index=operation_index,
                operation=operation,
                plan=plan,
                progress=progress,
                safe_result=safe_result,
                upid=upid,
            )
            if not claimed:
                terminal = await _task_binding_failure(
                    session,
                    run_record,
                    operation_index=operation_index,
                    operation=operation,
                    plan=plan,
                    progress=progress,
                    safe_result=safe_result,
                    code="provider_task_reference_reused",
                )
                if cancellation_deferred:
                    raise asyncio.CancelledError
                return terminal
            if cancellation_deferred:
                await _persist_cancelled_checkpoint(
                    session,
                    run_record,
                    event="provider_task_poll_cancelled",
                    code="provider_task_poll_cancelled",
                    message="Task processing was cancelled after submission; outcome is unknown.",
                    operation_index=operation_index,
                    operation=operation,
                    plan=plan,
                    progress=progress,
                    error="Submitted provider task outcome is unknown.",
                    provider_task_ref=upid,
                    payload={"result": {**safe_result, "result": "submitted"}},
                )
                raise asyncio.CancelledError
            run_record, terminal_run = await _resolve_submitted_task(
                session,
                run_record,
                operation_index=operation_index,
                operation=operation,
                plan=plan,
                adapter=adapter,
                progress=progress,
                safe_result=safe_result,
                upid=upid,
                node=str(safe_result.get("node") or ""),
            )
            if terminal_run is not None:
                return terminal_run
            continue

        progress.task_refs.extend(new_refs)

        run_record = await _await_durable_checkpoint(
            asyncio.create_task(
                _append_event_checkpoint(
                    session,
                    run_record,
                    event="provider_task_reference_missing",
                    status="outcome_unknown",
                    code="provider_task_reference_missing",
                    message=(
                        "The provider returned no valid task reference and did not explicitly "
                        "declare synchronous success; mutation outcome is unknown."
                    ),
                    operation_index=operation_index,
                    operation=operation,
                    payload={"result": safe_result},
                    provider_task_refs=progress.task_refs,
                    warnings=plan.warnings,
                    errors=["Provider mutation outcome is unknown."],
                    result_summary=progress.summary(
                        len(plan.operations),
                        outcome_unknown_operation_id=operation.id,
                    ),
                )
            )
        )
        if cancellation_deferred:
            raise asyncio.CancelledError
        return await operation_run_with_events(session, run_record)

    destructive_count = sum(operation.is_destructive for operation in plan.operations)
    run_record = await _await_durable_checkpoint(
        asyncio.create_task(
            _append_event_checkpoint(
                session,
                run_record,
                event="run_completed",
                status="completed",
                code="run_completed",
                message="All provider operations reached a known successful terminal state.",
                provider_task_refs=progress.task_refs,
                warnings=plan.warnings,
                errors=[],
                result_summary=progress.summary(
                    len(plan.operations),
                    noop=sum(operation.action == "noop" for operation in plan.operations),
                    destructive=destructive_count,
                ),
            )
        )
    )
    return await operation_run_with_events(session, run_record)


async def apply_plan(
    plan: PlanResponse,
    request: ApplyRequest,
    adapter: CephProviderAdapter,
    session: DatabaseSessionProtocol,
) -> OperationRun:
    if not ceph_write_execution_enabled():
        raise CephApplyError(
            503,
            {
                "reason": "ceph_write_execution_disabled",
                "detail": (
                    "Ceph apply requires explicit operator and trusted actor gateway gates."
                ),
                "plan_id": plan.id,
            },
        )
    validation_errors = [item.message for item in plan.validations if item.severity == "error"]
    if validation_errors:
        await _raise_with_run(
            session,
            plan=plan,
            request=request,
            status_code=422,
            status="failed",
            reason="plan_validation_failed",
            errors=validation_errors,
        )

    blockers = [operation for operation in plan.operations if not operation.supported]
    if blockers:
        await _raise_with_run(
            session,
            plan=plan,
            request=request,
            status_code=409,
            status="blocked",
            reason="plan_has_blocked_operations",
            errors=[
                operation.blocked_reason or "provider operation is blocked"
                for operation in blockers
            ],
        )

    try:
        run_record = await _consume_approval_and_create_run(
            session,
            plan=plan,
            request=request,
        )
    except CephApprovalError as exc:
        raise CephApplyError(
            exc.status_code,
            {
                "reason": exc.reason,
                "detail": exc.detail,
                "plan_id": plan.id,
                **exc.recovery,
            },
        ) from exc
    try:
        return await _execute_plan_operations(plan, adapter, session, run_record)
    except _CephRunLeaseLost as lease_loss:
        return await _operation_after_lease_loss(session, lease_loss.run_id)


async def reconcile_provider(
    request: ReconcileRequest,
    adapter: CephProviderAdapter,
    session: DatabaseSessionProtocol,
) -> OperationRun:
    try:
        capabilities = await adapter.capabilities()
    except CephProviderBoundaryError:
        raise
    except Exception:  # noqa: BLE001
        raise CephProviderBoundaryError(
            "provider_capabilities_unavailable",
            "Ceph provider capabilities could not be read safely.",
        ) from None
    run_record = await _create_run(
        session,
        plan=None,
        provider=capabilities.provider,
        actor=request.actor,
        branch_schema_id=request.branch_schema_id,
        request_summary={"scope": request.scope, "operation": "reconcile"},
        warnings=capabilities.notes,
    )
    run_record = await _append_event_checkpoint(
        session,
        run_record,
        event="reconcile_started",
        status="running",
        code="reconcile_started",
        message="Read-only provider reconciliation started.",
        warnings=capabilities.notes,
        errors=[],
        result_summary={"operation": "reconcile", "result": "running"},
    )
    try:
        result = await adapter.reconcile(request.scope)
    except CephProviderBoundaryError as exc:
        run_record = await _append_event_checkpoint(
            session,
            run_record,
            event="reconcile_failed",
            status="failed",
            code=exc.reason,
            message="Read-only provider reconciliation failed safely.",
            warnings=capabilities.notes,
            errors=["Provider reconciliation was unavailable."],
            result_summary={
                "operation": "reconcile",
                "result": "failed",
                "correlation_id": exc.correlation_id,
            },
        )
        return await operation_run_with_events(session, run_record)
    except Exception:  # noqa: BLE001
        failure = CephProviderBoundaryError(
            "provider_reconcile_unavailable",
            "Ceph provider reconciliation could not be completed safely.",
        )
        run_record = await _append_event_checkpoint(
            session,
            run_record,
            event="reconcile_failed",
            status="failed",
            code=failure.reason,
            message="Read-only provider reconciliation failed safely.",
            warnings=capabilities.notes,
            errors=["Provider reconciliation was unavailable."],
            result_summary={
                "operation": "reconcile",
                "result": "failed",
                "correlation_id": failure.correlation_id,
            },
        )
        return await operation_run_with_events(session, run_record)

    run_record = await _append_event_checkpoint(
        session,
        run_record,
        event="reconcile_completed",
        status="completed",
        code="reconcile_completed",
        message="Read-only provider reconciliation completed.",
        warnings=capabilities.notes,
        errors=[],
        result_summary=redact_secrets(result),
    )
    return await operation_run_with_events(session, run_record)


def record_to_operation_run(
    record: CephOperationRunRecord,
    *,
    events: list[OperationEvent] | None = None,
) -> OperationRun:
    return OperationRun(
        id=_safe_text(record.id) or "operation-redacted",
        plan_id=_safe_text(record.plan_id),
        endpoint_id=record.endpoint_id,
        endpoint_config_revision=record.endpoint_config_revision,
        plan_digest=_safe_text(record.plan_digest),
        requester=_safe_text(record.requester),
        approver=_safe_text(record.approver),
        approval_id=_safe_text(record.approval_id),
        status=cast("OperationStatus", record.status),
        actor=_safe_text(record.actor),
        source_branch_schema_id=_safe_text(record.source_branch_schema_id),
        provider=_safe_text(record.provider) or "unknown",
        request_summary=redact_secrets(record.request_summary or {}),
        provider_task_refs=[
            safe
            for item in (record.provider_task_refs or [])
            if (safe := _safe_text(item)) is not None
        ],
        created_at=_dt_from_ts(record.created_at),
        updated_at=_dt_from_ts(record.updated_at),
        lease_expires_at=(
            _dt_from_ts(record.lease_expires_at) if record.lease_expires_at is not None else None
        ),
        warnings=redact_secrets(record.warnings or []),
        errors=redact_secrets(record.errors or []),
        result_summary=redact_secrets(record.result_summary or {}),
        events=events or [],
    )


__all__ = [
    "CephApprovalError",
    "CephApplyError",
    "CephPlanExpired",
    "CephPlanIntegrityError",
    "CephPlanNotFound",
    "apply_plan",
    "approval_recovery_metadata",
    "build_plan",
    "canonical_plan_digest",
    "issue_plan_approval",
    "load_persisted_plan",
    "normalize_actor",
    "operation_run_with_events",
    "persist_plan",
    "prevalidate_plan_approval",
    "reconcile_provider",
    "record_to_operation_run",
    "record_to_operation_event",
    "recover_stale_operation_run",
    "redact_secrets",
    "utcnow",
    "validate_payload",
    "validated_approval_recovery_metadata",
]
