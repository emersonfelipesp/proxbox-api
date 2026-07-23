"""Ceph v2 control-plane routes mounted under ``/ceph/v2``.

These endpoints let NetBox drive Ceph desired-state without direct operator
access to Proxmox or external Ceph tooling. The surface satisfies both:

* the resource-style API from issue #95 (``/plans``, ``/plans/{id}``,
  ``/plans/{id}/apply``, ``/operations/{id}``, ``/operations/{id}/events``,
  ``/validate``, ``/reconcile``, ``/capabilities``, ``/metrics``); and
* the flat client contract the ``netbox-ceph`` orchestrator already calls
  (``/plan``, ``/apply``, ``/reconcile``, ``/capabilities``, ``/metrics``).

Request handlers stay thin: durable planning, two-person approval, atomic token
consumption, validation, capability gating, and audit persistence live in
:mod:`proxbox_api.ceph.v2_engine`. Proxmox writes require one exact persisted
endpoint and a fresh ``allow_writes`` check before every provider mutation.
Secrets never reach NetBox; provider credentials are resolved behind the
adapter layer and redacted at the engine boundary.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlmodel import select

from proxbox_api.ceph.dashboard_client import (
    DashboardEndpointConfig,
    validate_dashboard_endpoint,
)
from proxbox_api.ceph.endpoint_binding import (
    BoundProxmoxSession,
    create_bound_proxmox_session,
)
from proxbox_api.ceph.prometheus import (
    PrometheusSourceConfig,
    validate_source,
)
from proxbox_api.ceph.rgw_client import RGWAdminConfig
from proxbox_api.ceph.v2_engine import (
    CephApplyError,
    CephApprovalError,
    CephPlanExpired,
    CephPlanIntegrityError,
    CephPlanNotFound,
    apply_plan,
    build_plan,
    issue_plan_approval,
    load_persisted_plan,
    normalize_actor,
    operation_run_with_events,
    persist_plan,
    prevalidate_plan_approval,
    reconcile_provider,
    recover_stale_operation_run,
    redact_secrets,
    validate_payload,
    validated_approval_recovery_metadata,
)
from proxbox_api.ceph.v2_providers import adapter_for_provider, provider_names
from proxbox_api.ceph.v2_providers.base import (
    CEPH_TRUSTED_ACTOR_GATEWAY_ENV,
    CEPH_WRITE_EXECUTION_ENV,
    CephProviderAdapter,
    CephProviderBoundaryError,
    CephWriteGateDenied,
    ceph_write_execution_enabled,
)
from proxbox_api.ceph.v2_providers.dashboard import DashboardCephProviderAdapter
from proxbox_api.ceph.v2_providers.external import ExternalCephProviderAdapter
from proxbox_api.ceph.v2_providers.prometheus import PrometheusCephProviderAdapter
from proxbox_api.ceph.v2_providers.proxmox import ProxmoxCephProviderAdapter
from proxbox_api.ceph.v2_schemas import (
    ApplyRequest,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalStatusResponse,
    CapabilitiesResponse,
    CephMetricSnapshot,
    MetricsResponse,
    OperationRun,
    PlanRequest,
    PlanResponse,
    ReconcileRequest,
    SSEEvent,
    ValidationResponse,
    validate_credential_ref,
)
from proxbox_api.database import (
    AsyncDatabaseSessionDep,
    CephApprovalRecord,
    CephDashboardEndpoint,
    CephExternalCluster,
    CephOperationRunRecord,
    PrometheusSource,
    ProxmoxEndpoint,
)
from proxbox_api.logger import logger
from proxbox_api.session.proxmox import ProxmoxSessionsDep
from proxbox_api.session.proxmox_core import ProxmoxSession

router = APIRouter()

ActorHeader = Annotated[str | None, Header(alias="X-Proxbox-Actor")]
_FORBIDDEN_PROXMOX_SELECTORS = {
    "source",
    "name",
    "domain",
    "ip_address",
    "port",
    "endpoint_ids",
    "proxmox_endpoint_ids",
}


def _require_ceph_write_execution_enabled() -> None:
    if ceph_write_execution_enabled():
        return
    raise HTTPException(
        status_code=503,
        detail={
            "reason": "ceph_write_execution_disabled",
            "detail": (
                "Ceph approval and apply are disabled until the operator enables "
                f"{CEPH_WRITE_EXECUTION_ENV} and confirms a trusted gateway with "
                f"{CEPH_TRUSTED_ACTOR_GATEWAY_ENV}. The gateway must authenticate and "
                "overwrite X-Proxbox-Actor."
            ),
        },
    )


def _mask_write_capabilities(capabilities: Any) -> Any:
    if ceph_write_execution_enabled():
        return capabilities
    masked = capabilities.model_copy(deep=True)
    masked.apply = False
    masked.destructive_operations = False
    masked.operation_kinds = {
        key: bool(value and key.endswith(":noop")) for key, value in masked.operation_kinds.items()
    }
    masked.notes = [
        *redact_secrets(masked.notes),
        (
            "Ceph write execution is default-off. Enable the operator gate only after "
            "an authenticated gateway overwrites X-Proxbox-Actor."
        ),
    ]
    return masked


def _safe_credential_ref(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return validate_credential_ref(value)
    except ValueError:
        return None


def _required_actor(actor_header: str | None) -> str:
    try:
        return normalize_actor(actor_header)
    except CephApprovalError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"reason": exc.reason, "detail": exc.detail},
        ) from exc


def _bind_required_actor(model: Any, actor_header: str | None) -> str:
    """Make the non-empty actor header authoritative and reject body spoofing."""

    actor = _required_actor(actor_header)
    body_actor = getattr(model, "actor", None)
    if body_actor and str(body_actor).strip().casefold() != actor.casefold():
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "actor_mismatch",
                "detail": "The body actor must match X-Proxbox-Actor.",
            },
        )
    model.actor = actor
    return actor


def _reject_proxmox_selector_query(request: Request) -> None:
    selectors = sorted(_FORBIDDEN_PROXMOX_SELECTORS.intersection(request.query_params))
    if selectors:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "endpoint_selector_query_forbidden",
                "detail": (
                    "Ceph endpoint-bound operations accept only the durable endpoint_id; "
                    "generic Proxmox selectors are forbidden."
                ),
            },
        )


def _gate_http_error(exc: CephWriteGateDenied) -> HTTPException:
    if exc.reason == "endpoint_missing":
        status_code = 404
    elif exc.reason in {
        "endpoint_configuration_changed",
        "endpoint_session_ambiguous",
        "endpoint_session_binding_changed",
        "endpoint_session_binding_mismatch",
    }:
        status_code = 409
    else:
        status_code = 403
    return HTTPException(
        status_code=status_code,
        detail={"reason": exc.reason, "detail": exc.detail},
    )


def _provider_http_error(exc: CephProviderBoundaryError) -> HTTPException:
    logger.warning(
        "Ceph v2 provider boundary failure reason=%s correlation_id=%s",
        exc.reason,
        exc.correlation_id,
    )
    return HTTPException(
        status_code=502,
        detail={
            "reason": exc.reason,
            "detail": exc.detail,
            "correlation_id": exc.correlation_id,
        },
    )


async def _exact_proxmox_adapter(
    session: AsyncDatabaseSessionDep,
    endpoint_id: int,
) -> tuple[ProxmoxCephProviderAdapter, BoundProxmoxSession]:
    try:
        bound, endpoint = await create_bound_proxmox_session(session, endpoint_id)
    except CephWriteGateDenied as exc:
        raise _gate_http_error(exc) from exc
    except CephProviderBoundaryError as exc:
        raise _provider_http_error(exc) from exc
    return (
        ProxmoxCephProviderAdapter(
            bound_session=bound,
            database_session=session,
            writes_authorized=bool(endpoint.enabled and endpoint.allow_writes),
        ),
        bound,
    )


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


def _dashboard_to_config(endpoint: CephDashboardEndpoint) -> DashboardEndpointConfig:
    return DashboardEndpointConfig(
        base_url=endpoint.base_url,
        username=endpoint.username,
        password=endpoint.get_decrypted_password(),
        token=endpoint.get_decrypted_token(),
        verify_ssl=endpoint.verify_ssl,
        api_version=endpoint.api_version,
        timeout=endpoint.timeout_seconds,
    )


async def _resolve_dashboard_endpoint(
    session: AsyncDatabaseSessionDep, cluster_ref: str | None
) -> CephDashboardEndpoint | None:
    """Pick the enabled Ceph Dashboard endpoint bound to ``cluster_ref``, else any."""

    stmt = select(CephDashboardEndpoint).where(CephDashboardEndpoint.enabled == True)  # noqa: E712
    rows = list((await session.exec(stmt)).all())
    if cluster_ref:
        for row in rows:
            if row.cluster_ref == cluster_ref:
                return row
    return rows[0] if rows else None


def _scope_object_ref(scope: dict[str, Any]) -> str | None:
    value = scope.get("object_ref") or scope.get("cluster_ref")
    return str(value) if value else None


async def _resolve_external_cluster(
    session: AsyncDatabaseSessionDep, cluster_ref: str | None
) -> CephExternalCluster | None:
    stmt = select(CephExternalCluster).where(CephExternalCluster.enabled == True)  # noqa: E712
    rows = list((await session.exec(stmt)).all())
    if cluster_ref:
        for row in rows:
            if row.cluster_ref == cluster_ref or row.name == cluster_ref:
                return row
    return rows[0] if rows else None


def _rgw_config_from_cluster(cluster: CephExternalCluster) -> RGWAdminConfig | None:
    access = cluster.get_decrypted_rgw_access_key()
    secret = cluster.get_decrypted_rgw_secret_key()
    if cluster.rgw_admin_url and access and secret:
        return RGWAdminConfig(
            base_url=cluster.rgw_admin_url,
            access_key=access,
            secret_key=secret,
            verify_ssl=cluster.verify_ssl,
        )
    return None


async def _external_adapter(
    cluster: CephExternalCluster | None,
    pxs: list[ProxmoxSession],
    session: AsyncDatabaseSessionDep,
) -> ExternalCephProviderAdapter:
    if cluster is None:
        return ExternalCephProviderAdapter(list(pxs))
    dashboard_cfg = None
    if cluster.dashboard_endpoint_id is not None:
        endpoint = await session.get(CephDashboardEndpoint, cluster.dashboard_endpoint_id)
        if endpoint is not None:
            dashboard_cfg = _dashboard_to_config(endpoint)
    prometheus_cfg = None
    if cluster.prometheus_source_id is not None:
        source = await session.get(PrometheusSource, cluster.prometheus_source_id)
        if source is not None:
            prometheus_cfg = _source_to_config(source)
    return ExternalCephProviderAdapter(
        list(pxs),
        dashboard=dashboard_cfg,
        prometheus=prometheus_cfg,
        rgw=_rgw_config_from_cluster(cluster),
        ceph_version=cluster.ceph_version_hint,
    )


async def build_adapter(
    provider: str | None,
    pxs: list[ProxmoxSession],
    session: AsyncDatabaseSessionDep,
    *,
    object_ref: str | None = None,
) -> CephProviderAdapter:
    """Resolve a provider adapter, injecting DB-stored endpoints where needed."""

    name = (provider or "proxmox").strip().lower().replace("-", "_")
    if name == "proxmox":
        return ProxmoxCephProviderAdapter(read_sessions=list(pxs))
    if name == "dashboard":
        endpoint = await _resolve_dashboard_endpoint(session, object_ref)
        config = _dashboard_to_config(endpoint) if endpoint else None
        return DashboardCephProviderAdapter(list(pxs), endpoint=config)
    if name == "prometheus":
        source = await _resolve_prometheus_source(session, object_ref)
        config = _source_to_config(source) if source else None
        return PrometheusCephProviderAdapter(list(pxs), source=config)
    if name == "external":
        cluster = await _resolve_external_cluster(session, object_ref)
        return await _external_adapter(cluster, list(pxs), session)
    return adapter_for_provider(provider, list(pxs))


def _with_actor(model: Any, actor: str | None) -> None:
    """Prefer an explicit body actor, else fall back to the request header."""

    if actor and getattr(model, "actor", None) in (None, ""):
        model.actor = actor


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def ceph_v2_capabilities(
    http_request: Request,
    session: AsyncDatabaseSessionDep,
    provider: Annotated[str | None, Query()] = None,
    endpoint_id: Annotated[int | None, Query(gt=0)] = None,
) -> CapabilitiesResponse:
    """Expose per-provider capability flags so the NetBox UI can gate controls."""

    names = [provider] if provider else provider_names()
    providers = []
    for name in names:
        normalized = (name or "proxmox").strip().lower().replace("-", "_")
        if normalized == "proxmox" and endpoint_id is not None:
            _reject_proxmox_selector_query(http_request)
            if not ceph_write_execution_enabled():
                endpoint = await session.get(ProxmoxEndpoint, endpoint_id)
                if endpoint is None:
                    raise _gate_http_error(
                        CephWriteGateDenied(
                            "endpoint_missing",
                            "The selected local Proxmox endpoint does not exist.",
                        )
                    )
                adapter = ProxmoxCephProviderAdapter()
                capabilities = await adapter.capabilities()
                capabilities.endpoint_id = endpoint_id
                providers.append(_mask_write_capabilities(capabilities))
                continue
            adapter, bound = await _exact_proxmox_adapter(session, endpoint_id)
            try:
                try:
                    providers.append(_mask_write_capabilities(await adapter.capabilities()))
                except CephProviderBoundaryError as exc:
                    raise _provider_http_error(exc) from exc
                except Exception:  # noqa: BLE001 - provider details stay private
                    failure = CephProviderBoundaryError(
                        "provider_capabilities_unavailable",
                        "Ceph provider capabilities could not be read safely.",
                    )
                    raise _provider_http_error(failure) from None
            finally:
                await bound.aclose()
            continue
        try:
            adapter = await build_adapter(name, [], session)
            providers.append(_mask_write_capabilities(await adapter.capabilities()))
        except CephProviderBoundaryError as exc:
            raise _provider_http_error(exc) from exc
        except Exception:  # noqa: BLE001 - provider details stay private
            failure = CephProviderBoundaryError(
                "provider_capabilities_unavailable",
                "Ceph provider capabilities could not be read safely.",
            )
            raise _provider_http_error(failure) from None
    return CapabilitiesResponse(providers=providers)


@router.post("/validate", response_model=ValidationResponse)
async def ceph_v2_validate(payload: dict[str, Any]) -> ValidationResponse:
    """Validate a single desired object or a full desired-state bundle."""

    return validate_payload(payload)


async def _build_and_persist(
    request: PlanRequest,
    session: AsyncDatabaseSessionDep,
) -> PlanResponse:
    name = request.provider.strip().lower().replace("-", "_")
    bound: BoundProxmoxSession | None = None
    try:
        if name == "proxmox":
            if request.endpoint_id is None:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "reason": "endpoint_id_required",
                        "detail": "Proxmox Ceph plans require an explicit local endpoint_id.",
                    },
                )
            adapter, bound = await _exact_proxmox_adapter(session, request.endpoint_id)
        else:
            adapter = await build_adapter(
                request.provider,
                [],
                session,
                object_ref=_scope_object_ref(request.scope),
            )
        plan = await build_plan(
            request,
            adapter,
            endpoint_config_revision=(
                bound.endpoint_config_revision if bound is not None else None
            ),
        )
        return await persist_plan(session, plan)
    except HTTPException:
        raise
    except CephProviderBoundaryError as exc:
        raise _provider_http_error(exc) from exc
    except Exception:  # noqa: BLE001 - provider details stay private
        failure = CephProviderBoundaryError(
            "provider_plan_unavailable",
            "Ceph provider state or plan generation could not be completed safely.",
        )
        raise _provider_http_error(failure) from None
    finally:
        if bound is not None:
            await bound.aclose()


async def _load_plan_or_http(
    session: AsyncDatabaseSessionDep,
    plan_id: str,
    *,
    require_current: bool = True,
) -> PlanResponse:
    try:
        return await load_persisted_plan(session, plan_id, require_current=require_current)
    except CephPlanNotFound as exc:
        raise HTTPException(status_code=404, detail="Plan not found.") from exc
    except CephPlanExpired as exc:
        raise HTTPException(
            status_code=410,
            detail={"reason": "plan_expired", "detail": "The persisted plan has expired."},
        ) from exc
    except CephPlanIntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "plan_integrity_failed",
                "detail": "The persisted plan failed canonical digest verification.",
            },
        ) from exc


def _validate_apply_envelope(
    plan_id: str,
    plan: PlanResponse,
    request: ApplyRequest,
) -> None:
    if plan.provider != "proxmox":
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "durable_provider_write_gate_unavailable",
                "detail": (
                    "Apply is closed for this provider until its endpoint selector and "
                    "write gate are durably bound to the canonical plan."
                ),
            },
        )
    if request.plan_id not in (None, plan_id):
        raise HTTPException(
            status_code=409,
            detail={"reason": "apply_plan_mismatch", "detail": "Body and path plan_id differ."},
        )
    if request.operations or request.desired_state is not None or request.scope:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "canonical_plan_required",
                "detail": (
                    "Apply may not replace operations, desired state, or scope from the "
                    "persisted plan."
                ),
            },
        )
    if request.provider.strip().lower().replace("-", "_") != plan.provider:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "apply_provider_mismatch",
                "detail": "Provider differs from the plan.",
            },
        )
    if request.branch_schema_id not in (None, plan.source_branch_schema_id):
        raise HTTPException(
            status_code=409,
            detail={"reason": "apply_branch_mismatch", "detail": "Branch differs from the plan."},
        )
    if request.endpoint_id != plan.endpoint_id:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "apply_endpoint_mismatch",
                "detail": "Apply endpoint_id differs from the persisted plan.",
            },
        )


@router.post("/plans", response_model=PlanResponse)
async def ceph_v2_create_plan(
    http_request: Request,
    request: PlanRequest,
    session: AsyncDatabaseSessionDep,
    actor: ActorHeader = None,
) -> PlanResponse:
    """Build a plan from NetBox desired state and current provider state."""

    _bind_required_actor(request, actor)
    _reject_proxmox_selector_query(http_request)
    return await _build_and_persist(request, session)


@router.get("/plans/{plan_id}", response_model=PlanResponse)
async def ceph_v2_get_plan(
    plan_id: str,
    session: AsyncDatabaseSessionDep,
) -> PlanResponse:
    """Inspect a previously built plan: diff, validations, warnings, blocked actions."""

    return await _load_plan_or_http(session, plan_id, require_current=False)


@router.post("/plans/{plan_id}/apply", response_model=OperationRun)
async def ceph_v2_apply_plan(
    plan_id: str,
    http_request: Request,
    request: ApplyRequest,
    session: AsyncDatabaseSessionDep,
    actor: ActorHeader = None,
) -> OperationRun:
    """Execute one immutable persisted plan using a single-use approval token."""

    _require_ceph_write_execution_enabled()
    _bind_required_actor(request, actor)
    plan = await _load_plan_or_http(session, plan_id)
    _validate_apply_envelope(plan_id, plan, request)
    request.plan_id = plan_id
    _reject_proxmox_selector_query(http_request)

    # Validation and blocked-operation rejection are intentionally evaluated
    # before constructing an authenticated provider session. ``apply_plan``
    # persists the rejected audit run but does not touch the adapter on either
    # path.
    if any(item.severity == "error" for item in plan.validations) or any(
        not operation.supported for operation in plan.operations
    ):
        try:
            return await apply_plan(
                plan,
                request,
                ProxmoxCephProviderAdapter(),
                session,
            )
        except CephApplyError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    if plan.endpoint_id is None:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "plan_endpoint_missing",
                "detail": "The persisted Proxmox plan has no endpoint binding.",
            },
        )
    try:
        await prevalidate_plan_approval(session, plan=plan, request=request)
    except CephApprovalError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "reason": exc.reason,
                "detail": exc.detail,
                "plan_id": plan.id,
                **exc.recovery,
            },
        ) from exc
    adapter, bound = await _exact_proxmox_adapter(session, plan.endpoint_id)
    try:
        try:
            await bound.verify_fresh(
                session,
                expected_revision=plan.endpoint_config_revision,
            )
        except CephWriteGateDenied as exc:
            raise _gate_http_error(exc) from exc
        capabilities = await adapter.capabilities()
        if not capabilities.apply:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "provider_write_capability_unavailable",
                    "detail": "The selected endpoint cannot currently execute Ceph writes.",
                },
            )
        try:
            return await apply_plan(plan, request, adapter, session)
        except CephApplyError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    finally:
        await bound.aclose()


async def _existing_approval_recovery(
    session: AsyncDatabaseSessionDep,
    plan: PlanResponse,
) -> dict[str, Any] | None:
    result = await session.exec(
        select(CephApprovalRecord).where(CephApprovalRecord.plan_id == plan.id)
    )
    record = result.first()
    if record is None:
        return None
    try:
        return await validated_approval_recovery_metadata(session, record, plan=plan)
    except CephApprovalError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"reason": exc.reason, "detail": exc.detail},
        ) from exc


@router.post(
    "/plans/{plan_id}/approvals",
    response_model=ApprovalResponse,
    status_code=201,
)
async def ceph_v2_approve_plan(
    plan_id: str,
    http_request: Request,
    request: ApprovalRequest,
    session: AsyncDatabaseSessionDep,
    actor: ActorHeader = None,
) -> ApprovalResponse:
    """Issue one short-lived opaque token after independent actor approval."""

    _require_ceph_write_execution_enabled()
    approver = _required_actor(actor)
    plan = await _load_plan_or_http(session, plan_id)
    if plan.provider != "proxmox":
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "durable_provider_write_gate_unavailable",
                "detail": (
                    "Approval is closed for this provider until its endpoint selector "
                    "and write gate are durably bound to the canonical plan."
                ),
            },
        )
    validation_errors = [item for item in plan.validations if item.severity == "error"]
    if validation_errors or plan.blocked_actions:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "plan_not_approvable",
                "detail": "Plans with validation errors or blocked operations cannot be approved.",
            },
        )
    if plan.requester and plan.requester.casefold() == approver.casefold():
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "two_person_approval_required",
                "detail": "The plan requester and approver must be different actors.",
            },
        )
    recovery = await _existing_approval_recovery(session, plan)
    if recovery is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "approval_already_issued",
                "detail": (
                    "This canonical plan already has an approval authority; "
                    "recover its status by id."
                ),
                **recovery,
            },
        )
    if request.endpoint_id != plan.endpoint_id:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "approval_endpoint_mismatch",
                "detail": "Approval endpoint_id differs from the persisted plan.",
            },
        )
    _reject_proxmox_selector_query(http_request)
    if plan.endpoint_id is None:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "plan_endpoint_missing",
                "detail": "The persisted Proxmox plan has no endpoint binding.",
            },
        )
    adapter, bound = await _exact_proxmox_adapter(session, plan.endpoint_id)
    try:
        try:
            await bound.verify_fresh(
                session,
                expected_revision=plan.endpoint_config_revision,
            )
        except CephWriteGateDenied as exc:
            raise _gate_http_error(exc) from exc
        if not (await adapter.capabilities()).apply:
            raise HTTPException(
                status_code=409,
                detail={
                    "reason": "provider_write_capability_unavailable",
                    "detail": "The selected endpoint cannot currently execute Ceph writes.",
                },
            )
        try:
            return await issue_plan_approval(
                session,
                plan=plan,
                endpoint_id=request.endpoint_id,
                approver=approver,
            )
        except CephApprovalError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"reason": exc.reason, "detail": exc.detail, **exc.recovery},
            ) from exc
    finally:
        await bound.aclose()


@router.get("/approvals/{approval_id}", response_model=ApprovalStatusResponse)
async def ceph_v2_approval_status(
    approval_id: str,
    session: AsyncDatabaseSessionDep,
) -> ApprovalStatusResponse:
    """Recover safe approval/run metadata without a transient raw token."""

    record = await session.get(CephApprovalRecord, approval_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Approval not found.")
    try:
        await validated_approval_recovery_metadata(session, record)
    except CephApprovalError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"reason": exc.reason, "detail": exc.detail},
        ) from exc
    return ApprovalStatusResponse(
        id=record.id,
        plan_id=record.plan_id,
        plan_digest=record.plan_digest,
        endpoint_id=record.endpoint_id,
        endpoint_config_revision=record.endpoint_config_revision,
        requester=record.requester,
        approver=record.approver,
        created_at=datetime.fromtimestamp(record.created_at, timezone.utc),
        expires_at=datetime.fromtimestamp(record.expires_at, timezone.utc),
        consumed_at=(
            datetime.fromtimestamp(record.consumed_at, timezone.utc)
            if record.consumed_at is not None
            else None
        ),
        consumed_by=redact_secrets(record.consumed_by),
        operation_run_id=redact_secrets(record.operation_run_id),
    )


@router.post("/plan", response_model=PlanResponse)
async def ceph_v2_plan_compat(
    http_request: Request,
    request: PlanRequest,
    session: AsyncDatabaseSessionDep,
    actor: ActorHeader = None,
) -> PlanResponse:
    """Flat client-contract alias for ``POST /plans`` (netbox-ceph orchestrator)."""

    _bind_required_actor(request, actor)
    _reject_proxmox_selector_query(http_request)
    return await _build_and_persist(request, session)


@router.post("/apply", response_model=OperationRun)
async def ceph_v2_apply_compat(
    http_request: Request,
    request: ApplyRequest,
    session: AsyncDatabaseSessionDep,
    actor: ActorHeader = None,
) -> OperationRun:
    """Compatibility path that accepts only a durable plan id and approval token."""

    _require_ceph_write_execution_enabled()
    _bind_required_actor(request, actor)
    _reject_proxmox_selector_query(http_request)
    if request.plan_id is None:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "persisted_plan_required",
                "detail": "Inline Ceph apply is closed; create, approve, then apply a persisted plan.",
            },
        )
    return await ceph_v2_apply_plan(
        request.plan_id,
        http_request,
        request,
        session,
        actor,
    )


async def _load_operation(session: AsyncDatabaseSessionDep, operation_id: str) -> OperationRun:
    record = await session.get(CephOperationRunRecord, operation_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Operation not found.")
    record = await recover_stale_operation_run(session, record)
    return await operation_run_with_events(session, record)


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

    The stream replays the durable append-only dispatch/task history. It never
    infers provider completion merely from UPID submission.
    """

    run = await _load_operation(session, operation_id)

    async def event_stream() -> Any:
        for durable_event in run.events:
            frame = SSEEvent(
                event=durable_event.event,
                operation_id=run.id,
                status=durable_event.status,
                message=durable_event.message,
                sequence=durable_event.sequence,
                timestamp=durable_event.created_at,
                data={
                    "code": durable_event.code,
                    "operation_index": durable_event.operation_index,
                    "operation_id": durable_event.operation_id,
                    "kind": durable_event.kind,
                    "action": durable_event.action,
                    "target_ref": durable_event.target_ref,
                    "provider_task_ref": durable_event.provider_task_ref,
                    "payload": durable_event.payload,
                    "provider": run.provider,
                },
            )
            safe_frame = redact_secrets(json.loads(frame.model_dump_json()))
            yield f"data: {json.dumps(safe_frame)}\n\n"

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
    try:
        adapter = await build_adapter(
            request.provider,
            list(pxs),
            session,
            object_ref=_scope_object_ref(request.scope),
        )
        return await reconcile_provider(request, adapter, session)
    except CephProviderBoundaryError as exc:
        raise _provider_http_error(exc) from exc
    except Exception:  # noqa: BLE001 - provider details stay private
        failure = CephProviderBoundaryError(
            "provider_reconcile_unavailable",
            "Ceph provider reconciliation could not be completed safely.",
        )
        raise _provider_http_error(failure) from None


