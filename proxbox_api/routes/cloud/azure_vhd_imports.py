"""Azure VHD import route."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.routes.cloud.azure_vhd_pipeline import build_azure_vhd_import_response
from proxbox_api.routes.proxmox.access_gate import gate_ssh_access
from proxbox_api.routes.proxmox_actions import _gate
from proxbox_api.schemas.cloud_image_security import (
    CloudImageSSHExecutionTarget,
    SSHBindingError,
    resolve_ssh_execution_target,
)
from proxbox_api.schemas.cloud_provision import AzureVhdImportRequest, AzureVhdImportResponse
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

router = APIRouter()


def _resolve_target(
    endpoint: ProxmoxEndpoint,
    request: AzureVhdImportRequest,
) -> CloudImageSSHExecutionTarget:
    try:
        return resolve_ssh_execution_target(endpoint, request)
    except SSHBindingError as error:
        detail = {
            "code": error.code,
            "endpoint_id": error.endpoint_id,
            "message": error.message,
        }
        if error.field is not None:
            detail["field"] = error.field
        raise HTTPException(status_code=error.status_code, detail=detail) from None


async def _refresh_endpoint(session: AsyncSession, endpoint_id: int) -> ProxmoxEndpoint:
    try:
        endpoint = await _maybe_await(session.get(ProxmoxEndpoint, endpoint_id))
        if not isinstance(endpoint, ProxmoxEndpoint):
            raise LookupError
        await _maybe_await(session.refresh(endpoint))
        return ProxmoxEndpoint.model_validate(endpoint.model_dump())
    except Exception:  # noqa: BLE001 - never expose database or credential details
        raise HTTPException(
            status_code=409,
            detail={
                "code": "endpoint_authority_refresh_failed",
                "endpoint_id": endpoint_id,
                "message": "The persisted endpoint authority could not be refreshed.",
            },
        ) from None


@router.post(
    "/azure/vhd-imports",
    response_model=AzureVhdImportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_azure_vhd(
    request: AzureVhdImportRequest,
    session: SessionDep,
) -> AzureVhdImportResponse | JSONResponse:
    """Plan or execute an Azure-exported VHD import into a Proxmox VM shell."""
    if request.execute:
        if request.endpoint_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="endpoint_id is required when execute=true.",
            )
        endpoint_id = request.endpoint_id
        gated = await _gate(
            session,
            endpoint_id,
            writes_disabled_reason="cloud_migration_writes_disabled",
        )
        if isinstance(gated, JSONResponse):
            return gated
        # The import runs over SSH, so also enforce the per-endpoint
        # SSH-transport gate (access_methods="api_ssh"); orthogonal to writes.
        endpoint = await gate_ssh_access(session, endpoint_id)
        execution_target = _resolve_target(endpoint, request)

        async def authorize_execution() -> None:
            refreshed = await _refresh_endpoint(session, endpoint_id)
            refreshed_target = _resolve_target(refreshed, request)
            if refreshed_target != execution_target:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "endpoint_configuration_changed",
                        "endpoint_id": endpoint_id,
                        "message": "The persisted SSH authority changed before execution.",
                    },
                )
    else:
        execution_target = None
        authorize_execution = None

    return await build_azure_vhd_import_response(
        request,
        execution_target=execution_target,
        authorize_execution=authorize_execution,
    )
