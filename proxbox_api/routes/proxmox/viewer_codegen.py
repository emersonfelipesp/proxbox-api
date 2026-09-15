"""Runtime endpoints for Proxmox API Viewer code generation artifacts."""

from __future__ import annotations

import asyncio
import json
import threading
from hashlib import sha256

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from proxbox_api.exception import ProxboxException
from proxbox_api.proxmox_codegen.apidoc_parser import PROXMOX_API_VIEWER_URL
from proxbox_api.proxmox_codegen.pipeline import (
    codegen_output_directory,
    generate_proxmox_codegen_bundle_async,
    is_default_codegen_source,
)
from proxbox_api.proxmox_codegen.pydantic_generator import (
    generate_pydantic_models_from_openapi,
)
from proxbox_api.proxmox_codegen.security import (
    VERSION_TAG_PATTERN,
    SchemaLimitError,
    validate_version_tag,
)
from proxbox_api.proxmox_to_netbox.netbox_schema import netbox_openapi_schema_source
from proxbox_api.proxmox_to_netbox.proxmox_schema import (
    DEFAULT_PROXMOX_OPENAPI_TAG,
    get_user_generated_dir,
    has_bundled_proxmox_schema,
    load_proxmox_generated_openapi,
)
from proxbox_api.routes.proxmox.runtime_generated import (
    generated_proxmox_route_state,
    register_generated_proxmox_routes,
)
from proxbox_api.settings_client import get_settings
from proxbox_api.ssrf import validate_endpoint_url

common_router = APIRouter()
runtime_codegen_router = APIRouter()
bundled_only_router = APIRouter()
router = APIRouter()

MAX_RENDERED_PYDANTIC_BYTES = 2 * 1024 * 1024
_RENDERED_PYDANTIC_CACHE: dict[str, str] = {}
_RENDERED_PYDANTIC_CACHE_LOCK = threading.Lock()


def _validate_version_tag_for_request(version_tag: str) -> str:
    try:
        return validate_version_tag(version_tag)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


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


