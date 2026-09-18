"""Root metadata, health check, and backend version endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request

from proxbox_api import __version__
from proxbox_api.app import bootstrap
from proxbox_api.services.interactive_policy import InteractiveStatus

root_meta_router = APIRouter()


@root_meta_router.get("/execution-policy", response_model=InteractiveStatus)
async def execution_policy(request: Request) -> InteractiveStatus:
    """Return local capability evidence without claiming fleet-wide cutover."""
    return InteractiveStatus.model_validate(request.app.state.interactive_runtime.status())


@root_meta_router.get("/")
async def standalone_info() -> dict:
    return {
        "message": "Proxbox Backend made in FastAPI framework",
        "proxbox": {
            "github": "https://github.com/netdevopsbr/netbox-proxbox",
            "docs": "https://docs.netbox.dev.br",
        },
        "fastapi": {
            "github": "https://github.com/tiangolo/fastapi",
            "website": "https://fastapi.tiangolo.com/",
            "reason": "FastAPI was chosen because of performance and reliability.",
        },
    }


@root_meta_router.get("/version")
async def backend_version() -> dict:
    """Return backend service version for external cache invalidation."""
    return {
        "version": __version__,
    }


@root_meta_router.get("/health")
async def health_check() -> dict:
    """Return backend health status for readiness checks."""
    return {
        "status": "ready" if bootstrap.init_ok else "initializing",
        "init_ok": bootstrap.init_ok,
    }
