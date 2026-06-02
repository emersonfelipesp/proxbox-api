"""Ceph v2 control-plane routes mounted under ``/ceph/v2``.

These endpoints let NetBox drive Ceph desired-state without direct operator
access to Proxmox or external Ceph tooling. The surface satisfies both:

* the resource-style API from issue #95 (``/plans``, ``/plans/{id}``,
  ``/plans/{id}/apply``, ``/operations/{id}``, ``/operations/{id}/events``,
  ``/validate``, ``/reconcile``, ``/capabilities``, ``/metrics``); and
* the flat client contract the ``netbox-ceph`` orchestrator already calls
  (``/plan``, ``/apply``, ``/reconcile``, ``/capabilities``, ``/metrics``).

Request handlers stay thin: planning, applying, validation, capability gating,
destructive-confirmation enforcement, idempotency, and persistence all live in
:mod:`proxbox_api.ceph.v2_engine`. Secrets never reach NetBox; provider
credentials are resolved behind the adapter layer and redacted at the engine
boundary.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from proxbox_api.ceph.v2_engine import (
    CephApplyError,
    CephPlanNotFound,
    apply_plan,
    build_plan,
    get_plan,
    reconcile_provider,
    record_to_operation_run,
    remember_plan,
    validate_payload,
)
from proxbox_api.ceph.v2_providers import adapter_for_provider, provider_names
from proxbox_api.ceph.v2_schemas import (
    ApplyRequest,
    CapabilitiesResponse,
    MetricsResponse,
    OperationRun,
    PlanRequest,
    PlanResponse,
    ReconcileRequest,
    SSEEvent,
    ValidationResponse,
)
from proxbox_api.database import AsyncDatabaseSessionDep, CephOperationRunRecord
from proxbox_api.logger import logger
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()

ActorHeader = Annotated[str | None, Header(alias="X-Proxbox-Actor")]


def _with_actor(model: Any, actor: str | None) -> None:
    """Prefer an explicit body actor, else fall back to the request header."""

    if actor and getattr(model, "actor", None) in (None, ""):
        model.actor = actor


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def ceph_v2_capabilities(
    pxs: ProxmoxSessionsDep,
    provider: Annotated[str | None, Query()] = None,
) -> CapabilitiesResponse:
    """Expose per-provider capability flags so the NetBox UI can gate controls."""

    names = [provider] if provider else provider_names()
    providers = []
    for name in names:
        adapter = adapter_for_provider(name, list(pxs))
        providers.append(await adapter.capabilities())
    return CapabilitiesResponse(providers=providers)


@router.post("/validate", response_model=ValidationResponse)
async def ceph_v2_validate(payload: dict[str, Any]) -> ValidationResponse:
    """Validate a single desired object or a full desired-state bundle."""

    return validate_payload(payload)


async def _build_and_remember(request: PlanRequest, pxs: ProxmoxSessionsDep) -> PlanResponse:
    adapter = adapter_for_provider(request.provider, list(pxs))
    plan = await build_plan(request, adapter)
    remember_plan(plan)
    return plan


@router.post("/plans", response_model=PlanResponse)
async def ceph_v2_create_plan(
    request: PlanRequest,
    pxs: ProxmoxSessionsDep,
    actor: ActorHeader = None,
) -> PlanResponse:
    """Build a plan from NetBox desired state and current provider state."""

    _with_actor(request, actor)
    return await _build_and_remember(request, pxs)


@router.get("/plans/{plan_id}", response_model=PlanResponse)
async def ceph_v2_get_plan(plan_id: str) -> PlanResponse:
    """Inspect a previously built plan: diff, validations, warnings, blocked actions."""

    try:
        return get_plan(plan_id)
    except CephPlanNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id!r} not found.") from exc


@router.post("/plans/{plan_id}/apply", response_model=OperationRun)
async def ceph_v2_apply_plan(
    plan_id: str,
    request: ApplyRequest,
    pxs: ProxmoxSessionsDep,
    session: AsyncDatabaseSessionDep,
    actor: ActorHeader = None,
) -> OperationRun:
    """Execute a validated plan, gated on destructive confirmation."""

    _with_actor(request, actor)
    try:
        plan = get_plan(plan_id)
    except CephPlanNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id!r} not found.") from exc
    adapter = adapter_for_provider(plan.provider, list(pxs))
    try:
        return await apply_plan(plan, request, adapter, session)
    except CephApplyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/plan", response_model=PlanResponse)
async def ceph_v2_plan_compat(
    request: PlanRequest,
    pxs: ProxmoxSessionsDep,
    actor: ActorHeader = None,
) -> PlanResponse:
    """Flat client-contract alias for ``POST /plans`` (netbox-ceph orchestrator)."""

    _with_actor(request, actor)
    return await _build_and_remember(request, pxs)


@router.post("/apply", response_model=OperationRun)
async def ceph_v2_apply_compat(
    request: ApplyRequest,
    pxs: ProxmoxSessionsDep,
    session: AsyncDatabaseSessionDep,
    actor: ActorHeader = None,
) -> OperationRun:
    """Flat client-contract path: build a plan from the payload, then apply it."""

    _with_actor(request, actor)
    plan_request = PlanRequest(
        provider=request.provider,
        desired_state=request.desired_state or None,  # type: ignore[arg-type]
        operations=request.operations,
        scope=request.scope,
        actor=request.actor,
        netbox_branch_schema_id=request.netbox_branch_schema_id,
        source_branch_schema_id=request.source_branch_schema_id,
        request_id=request.request_id,
    )
    plan = await _build_and_remember(plan_request, pxs)
    if request.plan_id is None:
        request.plan_id = plan.id
    adapter = adapter_for_provider(plan.provider, list(pxs))
    try:
        return await apply_plan(plan, request, adapter, session)
    except CephApplyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def _load_operation(session: AsyncDatabaseSessionDep, operation_id: str) -> OperationRun:
    record = await session.get(CephOperationRunRecord, operation_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Operation {operation_id!r} not found.")
    return record_to_operation_run(record)


@router.get("/operations/{operation_id}", response_model=OperationRun)
async def ceph_v2_operation(
    operation_id: str,
    session: AsyncDatabaseSessionDep,
) -> OperationRun:
    """Retrieve operation status, provider task refs, warnings, errors, results."""

    return await _load_operation(session, operation_id)


@router.get("/operations/{operation_id}/events")
async def ceph_v2_operation_events(
    operation_id: str,
    session: AsyncDatabaseSessionDep,
) -> StreamingResponse:
    """Stream operation progress as Server-Sent Events.

    Operations are applied synchronously, so by the time a client subscribes the
    run is already persisted. The stream replays the run's recorded lifecycle as
    a deterministic, well-typed event sequence the NetBox UI can render.
    """

    run = await _load_operation(session, operation_id)

    async def event_stream() -> Any:
        sequence = 0
        for event_name, message in (
            ("accepted", "Operation accepted."),
            (run.status, f"Operation {run.status}."),
        ):
            frame = SSEEvent(
                event=str(event_name),
                operation_id=run.id,
                status=run.status,
                message=message,
                sequence=sequence,
                timestamp=run.updated_at,
                data={
                    "provider": run.provider,
                    "provider_task_refs": run.provider_task_refs,
                    "warnings": run.warnings,
                    "errors": run.errors,
                    "result_summary": run.result_summary,
                },
            )
            sequence += 1
            yield f"data: {json.dumps(json.loads(frame.model_dump_json()))}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/reconcile", response_model=OperationRun)
async def ceph_v2_reconcile(
    request: ReconcileRequest,
    pxs: ProxmoxSessionsDep,
    session: AsyncDatabaseSessionDep,
    actor: ActorHeader = None,
) -> OperationRun:
    """Reconcile provider state back into NetBox inventory/current-state summaries."""

    _with_actor(request, actor)
    adapter = adapter_for_provider(request.provider, list(pxs))
    return await reconcile_provider(request, adapter, session)


@router.get("/metrics", response_model=MetricsResponse)
async def ceph_v2_metrics(
    pxs: ProxmoxSessionsDep,
    provider: Annotated[str, Query()] = "proxmox",
    object_ref: Annotated[str | None, Query()] = None,
) -> MetricsResponse:
    """Return the latest provider metrics for the requested scope."""

    adapter = adapter_for_provider(provider, list(pxs))
    scope: dict[str, Any] = {}
    if object_ref:
        scope["object_ref"] = object_ref
    warnings: list[str] = []
    try:
        metrics = await adapter.metrics(scope)
    except Exception as exc:  # noqa: BLE001 - surface adapter gaps as a warning, not a 500
        logger.warning("Ceph v2 metrics unavailable for provider %s: %s", provider, exc)
        metrics = {}
        warnings.append(str(exc))
    return MetricsResponse(provider=provider, scope=scope, metrics=metrics, warnings=warnings)
