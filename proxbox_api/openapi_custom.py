"""FastAPI custom OpenAPI integration including generated Proxmox schema extension."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


def _generated_proxmox_openapi() -> dict[str, Any]:
    path = Path(__file__).resolve().parent / "generated" / "proxmox" / "openapi.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


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
        openapi_schema.setdefault("info", {})["x-proxmox-generated-openapi"] = {
            "source": "proxbox_api/generated/proxmox/openapi.json",
            "endpoint_count": len((proxmox_generated.get("paths") or {}).keys()),
            "version": proxmox_generated.get("info", {}).get("version"),
        }
        openapi_schema["x-proxmox-generated-openapi"] = proxmox_generated

    app.openapi_schema = openapi_schema
    return app.openapi_schema
