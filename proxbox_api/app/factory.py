"""FastAPI application factory: middleware, routers, OpenAPI, and lifespan."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from proxbox_api.app import bootstrap
from proxbox_api.app.cache_routes import register_cache_routes
from proxbox_api.app.cors import build_cors_origins
from proxbox_api.app.exceptions import register_exception_handlers
from proxbox_api.app.full_update import register_full_update_routes
from proxbox_api.app.root_meta import root_meta_router
from proxbox_api.app.websockets import register_websocket_routes
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.openapi_custom import custom_openapi_builder
from proxbox_api.routes.admin import router as admin_router
from proxbox_api.routes.dcim import router as dcim_router
from proxbox_api.routes.extras import router as extras_router
from proxbox_api.routes.netbox import router as netbox_router
from proxbox_api.routes.proxmox import router as proxmox_router
from proxbox_api.routes.proxmox.cluster import router as px_cluster_router
from proxbox_api.routes.proxmox.nodes import router as px_nodes_router
from proxbox_api.routes.proxmox.runtime_generated import register_generated_proxmox_routes
from proxbox_api.routes.virtualization import router as virtualization_router
from proxbox_api.routes.virtualization.virtual_machines import router as virtual_machines_router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# Legacy module-level placeholders (some tooling may read these names).
configuration = None
default_config: dict = {}
plugin_configuration: dict = {}
proxbox_cfg: dict = {}
PROXBOX_PLUGIN_NAME: str = "netbox_proxbox"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        register_generated_proxmox_routes(app)
    except ProxboxException as error:
        logger.warning(
            "Generated Proxmox proxy routes were not mounted: %s",
            error.message,
            extra={"detail": error.detail},
        )
        strict = os.environ.get("PROXBOX_STRICT_STARTUP", "").lower() in ("1", "true", "yes")
        if strict:
            raise
    yield


def create_app() -> FastAPI:
    """Build and configure the Proxbox FastAPI application."""
    bootstrap.init_database_and_netbox()

    app = FastAPI(
        title="Proxbox Backend",
        description="## Proxbox Backend made in FastAPI framework",
        version="0.0.1",
        lifespan=_lifespan,
    )

    def custom_openapi():
        return custom_openapi_builder(app)

    app.openapi = custom_openapi

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    static_dir = os.path.join(base_dir, "static")
    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    else:
        logger.info("Static asset directory not found; skipping /static mount", extra={"path": static_dir})

    origins = build_cors_origins(bootstrap.netbox_endpoints)
    logger.info("CORS allow_origins configured (%d entries)", len(origins))

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    app.include_router(root_meta_router)
    register_cache_routes(app)
    register_full_update_routes(app)
    register_websocket_routes(app)

    app.include_router(admin_router, prefix="/admin", tags=["admin"])
    app.include_router(netbox_router, prefix="/netbox", tags=["netbox"])
    app.include_router(px_nodes_router, prefix="/proxmox/nodes", tags=["proxmox / nodes"])
    app.include_router(px_cluster_router, prefix="/proxmox/cluster", tags=["proxmox / cluster"])
    app.include_router(proxmox_router, prefix="/proxmox", tags=["proxmox"])
    app.include_router(dcim_router, prefix="/dcim", tags=["dcim"])
    app.include_router(virtualization_router, prefix="/virtualization", tags=["virtualization"])
    app.include_router(
        virtual_machines_router,
        prefix="/virtualization/virtual-machines",
        tags=["virtualization / virtual-machines"],
    )
    app.include_router(extras_router, prefix="/extras", tags=["extras"])

    return app
