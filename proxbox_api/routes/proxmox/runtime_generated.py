"""Runtime-generated FastAPI routes for the Proxmox API contract."""

from __future__ import annotations

import inspect
import json
import sys
import threading
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path as FilePath
from types import ModuleType
from typing import Literal

from fastapi import Body, Depends, FastAPI, Path, Query

from proxbox_api.database import get_async_session
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.proxmox_codegen.pydantic_generator import (
    generate_pydantic_models_from_openapi,
)
from proxbox_api.proxmox_codegen.utils import extract_path_params, pascal_case, slugify_identifier
from proxbox_api.proxmox_to_netbox.proxmox_schema import (
    DEFAULT_PROXMOX_OPENAPI_TAG,
    available_proxmox_sdk_versions,
    load_proxmox_generated_openapi,
    proxmox_generated_route_cache_path,
)
from proxbox_api.session.proxmox import resolve_proxmox_target_session

_GENERATED_ROUTE_TAG_PREFIX = "proxmox / live-generated"
_GENERATED_ROUTE_NAME_PREFIX = "generated_proxmox_route__"
_GENERATED_ROUTE_CACHE_FORMAT = 1
_GENERATED_ROUTE_STATE_LOCK = threading.RLock()
_GENERATED_ROUTE_STATE: dict[str, object] = {
    "route_names": set(),
    "versions": {},
    "alias_version_tag": DEFAULT_PROXMOX_OPENAPI_TAG,
    "cache_path": str(proxmox_generated_route_cache_path()),
    "cache_enabled": True,
    "loaded_from_cache": False,
}


def _schema_to_annotation(schema: dict[str, object] | None) -> object:
    if not isinstance(schema, dict):
        return object

    schema_type = schema.get("type")
    if schema_type == "string":
        return str
    if schema_type == "integer":
        return int
    if schema_type == "number":
        return float
    if schema_type == "boolean":
        return bool
    if schema_type == "array":
        return list[_schema_to_annotation(schema.get("items"))]
    if schema_type == "object":
        return dict[str, object]
    return object


def _request_schema_without_path_params(
    path: str, operation: dict[str, object]
) -> dict[str, object] | None:
    request_schema = (
        operation.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )
    if not isinstance(request_schema, dict):
        return None

    path_param_names = set(extract_path_params(path))
    if not path_param_names:
        return request_schema

    schema = deepcopy(request_schema)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema["properties"] = {
            name: value for name, value in properties.items() if name not in path_param_names
        }
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [name for name in required if name not in path_param_names]
    return schema


def _render_proxmox_path(path_template: str, path_values: dict[str, object]) -> str:
    rendered = path_template
    for name, value in path_values.items():
        str_value = str(value)
        if ".." in str_value or str_value.startswith("/") or str_value.startswith("\\"):
            raise ValueError(f"Invalid path parameter value for {name}: {str_value!r}")
        rendered = rendered.replace(f"{{{name}}}", str_value)
    return rendered


def _load_model_module(openapi_document: dict[str, object], version_tag: str) -> ModuleType:
    code = generate_pydantic_models_from_openapi(openapi_document)
    module = ModuleType(f"proxbox_api.generated.proxmox.runtime_{version_tag.replace('.', '_')}")
    sys.modules[module.__name__] = module
    exec(code, module.__dict__)
    for value in module.__dict__.values():
        if (
            isinstance(value, type)
            and getattr(value, "__module__", None) == module.__name__
            and hasattr(value, "model_rebuild")
        ):
            value.model_rebuild(_types_namespace=module.__dict__)
    return module


def _version_sort_key(version_tag: str) -> tuple[int, str]:
    return (0 if version_tag == DEFAULT_PROXMOX_OPENAPI_TAG else 1, version_tag)


def _generated_route_cache_path() -> FilePath:
    return proxmox_generated_route_cache_path()


def _read_generated_route_cache() -> dict[str, object] | None:
    cache_path = _generated_route_cache_path()
    if not cache_path.exists():
        return None

    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception as error:
        logger.warning(
            "Unable to load generated Proxmox route cache from %s: %s",
            cache_path,
            error,
        )
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("cache_format") != _GENERATED_ROUTE_CACHE_FORMAT:
        return None

    documents = payload.get("documents")
    if not isinstance(documents, dict) or not documents:
        return None

    normalized_documents: dict[str, dict[str, object]] = {}
    for version_tag, document in documents.items():
        if not isinstance(version_tag, str) or not isinstance(document, dict):
            return None
        normalized_documents[version_tag] = document

    return {
        "alias_version_tag": payload.get("alias_version_tag") or DEFAULT_PROXMOX_OPENAPI_TAG,
        "documents": dict(
            sorted(normalized_documents.items(), key=lambda item: _version_sort_key(item[0]))
        ),
        "generated_at": payload.get("generated_at"),
        "source": "runtime-cache",
    }


