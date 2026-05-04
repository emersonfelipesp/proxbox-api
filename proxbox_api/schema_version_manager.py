"""Auto-detection and background generation of Proxmox OpenAPI schemas."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

from proxbox_api.logger import logger
from proxbox_api.proxmox_to_netbox.proxmox_schema import (
    available_proxmox_sdk_versions,
    proxmox_generated_openapi_path,
)

if TYPE_CHECKING:
    from fastapi import FastAPI


# Background generation tracking
_generation_lock = threading.Lock()
_generation_tasks: dict[str, dict[str, object]] = {}
# Keys: version_tag strings
# Values: {"status": "pending"|"running"|"completed"|"failed", "error": str|None}


def extract_release_tag(version_info: dict | str | None) -> str | None:
    """Extract major.minor release tag from Proxmox version data.

    The version.get() call returns either:
    - A dict like {"version": "8.3.2", "release": "8.3", "repoid": "..."}
    - A string (some SDK versions)

    Returns the release string (e.g. "8.3") or None if it cannot be determined.
    """
    if version_info is None:
        return None
    if isinstance(version_info, dict):
        release = version_info.get("release")
        if release:
            return str(release)
        # Fallback: extract from "version" field
        version_str = str(version_info.get("version", ""))
        if version_str:
            parts = version_str.split(".")
            if len(parts) >= 2:
                return f"{parts[0]}.{parts[1]}"
        return None
    if isinstance(version_info, str):
        parts = version_info.split(".")
        if len(parts) >= 2:
            return f"{parts[0]}.{parts[1]}"
        return version_info
    return None


def has_schema_for_release(release_tag: str) -> bool:
    """Check whether a generated OpenAPI schema exists for this release tag."""
    path = proxmox_generated_openapi_path(version_tag=release_tag)
    return path.exists()


def get_generation_status(version_tag: str) -> dict[str, object] | None:
    """Return the current generation status for a version, or None if never requested."""
    with _generation_lock:
        entry = _generation_tasks.get(version_tag)
        return entry.copy() if entry else None


def get_all_generation_statuses() -> dict[str, dict[str, object]]:
    """Return all tracked generation statuses."""
    with _generation_lock:
        return {k: v.copy() for k, v in _generation_tasks.items()}


async def ensure_schema_for_version(
    app: FastAPI,
    version_info: dict | str | None,
) -> dict[str, object]:
    """Check whether a schema exists for the connected Proxmox version.

    If no schema is found, trigger background generation. Returns a status dict
    that can be included in API responses as a loading message.
    """
    release_tag = extract_release_tag(version_info)
    if release_tag is None:
        return {"status": "skipped", "reason": "could not determine Proxmox release version"}

    if has_schema_for_release(release_tag):
        return {"status": "available", "version_tag": release_tag}

    # Check whether generation is already in progress
    with _generation_lock:
        existing = _generation_tasks.get(release_tag)
        if existing and existing.get("status") in ("pending", "running"):
            return {
                "status": "generating",
                "version_tag": release_tag,
                "message": (
                    f"Schema generation for Proxmox {release_tag} is already in progress. "
                    "Routes will be registered automatically when complete."
                ),
            }

    # Start background generation
    _start_background_generation(app, release_tag)
    return {
        "status": "generating",
        "version_tag": release_tag,
        "message": (
            f"No bundled schema found for Proxmox {release_tag}. "
            "Background generation started. This may take several minutes."
        ),
    }


def _start_background_generation(app: FastAPI, version_tag: str) -> None:
    """Launch a background asyncio task to generate the schema and register routes."""
    with _generation_lock:
        _generation_tasks[version_tag] = {"status": "pending", "error": None}

    loop = asyncio.get_event_loop()
    loop.create_task(_generate_and_register(app, version_tag))
    logger.info(
        "Background schema generation scheduled for Proxmox version %s.",
        version_tag,
    )


async def _generate_and_register(app: FastAPI, version_tag: str) -> None:
    """Background coroutine: generate schema then register routes without app restart."""
    from proxbox_api.proxmox_codegen.pipeline import generate_proxmox_codegen_bundle_async
    from proxbox_api.routes.proxmox.runtime_generated import register_generated_proxmox_routes

    with _generation_lock:
        _generation_tasks[version_tag] = {"status": "running", "error": None}

    logger.info(
        "Schema generation STARTED for Proxmox %s. This may take several minutes...",
        version_tag,
    )

    try:
        from proxbox_api.proxmox_to_netbox.proxmox_schema import get_user_generated_dir

        output_dir = get_user_generated_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        await generate_proxmox_codegen_bundle_async(
            output_dir=output_dir,
            version_tag=version_tag,
        )

        logger.info(
            "Schema generation COMPLETED for Proxmox %s. Registering routes...",
            version_tag,
        )

        # Re-register all available versions (including the newly generated one)
        register_generated_proxmox_routes(app)

        with _generation_lock:
            _generation_tasks[version_tag] = {"status": "completed", "error": None}

        logger.info(
            "Proxmox schema for version %s is now available and routes are registered.",
            version_tag,
        )

    except Exception as error:
        error_msg = str(error)
        with _generation_lock:
            _generation_tasks[version_tag] = {"status": "failed", "error": error_msg}
        logger.error(
            "Schema generation FAILED for Proxmox %s: %s",
            version_tag,
            error_msg,
        )


def get_schema_summary() -> dict[str, object]:
    """Return a summary of available schemas and any active generation tasks."""
    available = available_proxmox_sdk_versions()
    return {
        "available_versions": available,
        "generation_tasks": get_all_generation_statuses(),
    }
