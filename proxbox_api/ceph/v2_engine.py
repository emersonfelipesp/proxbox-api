"""Ceph v2 plan/apply engine."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import BaseModel, ValidationError
from sqlmodel import select

from proxbox_api.ceph.v2_providers.base import CephCapabilityUnsupported, CephProviderAdapter
from proxbox_api.ceph.v2_schemas import (
    ApplyRequest,
    CephMetricSnapshot,
    DesiredObject,
    DesiredStateBundle,
    OperationRun,
    PlanRequest,
    PlanResponse,
    ProviderCapabilities,
    ProviderOperation,
    ReconcileRequest,
    ValidationResponse,
    ValidationResult,
)
from proxbox_api.database import CephOperationRunRecord
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

_PLAN_STORE: dict[str, PlanResponse] = {}
_PLAN_STORE_ORDER: list[str] = []
_PLAN_STORE_LIMIT = 512
_SECRET_KEY_FRAGMENTS = ("password", "secret", "token", "private_key", "access_key")
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
    """Raised when a plan id is not present in the in-process plan store."""


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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt_from_ts(value: float) -> datetime:
    return datetime.fromtimestamp(value, timezone.utc)


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
        return str(value)
    return value


def redact_secrets(value: Any) -> Any:
    """Return a JSON-safe copy with secret-bearing fields redacted."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_string = str(key)
            lowered = key_string.lower()
            if lowered == "credential_ref":
                redacted[key_string] = _jsonable(item)
            elif any(fragment in lowered for fragment in _SECRET_KEY_FRAGMENTS):
                redacted[key_string] = "[REDACTED]"
            else:
                redacted[key_string] = redact_secrets(item)
        return redacted
    if isinstance(value, list | tuple | set):
        return [redact_secrets(item) for item in value]
    return _jsonable(value)


def remember_plan(plan: PlanResponse) -> None:
    _PLAN_STORE[plan.id] = plan
    _PLAN_STORE_ORDER.append(plan.id)
    while len(_PLAN_STORE_ORDER) > _PLAN_STORE_LIMIT:
        old_id = _PLAN_STORE_ORDER.pop(0)
        _PLAN_STORE.pop(old_id, None)


def get_plan(plan_id: str) -> PlanResponse:
    try:
        return _PLAN_STORE[plan_id]
    except KeyError as exc:
        raise CephPlanNotFound(plan_id) from exc


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
    ``warning`` (surfaced, not a hard block — destructive ops already require
    explicit confirmation) so operators can override deliberately.
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
                        "explicit confirmation."
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
        return ValidationResponse(
            valid=False,
            results=[
                ValidationResult(
                    severity="error",
                    code="schema_validation_failed",
                    message=str(error.get("msg") or "schema validation failed"),
                    target=".".join(str(part) for part in error.get("loc", ())) or None,
                )
                for error in exc.errors()
            ],
        )
    results = validation_results_for_request(request)
    return ValidationResponse(
        valid=not any(result.severity == "error" for result in results), results=results
    )


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
    return op