def _write_generated_route_cache(
    *,
    documents: dict[str, dict[str, object]],
    alias_version_tag: str,
) -> FilePath:
    cache_path = _generated_route_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_payload = {
        "cache_format": _GENERATED_ROUTE_CACHE_FORMAT,
        "generated_at": datetime.now(UTC).isoformat(),
        "alias_version_tag": alias_version_tag,
        "mounted_versions": sorted(documents.keys(), key=_version_sort_key),
        "documents": documents,
    }
    cache_path.write_text(json.dumps(cache_payload, indent=2, sort_keys=True), encoding="utf-8")
    return cache_path


def _operation_response_model(model_module: ModuleType, operation_id: str) -> type | None:
    return getattr(model_module, f"{pascal_case(operation_id)}Response", None)


def _operation_request_model(
    model_module: ModuleType,
    path: str,
    operation: dict[str, object],
    operation_id: str,
) -> type | None:
    request_schema = _request_schema_without_path_params(path=path, operation=operation)
    if not isinstance(request_schema, dict):
        return None
    if not request_schema.get("properties"):
        return None
    return getattr(model_module, f"{pascal_case(operation_id)}Request", None)


def _operation_parameters(operation: dict[str, object]) -> list[dict[str, object]]:
    parameters = operation.get("parameters")
    if not isinstance(parameters, list):
        return []
    return [parameter for parameter in parameters if isinstance(parameter, dict)]


def _path_parameter_name_map(operation: dict[str, object]) -> dict[str, str]:
    used_parameter_names = {
        "_database_session",
        "source",
        "target_name",
        "target_domain",
        "target_ip_address",
        "request_body",
    }
    mapping: dict[str, str] = {}

    for parameter in _operation_parameters(operation):
        if parameter.get("in") != "path":
            continue
        original_name = parameter.get("name")
        if not isinstance(original_name, str):
            continue

        python_name = slugify_identifier(original_name)
        if python_name in used_parameter_names:
            candidate = f"op_{python_name}"
            suffix = 1
            while candidate in used_parameter_names:
                candidate = f"op_{python_name}_{suffix}"
                suffix += 1
            python_name = candidate

        used_parameter_names.add(python_name)
        mapping[original_name] = python_name

    return mapping


def _mounted_fastapi_path(openapi_path: str, operation: dict[str, object]) -> str:
    mounted_path = openapi_path
    for original_name, python_name in _path_parameter_name_map(operation).items():
        mounted_path = mounted_path.replace(f"{{{original_name}}}", f"{{{python_name}}}")
    return mounted_path