@router.get("/metrics", response_model=MetricsResponse)
async def ceph_v2_metrics(
    pxs: ProxmoxSessionsDep,
    session: AsyncDatabaseSessionDep,
    provider: Annotated[str, Query()] = "proxmox",
    object_ref: Annotated[str | None, Query()] = None,
    endpoint_id: Annotated[int | None, Query(gt=0)] = None,
) -> MetricsResponse:
    """Return the latest provider metrics for the requested scope.

    For ``provider=prometheus`` (and ``dashboard``) the configured endpoint is
    resolved from the DB and wired into the adapter; the result is parsed into a
    typed :class:`CephMetricSnapshot` where it matches that shape.
    """

    scope: dict[str, Any] = {"object_ref": object_ref} if object_ref else {}
    warnings: list[str] = []
    normalized_provider = provider.strip().lower().replace("-", "_")
    provider_log_label = (
        normalized_provider if normalized_provider in provider_names() else "unknown"
    )
    try:
        adapter = await build_adapter(
            provider,
            list(pxs),
            session,
            object_ref=object_ref,
        )
        metrics = redact_secrets(await adapter.metrics(scope))
    except CephProviderBoundaryError as exc:
        logger.warning(
            "Ceph v2 metrics unavailable provider=%s reason=%s correlation_id=%s",
            provider_log_label,
            exc.reason,
            exc.correlation_id,
        )
        metrics = {}
        warnings.append(f"provider_metrics_unavailable correlation_id={exc.correlation_id}")
    except Exception:  # noqa: BLE001 - never expose provider diagnostics
        failure = CephProviderBoundaryError(
            "provider_metrics_unavailable",
            "Ceph provider metrics could not be read safely.",
        )
        logger.warning(
            "Ceph v2 metrics unavailable provider=%s reason=%s correlation_id=%s",
            provider_log_label,
            failure.reason,
            failure.correlation_id,
        )
        metrics = {}
        warnings.append(f"provider_metrics_unavailable correlation_id={failure.correlation_id}")
    if not metrics and provider == "prometheus":
        warnings.append("no Prometheus source configured")
    if not metrics and provider == "dashboard":
        warnings.append("no Ceph Dashboard endpoint configured")
    if not metrics and provider == "external":
        warnings.append("no metrics provider configured for the external cluster")
    snapshot: CephMetricSnapshot | None = None
    if metrics:
        try:
            snapshot = CephMetricSnapshot.model_validate(metrics)
        except Exception:  # noqa: BLE001 - non-snapshot providers return free-form metrics
            snapshot = None
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

    @field_validator("credential_ref")
    @classmethod
    def _credential_ref_is_opaque(cls, value: str | None) -> str | None:
        return validate_credential_ref(value) if value is not None else None


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
        credential_ref=_safe_credential_ref(source.credential_ref),
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
    ok, _error = await validate_source(_source_to_config(source))
    return {
        "id": source_id,
        "ok": ok,
        "error": None if ok else "Prometheus source validation failed.",
    }


