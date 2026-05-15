"""Runtime endpoints for Proxmox API Viewer code generation artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from proxbox_api.exception import ProxboxException
from proxbox_api.proxmox_codegen.apidoc_parser import PROXMOX_API_VIEWER_URL
from proxbox_api.proxmox_codegen.pipeline import generate_proxmox_codegen_bundle_async
from proxbox_api.proxmox_to_netbox.netbox_schema import netbox_openapi_schema_source
from proxbox_api.proxmox_to_netbox.proxmox_schema import (
    DEFAULT_PROXMOX_OPENAPI_TAG,
    get_bundled_generated_dir,
    get_user_generated_dir,
    load_proxmox_generated_openapi,
    proxmox_generated_openapi_path,
)
from proxbox_api.routes.proxmox.runtime_generated import (
    generated_proxmox_route_state,
    register_generated_proxmox_routes,
)
from proxbox_api.settings_client import get_settings
from proxbox_api.ssrf import validate_endpoint_url

router = APIRouter()


def _enforce_codegen_source_url(source_url: str) -> None:
    """Block codegen `source_url` values that resolve to internal/reserved hosts.

    Refuses the request before any DNS lookup is followed by Playwright or
    `urlopen`, preventing the codegen endpoint from being abused as an SSRF
    pivot toward cloud metadata services or RFC1918 hosts.
    """

    is_safe, reason = validate_endpoint_url(source_url, get_settings())
    if not is_safe:
        raise ProxboxException(
            message="Refusing codegen request: source_url is not allowed.",
            detail=reason,
        )


@router.post("/generate")
async def generate_viewer_codegen_artifacts(
    persist: bool = Query(
        default=True,
        description="Persist generated artifacts under proxbox_api/generated/proxmox.",
    ),
    workers: int = Query(
        default=10,
        ge=1,
        le=32,
        description="Async worker count for parallel endpoint capture.",
    ),
    retry_count: int = Query(
        default=2,
        ge=0,
        le=10,
        description="Retry attempts per endpoint for transient Playwright failures.",
    ),
    retry_backoff: float = Query(
        default=0.35,
        ge=0.0,
        le=5.0,
        description="Base exponential backoff seconds between retries.",
    ),
    checkpoint_every: int = Query(
        default=50,
        ge=1,
        le=500,
        description="Write crawl checkpoint after this many processed endpoints.",
    ),
    source_url: str = Query(
        default=PROXMOX_API_VIEWER_URL,
        description="Proxmox API viewer URL to crawl.",
    ),
    version_tag: str = Query(
        default=DEFAULT_PROXMOX_OPENAPI_TAG,
        description="Version tag used for generated artifacts subdirectory.",
    ),
):
    """Run Proxmox API Viewer to OpenAPI and Pydantic generation pipeline."""

    _enforce_codegen_source_url(source_url)
    try:
        output_dir = None
        if persist:
            output_dir = get_user_generated_dir()
            output_dir.mkdir(parents=True, exist_ok=True)
        bundle = await generate_proxmox_codegen_bundle_async(
            output_dir=output_dir,
            source_url=source_url,
            version_tag=version_tag,
            worker_count=workers,
            retry_count=retry_count,
            retry_backoff_seconds=retry_backoff,
            checkpoint_every=checkpoint_every,
        )
        viewer_capture = bundle.capture.get("viewer", {})
        completeness = bundle.capture.get("completeness", {})
        return {
            "message": "Generation completed",
            "source_url": bundle.source_url,
            "version_tag": bundle.version_tag,
            "generated_at": bundle.generated_at,
            "endpoint_count": bundle.endpoint_count,
            "operation_count": bundle.operation_count,
            "viewer": {
                "endpoint_count": viewer_capture.get("endpoint_count"),
                "navigation_items": viewer_capture.get("discovered_navigation_items"),
                "method_count": viewer_capture.get("method_count"),
                "duration_seconds": viewer_capture.get("duration_seconds"),
                "worker_count": viewer_capture.get("worker_count"),
                "failed_endpoint_count": viewer_capture.get("failed_endpoint_count"),
            },
            "completeness": {
                "fallback_method_count": completeness.get("fallback_method_count"),
                "missing_from_viewer": len(completeness.get("missing_from_viewer", [])),
            },
            "output_dir": (str(Path(output_dir) / bundle.version_tag) if output_dir else None),
            "retry": {
                "retry_count": retry_count,
                "retry_backoff": retry_backoff,
                "checkpoint_every": checkpoint_every,
            },
        }
    except Exception as error:
        raise ProxboxException(
            message="Failed to generate Proxmox codegen bundle.",
            python_exception=str(error),
        )


@router.get("/openapi")
async def proxmox_viewer_openapi(
    regenerate: bool = Query(
        default=False,
        description="Regenerate from upstream viewer before returning OpenAPI output.",
    ),
    workers: int = Query(
        default=10,
        ge=1,
        le=32,
        description="Async worker count used when regeneration is requested.",
    ),
    retry_count: int = Query(
        default=2,
        ge=0,
        le=10,
        description="Retry attempts per endpoint for transient Playwright failures.",
    ),
    retry_backoff: float = Query(
        default=0.35,
        ge=0.0,
        le=5.0,
        description="Base exponential backoff seconds between retries.",
    ),
    checkpoint_every: int = Query(
        default=50,
        ge=1,
        le=500,
        description="Write crawl checkpoint after this many processed endpoints.",
    ),
    source_url: str = Query(
        default=PROXMOX_API_VIEWER_URL,
        description="Proxmox API viewer URL used if regeneration is requested.",
    ),
    version_tag: str = Query(
        default=DEFAULT_PROXMOX_OPENAPI_TAG,
        description="Generated artifact version tag to load.",
    ),
):
    """Return generated OpenAPI schema for Proxmox API viewer endpoints."""

    try:
        output_dir = get_user_generated_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        openapi_path = proxmox_generated_openapi_path(version_tag=version_tag)
        if regenerate or not openapi_path.exists():
            _enforce_codegen_source_url(source_url)
            bundle = await generate_proxmox_codegen_bundle_async(
                output_dir=output_dir,
                source_url=source_url,
                version_tag=version_tag,
                worker_count=workers,
                retry_count=retry_count,
                retry_backoff_seconds=retry_backoff,
                checkpoint_every=checkpoint_every,
            )
            return bundle.openapi

        return json.loads(openapi_path.read_text(encoding="utf-8"))
    except Exception as error:
        raise ProxboxException(
            message="Failed to load generated OpenAPI schema.",
            python_exception=str(error),
        )


@router.get("/openapi/embedded")
async def proxmox_viewer_openapi_embedded(
    version_tag: str = Query(
        default=DEFAULT_PROXMOX_OPENAPI_TAG,
        description="Generated artifact version tag to load.",
    ),
):
    """Return generated Proxmox OpenAPI as consumed by custom FastAPI OpenAPI extension."""

    schema = load_proxmox_generated_openapi(version_tag=version_tag)
    if not schema:
        raise ProxboxException(
            message="Generated Proxmox OpenAPI schema not found.",
            detail="Run /proxmox/viewer/generate first.",
        )
    return schema


@router.get("/integration/contracts")
async def proxmox_netbox_integration_contracts():
    """Report Proxmox and NetBox schema contract sources for transformation workflows."""

    proxmox = load_proxmox_generated_openapi()
    return {
        "proxmox_generated_openapi_present": bool(proxmox),
        "proxmox_generated_path_count": len((proxmox.get("paths") or {}).keys()) if proxmox else 0,
        "netbox_schema_source": netbox_openapi_schema_source(),
    }


@router.post("/routes/refresh")
async def refresh_generated_proxmox_routes(
    version_tag: str | None = Query(
        default=None,
        description="Optional generated artifact version tag to rebuild. Omit to rebuild all available versions.",
    ),
):
    """Rebuild runtime-generated live Proxmox routes from the embedded OpenAPI contract."""

    from proxbox_api.main import app

    normalized_version_tag = version_tag if isinstance(version_tag, str) else None
    result = register_generated_proxmox_routes(app, version_tag=normalized_version_tag)
    result["state"] = generated_proxmox_route_state()
    return result


@router.get("/schema-status")
async def schema_generation_status(
    version_tag: str | None = Query(
        default=None,
        description="Specific version tag to check. Omit to see all available versions.",
    ),
):
    """Report schema availability and any active background generation status.

    Returns which bundled Proxmox OpenAPI schemas are available and whether
    any background generation tasks are in progress.
    """
    from proxbox_api.proxmox_to_netbox.proxmox_schema import available_proxmox_sdk_versions
    from proxbox_api.schema_version_manager import (
        get_all_generation_statuses,
        get_generation_status,
        has_schema_for_release,
    )

    available = available_proxmox_sdk_versions()

    if version_tag is not None:
        gen_status = get_generation_status(version_tag)
        return {
            "version_tag": version_tag,
            "schema_available": has_schema_for_release(version_tag),
            "generation": gen_status,
        }

    return {
        "available_versions": available,
        "generation_tasks": get_all_generation_statuses(),
    }


@router.get("/pydantic", response_class=PlainTextResponse)
async def proxmox_viewer_pydantic_models(
    regenerate: bool = Query(
        default=False,
        description="Regenerate from upstream viewer before returning model source.",
    ),
    workers: int = Query(
        default=10,
        ge=1,
        le=32,
        description="Async worker count used when regeneration is requested.",
    ),
    retry_count: int = Query(
        default=2,
        ge=0,
        le=10,
        description="Retry attempts per endpoint for transient Playwright failures.",
    ),
    retry_backoff: float = Query(
        default=0.35,
        ge=0.0,
        le=5.0,
        description="Base exponential backoff seconds between retries.",
    ),
    checkpoint_every: int = Query(
        default=50,
        ge=1,
        le=500,
        description="Write crawl checkpoint after this many processed endpoints.",
    ),
    source_url: str = Query(
        default=PROXMOX_API_VIEWER_URL,
        description="Proxmox API viewer URL used if regeneration is requested.",
    ),
    version_tag: str = Query(
        default=DEFAULT_PROXMOX_OPENAPI_TAG,
        description="Generated artifact version tag to load.",
    ),
):
    """Return generated Pydantic v2 models source code for Proxmox API endpoints."""

    try:
        output_dir = get_user_generated_dir()
        output_dir.mkdir(parents=True, exist_ok=True)
        models_path = output_dir / version_tag / "pydantic_models.py"
        if not models_path.exists():
            bundled = get_bundled_generated_dir() / version_tag / "pydantic_models.py"
            if bundled.exists():
                models_path = bundled
        if regenerate or not models_path.exists():
            _enforce_codegen_source_url(source_url)
            bundle = await generate_proxmox_codegen_bundle_async(
                output_dir=output_dir,
                source_url=source_url,
                version_tag=version_tag,
                worker_count=workers,
                retry_count=retry_count,
                retry_backoff_seconds=retry_backoff,
                checkpoint_every=checkpoint_every,
            )
            return bundle.pydantic_models_code
        return models_path.read_text(encoding="utf-8")
    except Exception as error:
        raise ProxboxException(
            message="Failed to load generated Pydantic models.",
            python_exception=str(error),
        )
