"""Root metadata and backend version endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from proxbox_api import __version__

root_meta_router = APIRouter()


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