def _provider_supports_operation(
    capabilities: ProviderCapabilities,
    operation: ProviderOperation,
) -> bool:
    if operation.action == "noop":
        return True
    keys = (
        f"{operation.kind}:{operation.action}",
        f"{operation.kind}:*",
        operation.action,
        operation.kind,
    )
    for key in keys:
        if key in capabilities.operation_kinds:
            return capabilities.operation_kinds[key]
    return capabilities.apply


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
) -> PlanResponse:
    capabilities = await adapter.capabilities()
    validations = validation_results_for_request(request)
    warnings = list(capabilities.notes)

    if any(result.severity == "error" for result in validations):
        operations: list[ProviderOperation] = []
        live: dict[str, Any] = {}
    else:
        try:
            operations, live = await _raw_operations(request, adapter, capabilities)
            if capabilities.supported and capabilities.plan:
                operations = await adapter.plan(operations)
        except CephCapabilityUnsupported as exc:
            warnings.append(str(exc))
            operations = _generic_operations_from_desired(request, capabilities.provider)
            live = {}

    normalized = sorted(
        (_normalize_operation(operation, capabilities) for operation in operations),
        key=_operation_priority,
    )
    snapshot = _snapshot_from_request(request, metric_snapshot)
    validations = validations + metric_safety_validations(snapshot, normalized)
    blocked_actions = [operation for operation in normalized if not operation.supported]
    branch_id = request.branch_schema_id
    plan = PlanResponse(
        id=str(uuid4()),
        provider=capabilities.provider,
        netbox_branch_schema_id=branch_id,
        source_branch_schema_id=branch_id,
        operations=normalized,
        validations=validations,
        warnings=warnings,
        blocked_actions=blocked_actions,
        created_at=utcnow(),
        live_state_summary=_jsonable(live.get("summary", {})) if isinstance(live, dict) else {},
        request_summary=redact_secrets(request),
    )
    remember_plan(plan)
    return plan


async def _commit_and_refresh(session: object, record: CephOperationRunRecord) -> None:
    await _maybe_await(session.commit())
    await _maybe_await(session.refresh(record))


async def _create_run(
    session: object,
    *,
    plan: PlanResponse | None,
    provider: str,
    actor: str | None,
    branch_schema_id: str | None,
    request_summary: dict[str, Any],
    status: str = "running",
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
    result_summary: dict[str, Any] | None = None,
) -> CephOperationRunRecord:
    now = time.time()
    record = CephOperationRunRecord(
        id=str(uuid4()),
        plan_id=plan.id if plan is not None else None,
        status=status,
        actor=actor,
        source_branch_schema_id=branch_schema_id,
        provider=provider,
        request_summary=redact_secrets(request_summary),
        provider_task_refs=[],
        created_at=now,
        updated_at=now,
        warnings=warnings or [],
        errors=errors or [],
        result_summary=result_summary or {},
    )
    session.add(record)
    await _commit_and_refresh(session, record)
    return record


async def _update_run(
    session: object,
    record: CephOperationRunRecord,
    *,
    status: str,
    provider_task_refs: list[str] | None = None,
    warnings: list[str] | None = None,
    errors: list[str] | None = None,
    result_summary: dict[str, Any] | None = None,
) -> CephOperationRunRecord:
    record.status = status
    record.updated_at = time.time()
    if provider_task_refs is not None:
        record.provider_task_refs = list(provider_task_refs)
    if warnings is not None:
        record.warnings = list(warnings)
    if errors is not None:
        record.errors = list(errors)
    if result_summary is not None:
        record.result_summary = redact_secrets(result_summary)
    session.add(record)
    await _commit_and_refresh(session, record)
    return record


async def completed_run_for_plan(
    session: object,
    plan_id: str,
) -> OperationRun | None:
    result = await _maybe_await(
        session.exec(
            select(CephOperationRunRecord)
            .where(CephOperationRunRecord.plan_id == plan_id)
            .where(CephOperationRunRecord.status == "completed")
            .order_by(CephOperationRunRecord.created_at.desc())
        )
    )
    record = result.first()
    if record is None:
        return None
    run = record_to_operation_run(record)
    run.result_summary = {**run.result_summary, "idempotent_replay": True}
    return run


def _confirmation_satisfied(plan: PlanResponse, request: ApplyRequest) -> bool:
    if request.confirm_destructive:
        return True
    return request.confirmation_token in {
        plan.id,
        f"confirm:{plan.id}",
        f"confirm-destructive:{plan.id}",
    }


def _task_refs_from_result(result: dict[str, Any]) -> list[str]:
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
    return refs


