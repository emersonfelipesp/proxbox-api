"""Azure VHD import route."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.routes.cloud.azure_vhd_pipeline import build_azure_vhd_import_response
from proxbox_api.routes.proxmox.access_gate import gate_ssh_access
from proxbox_api.routes.proxmox_actions import _gate
from proxbox_api.schemas.cloud_provision import AzureVhdImportRequest, AzureVhdImportResponse

router = APIRouter()


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
        gated = await _gate(
            session,
            request.endpoint_id,
            writes_disabled_reason="cloud_migration_writes_disabled",
        )
        if isinstance(gated, JSONResponse):
            return gated
        # The import runs over SSH, so also enforce the per-endpoint
        # SSH-transport gate (access_methods="api_ssh"); orthogonal to writes.
        await gate_ssh_access(session, request.endpoint_id)

    return build_azure_vhd_import_response(request)
