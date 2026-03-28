"""FastAPI custom OpenAPI integration including generated Proxmox schema extension."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from proxbox_api.proxmox_to_netbox.proxmox_schema import (
    DEFAULT_PROXMOX_OPENAPI_TAG,
    load_proxmox_generated_openapi,
    proxmox_generated_openapi_path,
)


def _generated_proxmox_openapi() -> dict[str, Any]:
    return load_proxmox_generated_openapi(version_tag=DEFAULT_PROXMOX_OPENAPI_TAG)


def custom_openapi_builder(app: FastAPI) -> dict[str, Any]:
    """Build and cache custom OpenAPI schema following FastAPI official override pattern."""

    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        summary="Proxbox API with embedded generated Proxmox OpenAPI contract",
        description=app.description,
        routes=app.routes,
    )

    proxmox_generated = _generated_proxmox_openapi()
    if proxmox_generated:
        source = str(
            proxmox_generated_openapi_path(
                version_tag=DEFAULT_PROXMOX_OPENAPI_TAG,
            ).relative_to(Path(__file__).resolve().parents[1])
        )
        openapi_schema.setdefault("info", {})["x-proxmox-generated-openapi"] = {
            "source": source,
            "endpoint_count": len((proxmox_generated.get("paths") or {}).keys()),
            "version": proxmox_generated.get("info", {}).get("version"),
            "version_tag": DEFAULT_PROXMOX_OPENAPI_TAG,
        }
        openapi_schema["x-proxmox-generated-openapi"] = proxmox_generated

    app.openapi_schema = openapi_schema
    return app.openapi_schema
