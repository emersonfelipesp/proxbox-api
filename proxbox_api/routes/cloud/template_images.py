"""Cloud image template build route."""

from __future__ import annotations

import asyncio
from pathlib import PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import CloudImageBuildOperation, ProxmoxEndpoint
from proxbox_api.logger import logger
from proxbox_api.routes.cloud.cloud_init_templates import (
    find_product_version,
    generate_cloud_init_userdata,
    generate_firecracker_userdata,
)
from proxbox_api.routes.cloud.display import qemu_display_create_kwargs
from proxbox_api.routes.cloud.pipeline_scripts import (
    PipelineExecutionCancelled,
    build_pipeline_response,
    cancel_pipeline_operation,
    execute_pipeline_response,
    pipeline_execution_contract,
)
from proxbox_api.routes.cloud.provision import _extract_task_id, _wait_for_upid
from proxbox_api.routes.proxmox.access_gate import gate_ssh_access
from proxbox_api.routes.proxmox_actions import _gate, _open_proxmox_session
from proxbox_api.schemas.cloud_provision import (
    CloudImageBuildOperationResponse,
    CloudImageBuildProvider,
    CloudImageSSHExecutionTarget,
    CloudImageTemplateBuildRequest,
    CloudImageTemplateBuildResponse,
    CloudImageTemplateExecutionSummary,
    CloudImageTemplatePreflightRequest,
    CloudImageTemplatePreflightResponse,
    PackerFinding,
    PackerFindingSeverity,
    ProxmoxProductType,
)
from proxbox_api.services.packer_plans import (
    PackerPlanError,
    acquire_operation_lease,
    finish_operation,
    issue_packer_plan,
    mark_operation_running,
    operation_response,
    record_cancel_request,
    verify_packer_plan,
)
from proxbox_api.services.packer_preflight import run_packer_preflight
from proxbox_api.session.proxmox import ProxmoxSession
from proxbox_api.session.proxmox_providers import (
    load_proxmox_session_schemas,
    proxmox_session_schema_from_endpoint,
)
from proxbox_api.ssrf import validate_endpoint_url
from proxbox_api.utils.async_compat import maybe_await as _maybe_await
from proxbox_api.utils.cancellation import await_task_through_repeated_cancellation

router = APIRouter()

_IMAGE_EXTENSIONS = {".qcow2", ".raw", ".vmdk", ".vma"}
_PIPELINE_PRODUCT_TYPES = frozenset(
    {ProxmoxProductType.pve, ProxmoxProductType.pfsense, ProxmoxProductType.opnsense}
)


def _filename_from_request(req: CloudImageTemplateBuildRequest) -> str:
    raw = (req.image_filename or "").strip()
    if not raw:
        raw = PurePosixPath(urlsplit(req.image_url or "").path).name
    if not raw:
        raise HTTPException(status_code=422, detail="image_filename could not be derived.")
    path = PurePosixPath(raw)
    suffix = path.suffix.lower()
    if suffix == ".img":
        return f"{path.stem}.qcow2"
    if suffix not in _IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"image_filename must end with one of {sorted(_IMAGE_EXTENSIONS)}.",
        )
    return path.name


def _is_ready_template(config: object) -> bool:
    if not isinstance(config, dict):
        return False
    values = cast(dict[str, object], config)
    return (
        str(values.get("template")) in {"1", "True", "true"}
        and "scsi0" in values
        and "cloudinit" in str(values.get("ide2", "")).lower()
        and "scsi0" in str(values.get("boot", ""))
    )


async def _vm_config_or_none(
    proxmox: ProxmoxSession,
    *,
    node: str,
    vmid: int,
) -> dict[str, Any] | None:
    sdk_session = proxmox.session
    if sdk_session is None:
        return None
    try:
        config = await _maybe_await(sdk_session.nodes(node).qemu(vmid).config.get())
    except Exception:  # noqa: BLE001
        return None
    return cast(dict[str, Any], config) if isinstance(config, dict) else {}


