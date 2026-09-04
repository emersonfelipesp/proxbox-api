"""Extras route handlers for NetBox bootstrap status."""

from __future__ import annotations

from fastapi import APIRouter, Request

from proxbox_api.services.netbox_bootstrap import BootstrapStatus

router = APIRouter()


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