def _build_generated_endpoint(  # noqa: C901
    *,
    openapi_path: str,
    method: str,
    operation: dict[str, object],
    request_model: type | None,
    response_model: type | None,
) -> object:
    operation_id = operation.get("operationId") or f"{method.lower()}_{openapi_path}"
    path_param_name_map = _path_parameter_name_map(operation)
    path_param_map: dict[str, str] = {
        python_name: original_name for original_name, python_name in path_param_name_map.items()
    }
    query_param_map: dict[str, str] = {}
    signature_parameters: list[inspect.Parameter] = [
        inspect.Parameter(
            "_database_session",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=object,
            default=Depends(get_async_session),
        ),
        inspect.Parameter(
            "source",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=Literal["database", "netbox"],
            default=Query(
                default="database",
                description="Source of configured Proxmox endpoints.",
            ),
        ),
        inspect.Parameter(
            "target_name",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=str | None,
            default=Query(
                default=None,
                description="Explicit Proxmox endpoint name when multiple endpoints are configured.",
            ),
        ),
        inspect.Parameter(
            "target_domain",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=str | None,
            default=Query(
                default=None,
                description="Explicit Proxmox endpoint domain when multiple endpoints are configured.",
            ),
        ),
        inspect.Parameter(
            "target_ip_address",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=str | None,
            default=Query(
                default=None,
                description="Explicit Proxmox endpoint IP address when multiple endpoints are configured.",
            ),
        ),
    ]

    used_parameter_names = {param.name for param in signature_parameters}
    control_query_aliases = {"source", "target_name", "target_domain", "target_ip_address"}

    def unique_parameter_name(base: str) -> str:
        candidate = base
        suffix = 1
        while candidate in used_parameter_names:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate

    for parameter in _operation_parameters(operation):
        location = parameter.get("in")
        original_name = parameter.get("name")
        if not isinstance(original_name, str):
            continue

        schema = parameter.get("schema") if isinstance(parameter.get("schema"), dict) else {}
        description = parameter.get("description")
        required = bool(parameter.get("required"))
        python_name = (
            path_param_name_map[original_name]
            if location == "path" and original_name in path_param_name_map
            else slugify_identifier(original_name)
        )
        if python_name in used_parameter_names:
            python_name = unique_parameter_name(f"op_{python_name}")
        annotation = _schema_to_annotation(schema)
        alias: str | None = original_name if python_name != original_name else None
        if location == "query" and original_name in control_query_aliases:
            alias = f"op_{original_name}"

        if location == "path":
            signature_parameters.append(
                inspect.Parameter(
                    python_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=annotation,
                    default=Path(
                        ...,
                        description=description,
                    ),
                )
            )
            used_parameter_names.add(python_name)
            continue

        if location == "query":
            query_param_map[python_name] = original_name
            if not required:
                annotation = annotation | None
            signature_parameters.append(
                inspect.Parameter(
                    python_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=annotation,
                    default=Query(
                        ... if required else None,
                        description=description,
                        alias=alias,
                    ),
                )
            )
            used_parameter_names.add(python_name)

    if request_model is not None:
        signature_parameters.append(
            inspect.Parameter(
                "request_body",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=request_model,
                default=Body(...),
            )
        )

    async def generated_endpoint(**kwargs: object) -> object:
        database_session = kwargs.pop("_database_session")
        request_body = kwargs.pop("request_body", None)
        source = kwargs.pop("source", "database")
        name = kwargs.pop("target_name", None)
        domain = kwargs.pop("target_domain", None)
        ip_address = kwargs.pop("target_ip_address", None)

        target = None
        try:
            target = await resolve_proxmox_target_session(
                database_session=database_session,
                source=source,
                name=name,
                domain=domain,
                ip_address=ip_address,
            )

            path_values = {
                original_name: kwargs.pop(python_name)
                for python_name, original_name in path_param_map.items()
            }
            query_values = {
                original_name: kwargs.get(python_name)
                for python_name, original_name in query_param_map.items()
                if kwargs.get(python_name) is not None
            }

            resource = target.session(_render_proxmox_path(openapi_path, path_values).lstrip("/"))
            handler = getattr(resource, method.lower())

            payload: dict[str, object] = {}
            if request_body is not None:
                payload.update(request_body.model_dump(by_alias=True, exclude_none=True))
            payload.update(query_values)

            if method.upper() == "GET":
                result = await resolve_async(handler(**query_values))
            else:
                result = await resolve_async(handler(**payload))
        except ProxboxException:
            raise
        except Exception as error:
            raise ProxboxException(
                message=f"Generated Proxmox proxy request failed for {method.upper()} {openapi_path}.",
                detail=f"Operation ID: {operation_id}",
                python_exception=str(error),
            )
        finally:
            close_method = getattr(target, "aclose", None)
            if callable(close_method):
                await close_method()

        if response_model is None:
            return result

        try:
            return response_model.model_validate(result)
        except Exception as error:
            raise ProxboxException(
                message=f"Generated Proxmox proxy response validation failed for {method.upper()} {openapi_path}.",
                detail=f"Operation ID: {operation_id}",
                python_exception=str(error),
            )

    generated_endpoint.__name__ = f"{_GENERATED_ROUTE_NAME_PREFIX}{operation_id}"
    generated_endpoint.__qualname__ = generated_endpoint.__name__
    generated_endpoint.__signature__ = inspect.Signature(
        parameters=signature_parameters,
        return_annotation=response_model or dict[str, object],
    )
    return generated_endpoint


def _remove_generated_routes(app: FastAPI, route_names: set[str]) -> None:
    if not route_names:
        return
    app.router.routes = [
        route for route in app.router.routes if getattr(route, "name", None) not in route_names
    ]


def _prioritize_generated_routes(app: FastAPI, route_names: set[str]) -> None:
    if not route_names:
        return

    generated_routes = [
        route for route in app.router.routes if getattr(route, "name", None) in route_names
    ]
    other_routes = [
        route for route in app.router.routes if getattr(route, "name", None) not in route_names
    ]
    app.router.routes = generated_routes + other_routes