async def _verify_pipeline_artifact(
    proxmox: ProxmoxSession,
    *,
    node: str,
    vmid: int,
    provider: CloudImageBuildProvider,
) -> tuple[bool, bool]:
    """Return ``(verified, possible_partial_state)`` from a final API read."""

    config = await _vm_config_or_none(proxmox, node=node, vmid=vmid)
    if config is None:
        # The API boundary deliberately hides whether a lookup failed or the VM
        # is absent. After an attempted write, either outcome needs inspection.
        return False, True
    if provider == CloudImageBuildProvider.proxmox_iso:
        ide2 = str(config.get("ide2") or "")
        verified = bool(config.get("scsi0") and ":iso/" in ide2 and "media=cdrom" in ide2)
        return verified, not verified
    verified = _is_ready_template(config)
    return verified, not verified


def _plan_error(error: PackerPlanError) -> HTTPException:
    messages = {
        "preflight_plan_invalid": "The signed preflight plan is invalid.",
        "preflight_plan_expired": "The signed preflight plan has expired.",
        "preflight_plan_mismatch": "The signed preflight plan does not match this build.",
        "preflight_plan_already_consumed": "The signed preflight plan was already consumed.",
        "build_target_leased": "Another operation owns the endpoint and VMID lease.",
        "build_target_recovery_required": (
            "The endpoint and VMID remain blocked pending explicit recovery."
        ),
    }
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": error.code,
            "message": messages.get(error.code, "The signed preflight plan cannot be used."),
        },
    )


async def _close_proxmox_session(proxmox: ProxmoxSession, *, context: str) -> None:
    """Complete one close through repeated request cancellation."""

    close_task = asyncio.create_task(proxmox.aclose())
    try:
        await await_task_through_repeated_cancellation(close_task)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # noqa: BLE001 - cleanup must not replace route results
        logger.warning(
            "%s session cleanup failed error_type=%s",
            context,
            type(error).__name__,
        )


async def _finish_operation_durably(
    session: SessionDep,
    operation: CloudImageBuildOperation,
    *,
    state: Literal["completed", "failed", "cancelled", "recovery_required"],
    execution: CloudImageTemplateExecutionSummary,
    verified: bool,
    recovery_required: bool,
    error_code: str | None,
) -> bool:
    """Persist one terminal transition despite repeated caller cancellation."""

    task = asyncio.create_task(
        finish_operation(
            session,
            operation,
            state=state,
            execution=execution,
            verified=verified,
            recovery_required=recovery_required,
            error_code=error_code,
        )
    )
    return await await_task_through_repeated_cancellation(task)


async def _record_cancel_request_durably(
    session: SessionDep,
    operation: CloudImageBuildOperation,
    *,
    cancellation_succeeded: bool,
) -> bool:
    task = asyncio.create_task(
        record_cancel_request(
            session,
            operation,
            cancellation_succeeded=cancellation_succeeded,
        )
    )
    return await await_task_through_repeated_cancellation(task)


async def _image_exists(
    proxmox: ProxmoxSession,
    *,
    node: str,
    storage: str,
    volid: str,
) -> bool:
    sdk_session = proxmox.session
    if sdk_session is None:
        return False
    try:
        content = await _maybe_await(
            sdk_session.nodes(node).storage(storage).content.get(content="import")
        )
    except Exception:  # noqa: BLE001
        return False
    rows = content if isinstance(content, list) else []
    return any(
        isinstance(row, dict) and cast(dict[str, object], row).get("volid") == volid for row in rows
    )


async def _wait_for_task(
    proxmox: ProxmoxSession,
    *,
    node: str,
    response: object,
) -> str | None:
    upid = _extract_task_id(response)
    if upid and upid.startswith("UPID:"):
        await _wait_for_upid(proxmox, node, upid)
    return upid