# --------------------------------------------------------------------------- #
# Ceph Dashboard endpoint registration (#98)
# --------------------------------------------------------------------------- #
class DashboardEndpointCreate(BaseModel):
    """Request body to register a direct Ceph Dashboard endpoint."""

    name: str = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)
    username: str | None = None
    password: str | None = None
    token: str | None = None
    credential_ref: str | None = None
    cluster_ref: str | None = None
    api_version: str = "1.0"
    verify_ssl: bool = True
    enabled: bool = True
    timeout_seconds: int = 30

    @field_validator("credential_ref")
    @classmethod
    def _credential_ref_is_opaque(cls, value: str | None) -> str | None:
        return validate_credential_ref(value) if value is not None else None


class DashboardEndpointOut(BaseModel):
    """Redacted Ceph Dashboard endpoint record (never exposes secrets)."""

    id: int
    name: str
    base_url: str
    username: str | None = None
    credential_ref: str | None = None
    cluster_ref: str | None = None
    api_version: str
    verify_ssl: bool
    enabled: bool
    timeout_seconds: int
    has_secret: bool


def _dashboard_out(endpoint: CephDashboardEndpoint) -> DashboardEndpointOut:
    return DashboardEndpointOut(
        id=endpoint.id or 0,
        name=endpoint.name,
        base_url=endpoint.base_url,
        username=endpoint.username,
        credential_ref=_safe_credential_ref(endpoint.credential_ref),
        cluster_ref=endpoint.cluster_ref,
        api_version=endpoint.api_version,
        verify_ssl=endpoint.verify_ssl,
        enabled=endpoint.enabled,
        timeout_seconds=endpoint.timeout_seconds,
        has_secret=bool(endpoint.password or endpoint.token),
    )