@runtime_codegen_router.post("/generate")
async def generate_viewer_codegen_artifacts(
    persist: bool = Query(
        default=True,
        description="Persist generated artifacts under the configured user schema directory.",
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
        pattern=VERSION_TAG_PATTERN,
        description="Version tag used for generated artifacts subdirectory.",
    ),
):
    """Run Proxmox API Viewer to OpenAPI and Pydantic generation pipeline."""

    version_tag = _validate_version_tag_for_request(version_tag)
    if persist and has_bundled_proxmox_schema(version_tag):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Version tag '{version_tag}' is bundled and immutable. "
                "Choose a new version_tag or set persist=false for an inspection-only generation."
            ),
        )
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
        inspection_only = not is_default_codegen_source(source_url)
        return {
            "message": (
                "Generation completed. Custom-source artifacts are inspection-only and are not "
                "used for runtime route registration."
                if inspection_only
                else "Generation completed"
            ),
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
            "output_dir": (
                str(
                    codegen_output_directory(
                        output_dir,
                        source_url=source_url,
                        version_tag=bundle.version_tag,
                    )
                )
                if output_dir
                else None
            ),
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


@runtime_codegen_router.get("/openapi")
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

    version_tag = _validate_version_tag_for_request(version_tag)
    try:
        if regenerate:
            _enforce_codegen_source_url(source_url)
            bundle = await generate_proxmox_codegen_bundle_async(
                output_dir=None,
                source_url=source_url,
                version_tag=version_tag,
                worker_count=workers,
                retry_count=retry_count,
                retry_backoff_seconds=retry_backoff,
                checkpoint_every=checkpoint_every,
            )
            return bundle.openapi

        schema = load_proxmox_generated_openapi(version_tag=version_tag)
        if schema:
            return schema
        _enforce_codegen_source_url(source_url)
        bundle = await generate_proxmox_codegen_bundle_async(
            output_dir=None,
            source_url=source_url,
            version_tag=version_tag,
            worker_count=workers,
            retry_count=retry_count,
            retry_backoff_seconds=retry_backoff,
            checkpoint_every=checkpoint_every,
        )
        return bundle.openapi
    except Exception as error:
        raise ProxboxException(
            message="Failed to load generated OpenAPI schema.",
            python_exception=str(error),
        )


@bundled_only_router.get("/openapi")
async def proxmox_viewer_bundled_openapi(
    regenerate: bool = Query(
        default=False,
        description="Unavailable unless runtime code generation is explicitly enabled.",
    ),
    version_tag: str = Query(
        default=DEFAULT_PROXMOX_OPENAPI_TAG,
        description="Bundled artifact version tag to load.",
    ),
):
    """Return an immutable bundled OpenAPI schema without generation fallback."""

    if regenerate:
        raise HTTPException(status_code=404, detail="Runtime code generation is disabled.")
    version_tag = _validate_version_tag_for_request(version_tag)
    schema = load_proxmox_generated_openapi(version_tag=version_tag, allow_user=False)
    if not schema:
        raise HTTPException(status_code=404, detail="Bundled Proxmox OpenAPI schema not found.")
    return schema


@common_router.get("/openapi/embedded")
async def proxmox_viewer_openapi_embedded(
    version_tag: str = Query(
        default=DEFAULT_PROXMOX_OPENAPI_TAG,
        description="Generated artifact version tag to load.",
    ),
):
    """Return generated Proxmox OpenAPI as consumed by custom FastAPI OpenAPI extension."""

    version_tag = _validate_version_tag_for_request(version_tag)
    schema = load_proxmox_generated_openapi(version_tag=version_tag)
    if not schema:
        raise ProxboxException(
            message="Generated Proxmox OpenAPI schema not found.",
            detail="Run /proxmox/viewer/generate first.",
        )
    return schema


@common_router.get("/integration/contracts")
async def proxmox_netbox_integration_contracts():
    """Report Proxmox and NetBox schema contract sources for transformation workflows."""

    proxmox = load_proxmox_generated_openapi()
    return {
        "proxmox_generated_openapi_present": bool(proxmox),
        "proxmox_generated_path_count": len((proxmox.get("paths") or {}).keys()) if proxmox else 0,
        "netbox_schema_source": netbox_openapi_schema_source(),
    }


@runtime_codegen_router.post("/routes/refresh")
async def refresh_generated_proxmox_routes(
    version_tag: str | None = Query(
        default=None,
        pattern=VERSION_TAG_PATTERN,
        description="Optional generated artifact version tag to rebuild. Omit to rebuild all available versions.",
    ),
):
    """Rebuild runtime-generated live Proxmox routes from the embedded OpenAPI contract."""

    from proxbox_api.main import app

    normalized_version_tag = (
        _validate_version_tag_for_request(version_tag) if isinstance(version_tag, str) else None
    )
    result = register_generated_proxmox_routes(app, version_tag=normalized_version_tag)
    result["state"] = generated_proxmox_route_state()
    return result


@common_router.get("/schema-status")
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
        version_tag = _validate_version_tag_for_request(version_tag)
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


@runtime_codegen_router.get("/pydantic", response_class=PlainTextResponse)
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

    version_tag = _validate_version_tag_for_request(version_tag)
    try:
        if regenerate:
            _enforce_codegen_source_url(source_url)
            bundle = await generate_proxmox_codegen_bundle_async(
                output_dir=None,
                source_url=source_url,
                version_tag=version_tag,
                worker_count=workers,
                retry_count=retry_count,
                retry_backoff_seconds=retry_backoff,
                checkpoint_every=checkpoint_every,
            )
            return await _render_pydantic_models(bundle.openapi)
        schema = load_proxmox_generated_openapi(version_tag=version_tag)
        if not schema:
            raise ProxboxException(
                message="Generated Proxmox OpenAPI schema not found.",
                detail="Run /proxmox/viewer/generate first.",
            )
        return await _render_pydantic_models(schema)
    except Exception as error:
        raise ProxboxException(
            message="Failed to load generated Pydantic models.",
            python_exception=str(error),
        )


def _rendered_schema_digest(schema: dict[str, object]) -> str:
    encoded = json.dumps(
        schema,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _render_pydantic_models_bounded(schema: dict[str, object]) -> str:
    digest = _rendered_schema_digest(schema)
    with _RENDERED_PYDANTIC_CACHE_LOCK:
        cached = _RENDERED_PYDANTIC_CACHE.get(digest)
    if cached is not None:
        return cached

    rendered = generate_pydantic_models_from_openapi(schema)
    rendered_bytes = rendered.encode("utf-8")
    if len(rendered_bytes) > MAX_RENDERED_PYDANTIC_BYTES:
        raise SchemaLimitError(
            "Rendered Pydantic source exceeds "
            f"MAX_RENDERED_PYDANTIC_BYTES ({MAX_RENDERED_PYDANTIC_BYTES})."
        )
    with _RENDERED_PYDANTIC_CACHE_LOCK:
        _RENDERED_PYDANTIC_CACHE[digest] = rendered
    return rendered


async def _render_pydantic_models(schema: dict[str, object]) -> str:
    return await asyncio.to_thread(_render_pydantic_models_bounded, schema)


@bundled_only_router.get("/pydantic", response_class=PlainTextResponse)
async def proxmox_viewer_bundled_pydantic_models(
    version_tag: str = Query(
        default=DEFAULT_PROXMOX_OPENAPI_TAG,
        description="Bundled artifact version tag to load.",
    ),
):
    """Render models only from an immutable bundled OpenAPI schema."""

    version_tag = _validate_version_tag_for_request(version_tag)
    try:
        schema = load_proxmox_generated_openapi(version_tag=version_tag, allow_user=False)
        if not schema:
            raise HTTPException(status_code=404, detail="Bundled Proxmox OpenAPI schema not found.")
        return await _render_pydantic_models(schema)
    except HTTPException:
        raise
    except Exception as error:
        raise ProxboxException(
            message="Failed to load generated Pydantic models.",
            python_exception=str(error),
        ) from error


router.include_router(common_router)
router.include_router(bundled_only_router)