async def _raise_with_run(
    session: object,
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
    run = record_to_operation_run(run_record)
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


async def apply_plan(
    plan: PlanResponse,
    request: ApplyRequest,
    adapter: CephProviderAdapter,
    session: object,
) -> OperationRun:
    existing = await completed_run_for_plan(session, plan.id)
    if existing is not None:
        return existing

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

    destructive = [operation for operation in plan.operations if operation.is_destructive]
    if destructive and not _confirmation_satisfied(plan, request):
        await _raise_with_run(
            session,
            plan=plan,
            request=request,
            status_code=409,
            status="failed",
            reason="destructive_confirmation_required",
            errors=[
                "Destructive Ceph operations require confirm_destructive=true or "
                f"confirmation_token='confirm-destructive:{plan.id}'."
            ],
        )

    run_record = await _create_run(
        session,
        plan=plan,
        provider=plan.provider,
        actor=request.actor,
        branch_schema_id=request.branch_schema_id or plan.source_branch_schema_id,
        request_summary={
            "plan_id": plan.id,
            "operations": [operation.model_dump(mode="json") for operation in plan.operations],
        },
        warnings=plan.warnings,
    )

    task_refs: list[str] = []
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for operation in plan.operations:
        try:
            result = await adapter.apply(
                operation,
                confirm_destructive=_confirmation_satisfied(plan, request),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{operation.action} {operation.kind} {operation.target_ref}: {exc}")
            break
        task_refs.extend(_task_refs_from_result(result))
        results.append(redact_secrets(result))

    if errors:
        updated = await _update_run(
            session,
            run_record,
            status="failed",
            provider_task_refs=task_refs,
            warnings=plan.warnings,
            errors=errors,
            result_summary={
                "applied": len(results),
                "total": len(plan.operations),
                "results": results,
            },
        )
        return record_to_operation_run(updated)

    updated = await _update_run(
        session,
        run_record,
        status="completed",
        provider_task_refs=task_refs,
        warnings=plan.warnings,
        errors=[],
        result_summary={
            "applied": len(results),
            "noop": sum(1 for operation in plan.operations if operation.action == "noop"),
            "total": len(plan.operations),
            "destructive": len(destructive),
            "results": results,
        },
    )
    return record_to_operation_run(updated)


async def reconcile_provider(
    request: ReconcileRequest,
    adapter: CephProviderAdapter,
    session: object,
) -> OperationRun:
    capabilities = await adapter.capabilities()
    run_record = await _create_run(
        session,
        plan=None,
        provider=capabilities.provider,
        actor=request.actor,
        branch_schema_id=request.branch_schema_id,
        request_summary={"scope": request.scope, "operation": "reconcile"},
        warnings=capabilities.notes,
    )
    try:
        result = await adapter.reconcile(request.scope)
    except Exception as exc:  # noqa: BLE001
        updated = await _update_run(
            session,
            run_record,
            status="failed",
            warnings=capabilities.notes,
            errors=[str(exc)],
            result_summary={"operation": "reconcile", "result": "failed"},
        )
        return record_to_operation_run(updated)

    updated = await _update_run(
        session,
        run_record,
        status="completed",
        warnings=capabilities.notes,
        errors=[],
        result_summary=redact_secrets(result),
    )
    return record_to_operation_run(updated)


def record_to_operation_run(record: CephOperationRunRecord) -> OperationRun:
    return OperationRun(
        id=record.id,
        plan_id=record.plan_id,
        status=record.status,  # type: ignore[arg-type]
        actor=record.actor,
        source_branch_schema_id=record.source_branch_schema_id,
        provider=record.provider,
        request_summary=record.request_summary or {},
        provider_task_refs=record.provider_task_refs or [],
        created_at=_dt_from_ts(record.created_at),
        updated_at=_dt_from_ts(record.updated_at),
        warnings=record.warnings or [],
        errors=record.errors or [],
        result_summary=record.result_summary or {},
    )


__all__ = [
    "CephApplyError",
    "CephPlanNotFound",
    "apply_plan",
    "build_plan",
    "completed_run_for_plan",
    "get_plan",
    "reconcile_provider",
    "record_to_operation_run",
    "redact_secrets",
    "utcnow",
    "validate_payload",
]