async def _refresh_endpoint_snapshot(
    session: SessionDep,
    endpoint_id: int,
) -> ProxmoxEndpoint:
    """Force an authoritative read and return detached endpoint authority."""

    endpoint = await _maybe_await(session.get(ProxmoxEndpoint, endpoint_id))
    if not isinstance(endpoint, ProxmoxEndpoint):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "endpoint_not_found",
                "endpoint_id": endpoint_id,
                "message": "The persisted Proxmox endpoint does not exist.",
            },
        )
    # ``session.get`` may return an identity-map object populated before a
    # concurrent endpoint edit. Force a database round trip, then detach the
    # authority used by both the Proxmox API and SSH execution paths.
    await _maybe_await(session.refresh(endpoint))
    endpoint_snapshot = ProxmoxEndpoint.model_validate(endpoint.model_dump())
    if not endpoint_snapshot.enabled:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "endpoint_disabled",
                "endpoint_id": endpoint_id,
                "message": "The persisted Proxmox endpoint is disabled.",
            },
        )
    return endpoint_snapshot


async def _resolve_preflight_target(
    session: SessionDep,
    endpoint_id: int,
) -> tuple[ProxmoxEndpoint, ProxmoxSession]:
    """Resolve exactly one enabled, database-backed session for ``endpoint_id``."""

    endpoint_snapshot = await _refresh_endpoint_snapshot(session, endpoint_id)

    try:
        schemas = await load_proxmox_session_schemas(
            database_session=session,
            source="database",
            endpoint_ids=[endpoint_id],
        )
    except Exception:  # noqa: BLE001 - never expose endpoint credentials upstream
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "endpoint_session_unavailable",
                "endpoint_id": endpoint_id,
                "message": "The selected Proxmox endpoint session is unavailable.",
            },
        ) from None
    matches = [schema for schema in schemas if schema.db_endpoint_id == endpoint_id]
    if len(matches) != 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": (
                    "endpoint_session_missing" if not matches else "endpoint_session_ambiguous"
                ),
                "endpoint_id": endpoint_id,
                "message": "Exactly one enabled Proxmox session must match endpoint_id.",
            },
        )
    # The loader check preserves the exact-one database-session invariant, but
    # its rows may share this SQLAlchemy identity map. Build credentials from
    # the same explicitly refreshed snapshot used for plan and SSH validation.
    schema = proxmox_session_schema_from_endpoint(endpoint_snapshot)
    try:
        proxmox = await ProxmoxSession.create(schema, initialize_metadata=False)
    except Exception:  # noqa: BLE001 - never expose endpoint credentials upstream
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "endpoint_session_unavailable",
                "endpoint_id": endpoint_id,
                "message": "The selected Proxmox endpoint session is unavailable.",
            },
        ) from None
    return endpoint_snapshot, proxmox


