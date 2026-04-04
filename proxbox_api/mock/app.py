"""Standalone FastAPI app for the schema-driven Proxmox mock API."""

from __future__ import annotations

import os

from fastapi import FastAPI

from proxbox_api import __version__
from proxbox_api.mock.routes import register_generated_proxmox_mock_routes
from proxbox_api.proxmox_to_netbox.proxmox_schema import DEFAULT_PROXMOX_OPENAPI_TAG


def create_mock_app() -> FastAPI:
    """Build the standalone Proxmox mock API app."""

    version_tag = os.environ.get("PROXMOX_MOCK_SCHEMA_VERSION", DEFAULT_PROXMOX_OPENAPI_TAG)

    app = FastAPI(
        title="Proxmox Mock API",
        description="Schema-driven in-memory FastAPI mock for the generated Proxmox API.",
        version=__version__,
    )

    @app.get("/")
    async def root() -> dict[str, object]:
        return {
            "message": "Schema-driven Proxmox mock API",
            "schema_version": version_tag,
            "package_version": __version__,
        }

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/version")
    async def version() -> dict[str, str]:
        return {"version": __version__}

    register_generated_proxmox_mock_routes(app, version_tag=version_tag)
    return app
