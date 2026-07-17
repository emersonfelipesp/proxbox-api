"""Extras route handlers for NetBox custom field management."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from proxbox_api.services.custom_fields import (
    CUSTOM_FIELD_INVENTORY as CUSTOM_FIELD_INVENTORY,
)
from proxbox_api.services.custom_fields import (
    _coerce_object_type_entry as _coerce_object_type_entry,
)
from proxbox_api.services.custom_fields import (
    _normalize_current_object_types as _normalize_current_object_types,
)
from proxbox_api.services.custom_fields import (
    _union_object_types_with_current as _union_object_types_with_current,
)
from proxbox_api.services.custom_fields import (
    reconcile_custom_fields,
)
from proxbox_api.services.netbox_bootstrap import BootstrapStatus
from proxbox_api.session.netbox import NetBoxAsyncSessionDep

router = APIRouter()


@router.get(
    "/extras/custom-fields/create",
    response_model=list[dict[str, object]],
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def create_custom_fields(
    netbox_session: NetBoxAsyncSessionDep,
) -> list[dict[str, object]]:
    """Create custom fields in NetBox for Proxmox synchronization."""
    return await reconcile_custom_fields(netbox_session)


@router.post(
    "/custom-fields/reconcile",
    response_model=list[dict[str, object]],
    response_model_exclude_none=True,
    response_model_exclude_unset=True,
)
async def force_reconcile_custom_fields(
    netbox_session: NetBoxAsyncSessionDep,
) -> list[dict[str, object]]:
    """Force a live NetBox custom-field reconcile, bypassing the cache."""
    return await reconcile_custom_fields(netbox_session, force=True)


@router.get("/bootstrap-status", response_model=dict[str, object])
async def get_netbox_bootstrap_status(request: Request) -> dict[str, object]:
    """Return the last NetBox bootstrap status recorded by application startup."""
    status = getattr(request.app.state, "bootstrap_status", None)
    if isinstance(status, BootstrapStatus):
        return status.as_dict()
    return BootstrapStatus(
        ok=False,
        skipped=True,
        reason="bootstrap_not_run",
    ).as_dict()


CreateCustomFieldsDep = Annotated[list[dict[str, object]], Depends(create_custom_fields)]