def _build_version_route_specs(
    *,
    version_tag: str,
    document: dict[str, object],
) -> tuple[list[dict[str, object]], dict[str, object]]:
    model_module = _load_model_module(document, version_tag=version_tag)
    route_specs: list[dict[str, object]] = []
    method_count = 0
    version_route_names: set[str] = set()

    for openapi_path, path_item in sorted((document.get("paths") or {}).items()):
        if not isinstance(path_item, dict):
            continue
        for method, operation in sorted(path_item.items()):
            if method.upper() not in {"GET", "POST", "PUT", "DELETE"}:
                continue
            if not isinstance(operation, dict):
                continue

            operation_id = operation.get("operationId") or f"{method.lower()}_{openapi_path}"
            base_route_name = (
                f"{_GENERATED_ROUTE_NAME_PREFIX}{version_tag}__{method.lower()}__{operation_id}"
            )
            request_model = _operation_request_model(
                model_module=model_module,
                path=openapi_path,
                operation=operation,
                operation_id=operation_id,
            )
            response_model = _operation_response_model(
                model_module=model_module,
                operation_id=operation_id,
            )
            endpoint = _build_generated_endpoint(
                openapi_path=openapi_path,
                method=method,
                operation=operation,
                request_model=request_model,
                response_model=response_model,
            )
            mounted_fastapi_path = _mounted_fastapi_path(openapi_path, operation)
            route_specs.append(
                {
                    "path": f"/proxmox/api2/{version_tag}{mounted_fastapi_path}",
                    "endpoint": endpoint,
                    "methods": [method.upper()],
                    "name": base_route_name,
                    "summary": operation.get("summary"),
                    "description": operation.get("description"),
                    "response_model": response_model,
                    "tags": [f"{_GENERATED_ROUTE_TAG_PREFIX} / {version_tag}"],
                    # Only expose the latest version in Swagger UI; older versions
                    # are still routable but hidden to keep /openapi.json small.
                    "include_in_schema": version_tag == DEFAULT_PROXMOX_OPENAPI_TAG,
                }
            )
            version_route_names.add(base_route_name)

            if version_tag == DEFAULT_PROXMOX_OPENAPI_TAG:
                alias_route_name = f"{base_route_name}__alias"
                route_specs.append(
                    {
                        "path": f"/proxmox/api2{mounted_fastapi_path}",
                        "endpoint": endpoint,
                        "methods": [method.upper()],
                        "name": alias_route_name,
                        "summary": operation.get("summary"),
                        "description": operation.get("description"),
                        "response_model": response_model,
                        "tags": [f"{_GENERATED_ROUTE_TAG_PREFIX} / {version_tag}"],
                        # Alias duplicates the versioned latest route; hide to avoid
                        # doubling the Swagger entry count.
                        "include_in_schema": False,
                    }
                )
                version_route_names.add(alias_route_name)

            method_count += 1

    state = {
        "route_names": version_route_names,
        "route_count": len(route_specs),
        "path_count": len(document.get("paths") or {}),
        "method_count": method_count,
        "schema_version": document.get("info", {}).get("version"),
    }
    return route_specs, state


def _load_documents_for_registration(
    *,
    version_tag: str | None = None,
    openapi_document: dict[str, object] | None = None,
    openapi_documents: dict[str, dict[str, object]] | None = None,
) -> dict[str, dict[str, object]]:
    if openapi_documents is not None:
        return dict(sorted(openapi_documents.items(), key=lambda item: _version_sort_key(item[0])))
    if openapi_document is not None:
        target_version = version_tag or DEFAULT_PROXMOX_OPENAPI_TAG
        return {target_version: openapi_document}
    if version_tag is not None:
        return {version_tag: load_proxmox_generated_openapi(version_tag=version_tag)}

    cached = _read_generated_route_cache()
    if cached is not None:
        return cached["documents"]

    versions = available_proxmox_sdk_versions()
    return {
        discovered_version: load_proxmox_generated_openapi(version_tag=discovered_version)
        for discovered_version in sorted(versions, key=_version_sort_key)
    }