@router.get("/dashboard/endpoints", response_model=list[DashboardEndpointOut])
async def ceph_v2_list_dashboard_endpoints(
    session: AsyncDatabaseSessionDep,
) -> list[DashboardEndpointOut]:
    """List configured Ceph Dashboard endpoints (passwords/tokens redacted)."""

    rows = list((await session.exec(select(CephDashboardEndpoint))).all())
    return [_dashboard_out(row) for row in rows]


@router.post("/dashboard/endpoints", response_model=DashboardEndpointOut, status_code=201)
async def ceph_v2_create_dashboard_endpoint(
    payload: DashboardEndpointCreate,
    session: AsyncDatabaseSessionDep,
) -> DashboardEndpointOut:
    """Register a Ceph Dashboard endpoint. Password/token are encrypted at rest."""

    existing = (
        await session.exec(
            select(CephDashboardEndpoint).where(CephDashboardEndpoint.name == payload.name)
        )
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="An endpoint with that name already exists.")
    endpoint = CephDashboardEndpoint(
        name=payload.name,
        base_url=payload.base_url,
        username=payload.username,
        credential_ref=payload.credential_ref,
        cluster_ref=payload.cluster_ref,
        api_version=payload.api_version,
        verify_ssl=payload.verify_ssl,
        enabled=payload.enabled,
        timeout_seconds=payload.timeout_seconds,
    )
    endpoint.set_encrypted_password(payload.password)
    endpoint.set_encrypted_token(payload.token)
    session.add(endpoint)
    await session.commit()
    await session.refresh(endpoint)
    return _dashboard_out(endpoint)


