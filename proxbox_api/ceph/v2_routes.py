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
from pydantic import BaseModel, Field
from sqlmodel import select

from proxbox_api.ceph.prometheus import (
    PrometheusSourceConfig,
    validate_source,
)
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
    CephMetricSnapshot,
    MetricsResponse,
    OperationRun,
    PlanRequest,
    PlanResponse,
    ReconcileRequest,
    SSEEvent,
    ValidationResponse,
)
from proxbox_api.database import (
    AsyncDatabaseSessionDep,
    CephOperationRunRecord,
    PrometheusSource,
)
from proxbox_api.logger import logger
from proxbox_api.session.proxmox import ProxmoxSessionsDep

router = APIRouter()

ActorHeader = Annotated[str | None, Header(alias="X-Proxbox-Actor")]


def _source_to_config(source: PrometheusSource) -> PrometheusSourceConfig:
    return PrometheusSourceConfig(
        url=source.url,
        bearer_token=source.get_decrypted_bearer_token(),
        verify_ssl=source.verify_ssl,
        timeout=source.timeout_seconds,
    )


async def _resolve_prometheus_source(
    session: AsyncDatabaseSessionDep, cluster_ref: str | None
) -> PrometheusSource | None:
    """Pick the enabled Prometheus source bound to ``cluster_ref``, else any."""

    stmt = select(PrometheusSource).where(PrometheusSource.enabled == True)  # noqa: E712
    rows = list((await session.exec(stmt)).all())
    if cluster_ref:
        for row in rows:
            if row.cluster_ref == cluster_ref:
                return row
    return rows[0] if rows else None


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
    session: AsyncDatabaseSessionDep,
    provider: Annotated[str, Query()] = "proxmox",
    object_ref: Annotated[str | None, Query()] = None,
) -> MetricsResponse:
    """Return the latest provider metrics for the requested scope.

    For ``provider=prometheus`` the configured Prometheus source is resolved
    from the DB and injected into the adapter scope, and the result is parsed
    into a typed :class:`CephMetricSnapshot`.
    """

    adapter = adapter_for_provider(provider, list(pxs))
    scope: dict[str, Any] = {}
    if object_ref:
        scope["object_ref"] = object_ref
    warnings: list[str] = []
    if provider == "prometheus":
        source = await _resolve_prometheus_source(session, object_ref)
        if source is None:
            warnings.append("no Prometheus source configured")
        else:
            scope["prometheus_source"] = _source_to_config(source)
    try:
        metrics = await adapter.metrics(scope)
    except Exception as exc:  # noqa: BLE001 - surface adapter gaps as a warning, not a 500
        logger.warning("Ceph v2 metrics unavailable for provider %s: %s", provider, exc)
        metrics = {}
        warnings.append(str(exc))
    snapshot: CephMetricSnapshot | None = None
    if metrics:
        try:
            snapshot = CephMetricSnapshot.model_validate(metrics)
        except Exception:  # noqa: BLE001 - non-snapshot providers return free-form metrics
            snapshot = None
    # Drop the (unserializable) source config from the echoed scope.
    scope.pop("prometheus_source", None)
    return MetricsResponse(
        provider=provider, scope=scope, metrics=metrics, snapshot=snapshot, warnings=warnings
    )


class PrometheusSourceCreate(BaseModel):
    """Request body to register a Prometheus source for Ceph metrics."""

    name: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)
    bearer_token: str | None = None
    credential_ref: str | None = None
    cluster_ref: str | None = None
    verify_ssl: bool = True
    enabled: bool = True
    timeout_seconds: int = 15
    scrape_interval_seconds: int = 60


class PrometheusSourceOut(BaseModel):
    """Redacted Prometheus source record (never exposes the bearer token)."""

    id: int
    name: str
    url: str
    credential_ref: str | None = None
    cluster_ref: str | None = None
    verify_ssl: bool
    enabled: bool
    timeout_seconds: int
    scrape_interval_seconds: int
    has_token: bool


def _source_out(source: PrometheusSource) -> PrometheusSourceOut:
    return PrometheusSourceOut(
        id=source.id or 0,
        name=source.name,
        url=source.url,
        credential_ref=source.credential_ref,
        cluster_ref=source.cluster_ref,
        verify_ssl=source.verify_ssl,
        enabled=source.enabled,
        timeout_seconds=source.timeout_seconds,
        scrape_interval_seconds=source.scrape_interval_seconds,
        has_token=bool(source.bearer_token),
    )


@router.get("/metrics/sources", response_model=list[PrometheusSourceOut])
async def ceph_v2_list_prometheus_sources(
    session: AsyncDatabaseSessionDep,
) -> list[PrometheusSourceOut]:
    """List configured Prometheus sources (bearer tokens redacted)."""

    rows = list((await session.exec(select(PrometheusSource))).all())
    return [_source_out(row) for row in rows]


@router.post("/metrics/sources", response_model=PrometheusSourceOut, status_code=201)
async def ceph_v2_create_prometheus_source(
    payload: PrometheusSourceCreate,
    session: AsyncDatabaseSessionDep,
) -> PrometheusSourceOut:
    """Register a Prometheus source. The bearer token is encrypted at rest."""

    existing = (
        await session.exec(select(PrometheusSource).where(PrometheusSource.name == payload.name))
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="A source with that name already exists.")
    source = PrometheusSource(
        name=payload.name,
        url=payload.url,
        credential_ref=payload.credential_ref,
        cluster_ref=payload.cluster_ref,
        verify_ssl=payload.verify_ssl,
        enabled=payload.enabled,
        timeout_seconds=payload.timeout_seconds,
        scrape_interval_seconds=payload.scrape_interval_seconds,
    )
    source.set_encrypted_bearer_token(payload.bearer_token)
    session.add(source)
    await session.commit()
    await session.refresh(source)
    return _source_out(source)


@router.post("/metrics/sources/{source_id}/validate")
async def ceph_v2_validate_prometheus_source(
    source_id: int,
    session: AsyncDatabaseSessionDep,
) -> dict[str, Any]:
    """Probe a registered Prometheus source for reachability."""

    source = await session.get(PrometheusSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Prometheus source not found.")
    ok, error = await validate_source(_source_to_config(source))
    return {"id": source_id, "ok": ok, "error": error}