def _resolve_execution_ssh_target(
    endpoint: ProxmoxEndpoint,
    request: CloudImageTemplateBuildRequest,
) -> CloudImageSSHExecutionTarget:
    """Derive one executable SSH target exclusively from persisted authority."""

    endpoint_id = int(endpoint.id or 0)
    if not endpoint.enabled:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "endpoint_disabled",
                "endpoint_id": endpoint_id,
                "message": "The persisted Proxmox endpoint is disabled.",
            },
        )
    if not endpoint.allow_writes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "endpoint_writes_disabled",
                "endpoint_id": endpoint_id,
                "message": "The persisted Proxmox endpoint does not allow writes.",
            },
        )
    if not endpoint.ssh_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "endpoint_ssh_disabled",
                "endpoint_id": endpoint_id,
                "message": "The persisted Proxmox endpoint does not allow SSH execution.",
            },
        )
    if not request.target_node:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "target_node_required",
                "endpoint_id": endpoint_id,
                "message": "target_node is required for executable builds.",
            },
        )
    if not endpoint.has_cloud_image_ssh_binding:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "endpoint_ssh_binding_incomplete",
                "endpoint_id": endpoint_id,
                "message": "The endpoint has no complete persisted Cloud Image SSH binding.",
            },
        )
    if request.target_node != endpoint.ssh_target_node:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "endpoint_node_mismatch",
                "endpoint_id": endpoint_id,
                "message": "target_node does not match the endpoint's persisted SSH node.",
            },
        )

    try:
        target = CloudImageSSHExecutionTarget(
            host=str(endpoint.ssh_host),
            user=str(endpoint.ssh_username),
            port=endpoint.ssh_port,
            identity_file=str(endpoint.ssh_identity_file),
            known_host_fingerprint=str(endpoint.ssh_known_host_fingerprint),
        )
    except Exception:  # noqa: BLE001 - return only a stable, non-secret boundary error
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "endpoint_ssh_binding_invalid",
                "endpoint_id": endpoint_id,
                "message": "The endpoint's persisted Cloud Image SSH binding is invalid.",
            },
        ) from None

    assertions = {
        "ssh_host": target.host,
        "ssh_user": target.user,
        "ssh_port": target.port,
        "ssh_identity_file": target.identity_file,
        "ssh_known_host_fingerprint": target.known_host_fingerprint,
    }
    for field, expected in assertions.items():
        if field not in request.model_fields_set:
            continue
        if getattr(request, field) != expected:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "endpoint_ssh_binding_mismatch",
                    "endpoint_id": endpoint_id,
                    "field": field,
                    "message": "Caller SSH assertions do not match the persisted endpoint binding.",
                },
            )
    return target


@router.post(
    "/templates/images/preflight",
    response_model=CloudImageTemplatePreflightResponse,
    response_model_exclude_none=True,
)
async def preflight_cloud_image_template(
    req: CloudImageTemplatePreflightRequest,
    session: SessionDep,
) -> CloudImageTemplatePreflightResponse:
    """Validate one persisted Packer target using Proxmox GET requests only."""

    endpoint, proxmox = await _resolve_preflight_target(session, req.endpoint_id)
    try:
        response = await run_packer_preflight(
            req,
            proxmox,
            writes_enabled=endpoint.allow_writes,
        )
        if not response.ready or req.recipe_digest is None:
            return response
        claims, plan_digest, plan_token = issue_packer_plan(
            endpoint=endpoint,
            target=req.build_target(),
            recipe_digest=req.recipe_digest,
        )
        return response.model_copy(
            update={
                "plan_id": claims.plan_id,
                "plan_digest": plan_digest,
                "plan_token": plan_token,
                "expires_at": claims.expires_at,
            }
        )
    finally:
        await _close_proxmox_session(proxmox, context="Packer preflight")


@router.get(
    "/templates/images/operations/{operation_id}",
    response_model=CloudImageBuildOperationResponse,
    response_model_exclude_none=True,
)
async def get_cloud_image_build_operation(
    operation_id: str,
    session: SessionDep,
) -> CloudImageBuildOperationResponse:
    """Return the durable, secret-free state of one build operation."""

    operation = await session.get(CloudImageBuildOperation, operation_id)
    if operation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "cloud_image_operation_not_found",
                "message": "The Cloud Image Pipeline operation does not exist.",
            },
        )
    return operation_response(operation)