@router.post("/dashboard/endpoints/{endpoint_id}/validate")
async def ceph_v2_validate_dashboard_endpoint(
    endpoint_id: int,
    session: AsyncDatabaseSessionDep,
) -> dict[str, Any]:
    """Probe a registered Ceph Dashboard endpoint (auth + capability detection)."""

    endpoint = await session.get(CephDashboardEndpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Ceph Dashboard endpoint not found.")
    ok, _error = await validate_dashboard_endpoint(_dashboard_to_config(endpoint))
    return {
        "id": endpoint_id,
        "ok": ok,
        "error": None if ok else "Ceph Dashboard endpoint validation failed.",
    }


# --------------------------------------------------------------------------- #
# External (non-Proxmox) Ceph cluster registration (#97)
# --------------------------------------------------------------------------- #
class ExternalClusterCreate(BaseModel):
    """Request body to register an external (non-Proxmox) Ceph cluster."""

    name: str = Field(..., min_length=1)
    cluster_ref: str | None = None
    ceph_version_hint: str | None = None
    dashboard_endpoint_id: int | None = None
    prometheus_source_id: int | None = None
    rgw_admin_url: str | None = None
    rgw_access_key: str | None = None
    rgw_secret_key: str | None = None
    ssh_host: str | None = None
    ssh_user: str | None = None
    ssh_credential_ref: str | None = None
    verify_ssl: bool = True
    enabled: bool = True

    @field_validator("ssh_credential_ref")
    @classmethod
    def _credential_ref_is_opaque(cls, value: str | None) -> str | None:
        return validate_credential_ref(value) if value is not None else None


class ExternalClusterOut(BaseModel):
    """Redacted external cluster record (never exposes RGW secret keys)."""

    id: int
    name: str
    cluster_ref: str | None = None
    ceph_version_hint: str | None = None
    dashboard_endpoint_id: int | None = None
    prometheus_source_id: int | None = None
    rgw_admin_url: str | None = None
    has_rgw_credentials: bool
    ssh_host: str | None = None
    ssh_user: str | None = None
    verify_ssl: bool
    enabled: bool


def _external_out(cluster: CephExternalCluster) -> ExternalClusterOut:
    return ExternalClusterOut(
        id=cluster.id or 0,
        name=cluster.name,
        cluster_ref=cluster.cluster_ref,
        ceph_version_hint=cluster.ceph_version_hint,
        dashboard_endpoint_id=cluster.dashboard_endpoint_id,
        prometheus_source_id=cluster.prometheus_source_id,
        rgw_admin_url=cluster.rgw_admin_url,
        has_rgw_credentials=bool(cluster.rgw_access_key and cluster.rgw_secret_key),
        ssh_host=cluster.ssh_host,
        ssh_user=cluster.ssh_user,
        verify_ssl=cluster.verify_ssl,
        enabled=cluster.enabled,
    )


@router.get("/external/clusters", response_model=list[ExternalClusterOut])
async def ceph_v2_list_external_clusters(
    session: AsyncDatabaseSessionDep,
) -> list[ExternalClusterOut]:
    """List configured external Ceph clusters (RGW secret keys redacted)."""

    rows = list((await session.exec(select(CephExternalCluster))).all())
    return [_external_out(row) for row in rows]


@router.post("/external/clusters", response_model=ExternalClusterOut, status_code=201)
async def ceph_v2_create_external_cluster(
    payload: ExternalClusterCreate,
    session: AsyncDatabaseSessionDep,
) -> ExternalClusterOut:
    """Register an external Ceph cluster. RGW secret keys are encrypted at rest."""

    existing = (
        await session.exec(
            select(CephExternalCluster).where(CephExternalCluster.name == payload.name)
        )
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="A cluster with that name already exists.")
    cluster = CephExternalCluster(
        name=payload.name,
        cluster_ref=payload.cluster_ref,
        ceph_version_hint=payload.ceph_version_hint,
        dashboard_endpoint_id=payload.dashboard_endpoint_id,
        prometheus_source_id=payload.prometheus_source_id,
        rgw_admin_url=payload.rgw_admin_url,
        ssh_host=payload.ssh_host,
        ssh_user=payload.ssh_user,
        ssh_credential_ref=payload.ssh_credential_ref,
        verify_ssl=payload.verify_ssl,
        enabled=payload.enabled,
    )
    cluster.set_encrypted_rgw_access_key(payload.rgw_access_key)
    cluster.set_encrypted_rgw_secret_key(payload.rgw_secret_key)
    session.add(cluster)
    await session.commit()
    await session.refresh(cluster)
    return _external_out(cluster)


@router.post("/external/clusters/{cluster_id}/capabilities", response_model=CapabilitiesResponse)
async def ceph_v2_external_cluster_capabilities(
    cluster_id: int,
    pxs: ProxmoxSessionsDep,
    session: AsyncDatabaseSessionDep,
) -> CapabilitiesResponse:
    """Detect capabilities for an external cluster from its configured providers."""

    cluster = await session.get(CephExternalCluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail="External Ceph cluster not found.")
    adapter = await _external_adapter(cluster, list(pxs), session)
    return CapabilitiesResponse(providers=[await adapter.capabilities()])