def register_generated_proxmox_routes(
    app: FastAPI,
    *,
    version_tag: str | None = None,
    openapi_document: dict[str, object] | None = None,
    openapi_documents: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    """Register runtime-generated live Proxmox proxy routes on the FastAPI app."""

    with _GENERATED_ROUTE_STATE_LOCK:
        cache_snapshot = None
        cache_source = "explicit"
        if openapi_documents is None and openapi_document is None and version_tag is None:
            cache_snapshot = _read_generated_route_cache()
            if cache_snapshot is not None:
                cache_source = "runtime-cache"
            else:
                cache_source = "generated-artifacts"
        elif version_tag is not None:
            cache_source = "generated-artifacts"

        documents = _load_documents_for_registration(
            version_tag=version_tag,
            openapi_document=openapi_document,
            openapi_documents=openapi_documents,
        )
        if not documents:
            raise ProxboxException(
                message="Generated Proxmox OpenAPI schema not found.",
                detail="Run /proxmox/viewer/generate first.",
            )

        route_specs: list[dict[str, object]] = []
        version_state: dict[str, dict[str, object]] = {}
        all_route_names: set[str] = set()

        for mounted_version, document in documents.items():
            if not document:
                raise ProxboxException(
                    message="Generated Proxmox OpenAPI schema not found.",
                    detail=f"Missing or unreadable schema for version tag '{mounted_version}'.",
                )

            version_specs, state = _build_version_route_specs(
                version_tag=mounted_version,
                document=document,
            )
            route_specs.extend(version_specs)
            version_state[mounted_version] = state
            all_route_names.update(state["route_names"])

        previous_names = set(_GENERATED_ROUTE_STATE["route_names"])
        _remove_generated_routes(app, previous_names)
        for spec in route_specs:
            app.add_api_route(**spec)
        _prioritize_generated_routes(app, all_route_names)

        cache_path = _write_generated_route_cache(
            documents=dict(sorted(documents.items(), key=lambda item: _version_sort_key(item[0]))),
            alias_version_tag=_GENERATED_ROUTE_STATE["alias_version_tag"],
        )
        app.openapi_schema = None
        _GENERATED_ROUTE_STATE["route_names"] = all_route_names
        _GENERATED_ROUTE_STATE["versions"] = version_state
        _GENERATED_ROUTE_STATE["cache_path"] = str(cache_path)
        _GENERATED_ROUTE_STATE["cache_enabled"] = True
        _GENERATED_ROUTE_STATE["loaded_from_cache"] = cache_source == "runtime-cache"

        return {
            "message": "Generated Proxmox live routes registered.",
            "mounted_versions": sorted(version_state.keys(), key=_version_sort_key),
            "alias_version_tag": _GENERATED_ROUTE_STATE["alias_version_tag"],
            "cache_path": str(cache_path),
            "cache_source": cache_source,
            "route_count": len(route_specs),
            "versions": {
                mounted_version: {
                    "route_count": state["route_count"],
                    "path_count": state["path_count"],
                    "method_count": state["method_count"],
                    "schema_version": state["schema_version"],
                }
                for mounted_version, state in sorted(
                    version_state.items(), key=lambda item: _version_sort_key(item[0])
                )
            },
        }


def generated_proxmox_route_state() -> dict[str, object]:
    """Return metadata about the currently mounted generated Proxmox route set."""

    with _GENERATED_ROUTE_STATE_LOCK:
        versions = _GENERATED_ROUTE_STATE["versions"]
        return {
            "mounted_versions": sorted(versions.keys(), key=_version_sort_key),
            "alias_version_tag": _GENERATED_ROUTE_STATE["alias_version_tag"],
            "cache_path": _GENERATED_ROUTE_STATE["cache_path"],
            "cache_enabled": _GENERATED_ROUTE_STATE["cache_enabled"],
            "loaded_from_cache": _GENERATED_ROUTE_STATE["loaded_from_cache"],
            "route_count": len(_GENERATED_ROUTE_STATE["route_names"]),
            "versions": {
                mounted_version: {
                    "route_count": state["route_count"],
                    "path_count": state["path_count"],
                    "method_count": state["method_count"],
                    "schema_version": state["schema_version"],
                }
                for mounted_version, state in sorted(
                    versions.items(), key=lambda item: _version_sort_key(item[0])
                )
            },
        }


def clear_generated_proxmox_routes(app: FastAPI) -> None:
    """Remove all mounted runtime-generated Proxmox routes from the app."""

    with _GENERATED_ROUTE_STATE_LOCK:
        _remove_generated_routes(app, set(_GENERATED_ROUTE_STATE["route_names"]))
        _GENERATED_ROUTE_STATE["route_names"] = set()
        _GENERATED_ROUTE_STATE["versions"] = {}
        _GENERATED_ROUTE_STATE["loaded_from_cache"] = False
        app.openapi_schema = None


def clear_generated_proxmox_route_cache() -> None:
    """Remove the persisted runtime-generated Proxmox route cache artifact."""

    cache_path = _generated_route_cache_path()
    if cache_path.exists():
        cache_path.unlink()
