"""Utilities to read generated Proxmox OpenAPI artifacts for mapping contracts."""

from __future__ import annotations

import json
from pathlib import Path

from proxbox_api.logger import logger

DEFAULT_PROXMOX_OPENAPI_TAG = "latest"
RUNTIME_GENERATED_ROUTE_CACHE_FILENAME = "runtime_generated_routes_cache.json"


def proxmox_generated_openapi_path(
    version_tag: str = DEFAULT_PROXMOX_OPENAPI_TAG,
) -> Path:
    """Return canonical generated Proxmox OpenAPI artifact path for version tag."""

    return (
        Path(__file__).resolve().parents[1] / "generated" / "proxmox" / version_tag / "openapi.json"
    )


def proxmox_generated_openapi_root() -> Path:
    """Return the directory containing generated Proxmox OpenAPI artifacts."""

    return Path(__file__).resolve().parents[1] / "generated" / "proxmox"


def proxmox_generated_route_cache_path() -> Path:
    """Return the cache manifest path for runtime-generated Proxmox routes."""

    return proxmox_generated_openapi_root() / RUNTIME_GENERATED_ROUTE_CACHE_FILENAME


def available_proxmox_openapi_versions() -> list[str]:
    """List generated Proxmox version tags that have an embedded OpenAPI artifact."""

    root = proxmox_generated_openapi_root()
    if not root.exists():
        return []

    versions: list[str] = []
    for child in sorted(root.iterdir(), key=lambda entry: entry.name):
        if not child.is_dir():
            continue
        if child.name.startswith("__"):
            continue
        if (child / "openapi.json").exists():
            versions.append(child.name)
    return versions


def load_proxmox_generated_openapi(
    version_tag: str = DEFAULT_PROXMOX_OPENAPI_TAG,
) -> dict[str, object]:
    """Load generated Proxmox OpenAPI document for version tag if available."""

    path = proxmox_generated_openapi_path(version_tag=version_tag)
    if not path.exists():
        logger.warning("Generated Proxmox OpenAPI artifact not found at %s", path)
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        logger.warning("Failed to load generated Proxmox OpenAPI from %s: %s", path, error)
        return {}


def proxmox_operation_schema(
    path: str,
    method: str,
    version_tag: str = DEFAULT_PROXMOX_OPENAPI_TAG,
    openapi: dict[str, object] | None = None,
) -> dict[str, object] | None:
    """Get operation schema from generated Proxmox OpenAPI by path and method."""

    document = openapi or load_proxmox_generated_openapi(version_tag=version_tag)
    paths = document.get("paths", {}) if isinstance(document, dict) else {}
    item = paths.get(path)
    if not isinstance(item, dict):
        return None
    operation = item.get(method.lower())
    if not isinstance(operation, dict):
        return None
    return operation
