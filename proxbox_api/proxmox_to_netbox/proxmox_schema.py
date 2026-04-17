"""Utilities to read generated Proxmox OpenAPI artifacts for mapping contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path

from proxbox_api.logger import logger

DEFAULT_PROXMOX_OPENAPI_TAG = "latest"
RUNTIME_GENERATED_ROUTE_CACHE_FILENAME = "runtime_generated_routes_cache.json"


def get_user_generated_dir() -> Path:
    """Return the writable directory for runtime-generated Proxmox schemas.

    Priority: PROXBOX_GENERATED_DIR env var → XDG_DATA_HOME/proxbox/generated/proxmox
    → ~/.local/share/proxbox/generated/proxmox

    This path is used for schemas generated at runtime (e.g. via POST /proxmox/viewer/generate).
    Bundled schemas shipped with the package are under get_bundled_generated_dir().
    """
    env_dir = os.environ.get("PROXBOX_GENERATED_DIR")
    if env_dir:
        return Path(env_dir)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "proxbox" / "generated" / "proxmox"


def get_bundled_generated_dir() -> Path:
    """Return the read-only bundled schema directory shipped with the package."""
    return Path(__file__).resolve().parents[1] / "generated" / "proxmox"


def proxmox_generated_openapi_path(
    version_tag: str = DEFAULT_PROXMOX_OPENAPI_TAG,
) -> Path:
    """Return best path for the generated Proxmox OpenAPI artifact for version tag.

    Checks the user-writable location first (runtime-generated), then falls back
    to the bundled package location (pre-shipped schemas).
    """
    user_path = get_user_generated_dir() / version_tag / "openapi.json"
    if user_path.exists():
        return user_path
    return get_bundled_generated_dir() / version_tag / "openapi.json"


def proxmox_generated_openapi_root() -> Path:
    """Return the bundled directory containing pre-shipped Proxmox OpenAPI artifacts."""

    return get_bundled_generated_dir()


def proxmox_generated_route_cache_path() -> Path:
    """Return the cache manifest path for runtime-generated Proxmox routes.

    Written to the user-writable location so it works in read-only packaged installs.
    """
    return get_user_generated_dir() / RUNTIME_GENERATED_ROUTE_CACHE_FILENAME


def available_proxmox_sdk_versions() -> list[str]:
    """List generated Proxmox version tags that have an available OpenAPI artifact.

    Checks both the user-writable location (runtime-generated) and the bundled
    package location (pre-shipped). Merges results, deduplicating by version tag.
    """
    versions: set[str] = set()
    for root in (get_user_generated_dir(), get_bundled_generated_dir()):
        if not root.exists():
            continue
        for child in root.iterdir():
            if not child.is_dir():
                continue
            if child.name.startswith("__"):
                continue
            if (child / "openapi.json").exists():
                versions.add(child.name)
    return sorted(versions)


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


def best_matching_version(release_tag: str) -> str | None:
    """Find the best available bundled schema version for a given release tag.

    Checks exact match first, then falls back to highest same-major version,
    then to "latest". Returns None only when no schemas are available at all.
    """
    available = available_proxmox_sdk_versions()
    if not available:
        return None

    # Exact match
    if release_tag in available:
        return release_tag

    # Highest version sharing the same major number
    major = release_tag.split(".")[0] if "." in release_tag else release_tag
    candidates = sorted(
        (v for v in available if v != "latest" and v.split(".")[0] == major),
        reverse=True,
    )
    if candidates:
        return candidates[0]

    # Fall back to "latest"
    if "latest" in available:
        return "latest"

    return available[0]


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