@router.post(
    "/templates/images/operations/{operation_id}/cancel",
    response_model=CloudImageBuildOperationResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_cloud_image_build_operation(
    operation_id: str,
    session: SessionDep,
) -> CloudImageBuildOperationResponse | JSONResponse:
    """Request cancellation of one running, endpoint-bound remote unit."""

    operation = await session.get(CloudImageBuildOperation, operation_id)
    if operation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "cloud_image_operation_not_found",
                "message": "The Cloud Image Pipeline operation does not exist.",
            },
        )
    if operation.state != "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "cloud_image_operation_not_running",
                "message": "Only a running Cloud Image Pipeline operation can be cancelled.",
            },
        )

    gated = await _gate(session, operation.endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated
    await gate_ssh_access(session, operation.endpoint_id)
    await _maybe_await(session.refresh(gated))
    endpoint_snapshot = ProxmoxEndpoint.model_validate(gated.model_dump())
    execution_target = _resolve_execution_ssh_target(
        endpoint_snapshot,
        CloudImageTemplateBuildRequest(
            endpoint_id=operation.endpoint_id,
            target_node=operation.target_node,
            vmid=operation.vmid,
            execute=True,
        ),
    )
    cancel_task = asyncio.create_task(
        cancel_pipeline_operation(
            execution_target,
            remote_unit=operation.remote_unit,
        )
    )
    cancellation_requested = False
    try:
        cancellation_succeeded = await await_task_through_repeated_cancellation(cancel_task)
    except asyncio.CancelledError:
        cancellation_requested = True
        cancellation_succeeded = cancel_task.result()
    try:
        await _record_cancel_request_durably(
            session,
            operation,
            cancellation_succeeded=cancellation_succeeded,
        )
    except asyncio.CancelledError:
        cancellation_requested = True
    if cancellation_requested:
        raise asyncio.CancelledError
    await session.refresh(operation)
    return operation_response(operation)


async def _execute_bound_pipeline(
    req: CloudImageTemplateBuildRequest,
    session: SessionDep,
    endpoint: ProxmoxEndpoint,
) -> CloudImageTemplateBuildResponse:
    """Revalidate one signed plan, lease it, execute it, and verify its artifact."""

    if not req.preflight_plan_token:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "preflight_plan_required",
                "message": "A signed preflight plan is required for execution.",
            },
        )
    target, recipe_digest = pipeline_execution_contract(req)
    resolved_endpoint, proxmox = await _resolve_preflight_target(session, int(endpoint.id or 0))
    operation: CloudImageBuildOperation | None = None
    try:
        # Verify exactly once against the refreshed snapshot used to construct
        # both API credentials and SSH authority. A stale object returned by
        # the earlier route gate cannot authorize execution.
        try:
            plan, plan_digest = verify_packer_plan(
                req.preflight_plan_token,
                endpoint=resolved_endpoint,
                target=target,
                recipe_digest=recipe_digest,
            )
        except PackerPlanError as error:
            raise _plan_error(error) from None

        preflight = await run_packer_preflight(
            CloudImageTemplatePreflightRequest(
                endpoint_id=plan.endpoint_id,
                target_node=plan.target_node,
                vmid=plan.vmid,
                provider=CloudImageBuildProvider(plan.provider),
                image_storage=plan.image_storage,
                vm_storage=plan.vm_storage,
                snippets_storage=plan.snippets_storage,
                recipe_digest=plan.recipe_digest,
            ),
            proxmox,
            writes_enabled=resolved_endpoint.allow_writes,
        )
        if not preflight.ready:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "preflight_no_longer_ready",
                    "message": "The target no longer passes the exact read-only preflight.",
                },
            )

        # Preflight can involve multiple remote reads. Re-read endpoint
        # authority after it completes and authenticate the plan again directly
        # before acquiring the target lease and starting an SSH child.
        execution_endpoint = await _refresh_endpoint_snapshot(session, plan.endpoint_id)
        try:
            plan, plan_digest = verify_packer_plan(
                req.preflight_plan_token,
                endpoint=execution_endpoint,
                target=target,
                recipe_digest=recipe_digest,
            )
        except PackerPlanError as error:
            raise _plan_error(error) from None
        execution_target = _resolve_execution_ssh_target(execution_endpoint, req)

        try:
            lease_task = asyncio.create_task(
                acquire_operation_lease(
                    session,
                    plan=plan,
                    plan_digest=plan_digest,
                )
            )
            try:
                operation = await await_task_through_repeated_cancellation(lease_task)
            except asyncio.CancelledError:
                if not lease_task.cancelled():
                    operation = lease_task.result()
                raise
        except PackerPlanError as error:
            raise _plan_error(error) from None
        running_task = asyncio.create_task(mark_operation_running(session, operation))
        await await_task_through_repeated_cancellation(running_task)

        try:
            response, execution_error = await execute_pipeline_response(
                req,
                execution_target=execution_target,
                operation_id=operation.id,
                remote_unit=operation.remote_unit,
            )
        except PipelineExecutionCancelled as cancelled:
            try:
                await _finish_operation_durably(
                    session,
                    operation,
                    state="recovery_required",
                    execution=cancelled.execution,
                    verified=False,
                    recovery_required=True,
                    error_code="execution_cancelled",
                )
            except asyncio.CancelledError:
                # The helper only re-raises after the journal task has reached
                # a terminal state, so preserve the original cancellation.
                pass
            raise asyncio.CancelledError from None
        except HTTPException as error:
            detail = error.detail if isinstance(error.detail, dict) else {}
            await _finish_operation_durably(
                session,
                operation,
                state="failed",
                execution=CloudImageTemplateExecutionSummary(enabled=False),
                verified=False,
                recovery_required=False,
                error_code=str(detail.get("code") or "execution_rejected"),
            )
            raise
        except Exception as error:  # noqa: BLE001 - never expose the execution boundary
            logger.warning(
                "Cloud Image Pipeline execution failed error_type=%s",
                type(error).__name__,
            )
            await _finish_operation_durably(
                session,
                operation,
                state="recovery_required",
                execution=CloudImageTemplateExecutionSummary(attempted=True, enabled=True),
                verified=False,
                recovery_required=True,
                error_code="execution_unavailable",
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "code": "execution_unavailable",
                    "message": "The remote Cloud Image Pipeline execution failed.",
                },
            ) from None

        await session.refresh(operation)
        verified, possible_partial = await _verify_pipeline_artifact(
            proxmox,
            node=target.target_node,
            vmid=target.vmid,
            provider=target.provider,
        )
        if (
            response.status == "verification_pending"
            and verified
            and not operation.cancel_requested
        ):
            completion_finding = PackerFinding(
                code="artifact_verified",
                severity=PackerFindingSeverity.info,
                target=f"vmid:{target.vmid}",
                message="The expected Proxmox artifact passed final API verification.",
            )
            response = response.model_copy(
                update={
                    "status": "completed",
                    "verified": True,
                    "diagnostics": [*response.diagnostics, completion_finding],
                }
            )
            transitioned = await _finish_operation_durably(
                session,
                operation,
                state="completed",
                execution=response.execution,
                verified=True,
                recovery_required=False,
                error_code=None,
            )
            if transitioned or operation.state == "completed":
                return response
            race_finding = PackerFinding(
                code="execution_cancel_requested",
                severity=PackerFindingSeverity.error,
                target=f"vmid:{target.vmid}",
                message=(
                    "Cancellation won the journal transition; preserve the artifact for recovery."
                ),
            )
            return response.model_copy(
                update={
                    "status": "recovery_required",
                    "verified": False,
                    "recovery_required": True,
                    "diagnostics": [*response.diagnostics, race_finding],
                }
            )

        recovery_required = possible_partial or response.execution.attempted
        error_code = (
            "execution_cancel_requested"
            if operation.cancel_requested
            else execution_error or "artifact_verification_failed"
        )
        failure_finding = PackerFinding(
            code=(
                "execution_cancel_requested"
                if operation.cancel_requested
                else "artifact_verification_failed"
            ),
            severity=PackerFindingSeverity.error,
            target=f"vmid:{target.vmid}",
            message=(
                "The operation was cancelled; preserve any partial artifact for recovery."
                if operation.cancel_requested
                else "The expected artifact was not verified; preserve it for operator recovery."
            ),
        )
        response = response.model_copy(
            update={
                "status": "recovery_required" if recovery_required else "failed",
                "verified": False,
                "recovery_required": recovery_required,
                "diagnostics": [*response.diagnostics, failure_finding],
            }
        )
        await _finish_operation_durably(
            session,
            operation,
            state="recovery_required" if recovery_required else "failed",
            execution=response.execution,
            verified=False,
            recovery_required=recovery_required,
            error_code=error_code,
        )
        return response
    except asyncio.CancelledError:
        if operation is not None and operation.state not in {
            "completed",
            "failed",
            "cancelled",
            "recovery_required",
        }:
            try:
                await _finish_operation_durably(
                    session,
                    operation,
                    state="recovery_required",
                    execution=CloudImageTemplateExecutionSummary(
                        attempted=operation.attempted,
                        enabled=operation.attempted,
                        cancellation_attempted=operation.attempted,
                    ),
                    verified=False,
                    recovery_required=True,
                    error_code="execution_cancelled",
                )
            except asyncio.CancelledError:
                pass
        raise
    finally:
        await _close_proxmox_session(proxmox, context="Cloud Image Pipeline")


