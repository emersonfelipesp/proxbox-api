"""Utilities to read generated Proxmox OpenAPI artifacts for mapping contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def proxmox_generated_openapi_path() -> Path:
    """Return canonical generated Proxmox OpenAPI artifact path."""

    return (
        Path(__file__).resolve().parents[1] / "generated" / "proxmox" / "openapi.json"
    )


def load_proxmox_generated_openapi() -> dict[str, Any]:
    """Load generated Proxmox OpenAPI document if available."""

    path = proxmox_generated_openapi_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def proxmox_operation_schema(
    path: str,
    method: str,
    openapi: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Get operation schema from generated Proxmox OpenAPI by path and method."""

    document = openapi or load_proxmox_generated_openapi()
    paths = document.get("paths", {}) if isinstance(document, dict) else {}
    item = paths.get(path)
    if not isinstance(item, dict):
        return None
    operation = item.get(method.lower())
    if not isinstance(operation, dict):
        return None
    return operation