@router.post(
    "/templates/images",
    response_model=CloudImageTemplateBuildResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
)
async def build_cloud_image_template(
    req: CloudImageTemplateBuildRequest,
    session: SessionDep,
) -> CloudImageTemplateBuildResponse | JSONResponse:
    """Create a bootable Proxmox template from a cloud image URL."""
    if (
        req.execute is not None
        or req.provider is not None
        or req.user_data_yaml is not None
        or req.product_type in _PIPELINE_PRODUCT_TYPES
    ):
        if req.execute:
            if req.endpoint_id is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="endpoint_id is required when execute=true.",
                )
            gated = await _gate(session, req.endpoint_id)
            if isinstance(gated, JSONResponse):
                return gated
            # Remote execution writes to a Proxmox host over SSH, so enforce both
            # the write trust gate and the orthogonal SSH-transport gate.
            await gate_ssh_access(session, req.endpoint_id)
            return await _execute_bound_pipeline(req, session, gated)
        return build_pipeline_response(req)

    if req.endpoint_id is None or not req.target_node or not req.image_url:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="endpoint_id, target_node, and image_url are required for direct Proxmox API builds.",
        )

    gated = await _gate(session, req.endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated

    filename = _filename_from_request(req)
    image_volid = f"{req.image_storage}:import/{filename}"

    safe, _reason = validate_endpoint_url(req.image_url)
    if not safe:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "image_url_rejected",
                "message": "The image URL was rejected by endpoint safety policy.",
            },
        )

    generated_userdata: str | None = None
    if req.product_type == ProxmoxProductType.firecracker:
        generated_userdata = generate_firecracker_userdata(
            os_family=req.debian_release if req.debian_release != "bookworm" else "debian",
            os_codename=req.debian_release,
        )
    elif req.product_type in {ProxmoxProductType.pbs, ProxmoxProductType.pdm}:
        pv = find_product_version(req.product_type, req.product_version)
        if pv is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "catalog_entry_not_found",
                    "message": "No catalog entry matches the requested product and version.",
                },
            )
        generated_userdata = generate_cloud_init_userdata(
            req.product_type,
            pv,
            install_qemu_guest_agent=req.install_qemu_guest_agent,
            install_zabbix_agent2=req.install_zabbix_agent2,
            zabbix_server=req.zabbix_server,
            search_domain=req.search_domain,
            nameservers=req.nameservers,
        )

    proxmox: ProxmoxSession | None = None
    try:
        proxmox = await _open_proxmox_session(gated)
        existing = await _vm_config_or_none(
            proxmox,
            node=req.target_node,
            vmid=req.vmid,
        )
        if existing is not None:
            if _is_ready_template(existing):
                return CloudImageTemplateBuildResponse(
                    endpoint_id=req.endpoint_id,
                    target_node=req.target_node,
                    vmid=req.vmid,
                    name=str(existing.get("name") or req.name),
                    status="already_exists",
                    image_volid=image_volid,
                    boot=existing.get("boot"),
                    scsi0=existing.get("scsi0"),
                    ide2=existing.get("ide2"),
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"VMID {req.vmid} already exists and is not a ready cloud-init template.",
            )

        sdk_session = proxmox.session
        if sdk_session is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "code": "proxmox_session_unavailable",
                    "message": "The Proxmox API session is unavailable.",
                },
            )
        download_upid = None
        if not await _image_exists(
            proxmox,
            node=req.target_node,
            storage=req.image_storage,
            volid=image_volid,
        ):
            download_result = await _maybe_await(
                sdk_session.nodes(req.target_node)
                .storage(f"{req.image_storage}/download-url")
                .post(
                    content="import",
                    filename=filename,
                    url=req.image_url,
                    **{"verify-certificates": 1 if req.verify_image_certificates else 0},
                )
            )
            download_upid = await _wait_for_task(
                proxmox,
                node=req.target_node,
                response=download_result,
            )

        create_kwargs: dict[str, object] = {
            "vmid": req.vmid,
            "name": req.name,
            "memory": req.memory_mb,
            "cores": req.cores,
            "sockets": 1,
            "ostype": req.os_type,
            "agent": "enabled=1",
            "scsihw": "virtio-scsi-pci",
            "scsi0": f"{req.vm_storage}:0,import-from={image_volid},discard=on",
            "ide2": f"{req.vm_storage}:cloudinit",
            "boot": "order=scsi0",
            "net0": f"virtio,bridge={req.bridge}",
            "ciuser": req.ciuser,
            "ipconfig0": "ip=dhcp",
        }
        create_kwargs.update(qemu_display_create_kwargs(req.product_type))
        if req.cpu:
            create_kwargs["cpu"] = req.cpu
        description_parts = [req.description] if req.description else []
        if generated_userdata:
            description_parts.append(f"cloud-init user-data:\n{generated_userdata}")
        if description_parts:
            create_kwargs["description"] = "\n\n".join(description_parts)
        create_result = await _maybe_await(
            sdk_session.nodes(req.target_node).qemu.post(**create_kwargs)
        )
        create_upid = await _wait_for_task(proxmox, node=req.target_node, response=create_result)

        template_result = await _maybe_await(
            sdk_session.nodes(req.target_node).qemu(req.vmid).template.post(disk="scsi0")
        )
        template_upid = await _wait_for_task(
            proxmox,
            node=req.target_node,
            response=template_result,
        )
        config = await _vm_config_or_none(proxmox, node=req.target_node, vmid=req.vmid)
        if not _is_ready_template(config):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Proxmox template was created but did not reach the expected config.",
            )
        config = config or {}
        return CloudImageTemplateBuildResponse(
            endpoint_id=req.endpoint_id,
            target_node=req.target_node,
            vmid=req.vmid,
            name=str(config.get("name") or req.name),
            status="created",
            image_volid=image_volid,
            download_upid=download_upid,
            create_upid=create_upid,
            template_upid=template_upid,
            boot=config.get("boot"),
            scsi0=config.get("scsi0"),
            ide2=config.get("ide2"),
        )
    except HTTPException:
        raise
    except Exception as error:  # noqa: BLE001 - normalize the secret-bearing SDK boundary
        logger.warning(
            "Direct Cloud Image Template build failed error_type=%s",
            type(error).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": "proxmox_build_failed",
                "endpoint_id": req.endpoint_id,
                "message": "The Proxmox image-template build failed.",
            },
        ) from None
    finally:
        if proxmox is not None:
            try:
                await proxmox.aclose()
            except Exception as error:  # noqa: BLE001 - cleanup must not mask the route result
                logger.warning(
                    "Direct Cloud Image Template session cleanup failed error_type=%s",
                    type(error).__name__,
                )
