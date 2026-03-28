"""Runtime-generated FastAPI routes for the Proxmox API contract."""

from __future__ import annotations

import inspect
import sys
import threading
from copy import deepcopy
from types import ModuleType
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, Path, Query

from proxbox_api.database import get_session
from proxbox_api.exception import ProxboxException
from proxbox_api.proxmox_codegen.pydantic_generator import (
    generate_pydantic_models_from_openapi,
)
from proxbox_api.proxmox_codegen.utils import extract_path_params, pascal_case, slugify_identifier
from proxbox_api.proxmox_to_netbox.proxmox_schema import (
    DEFAULT_PROXMOX_OPENAPI_TAG,
    available_proxmox_openapi_versions,
    load_proxmox_generated_openapi,
)
from proxbox_api.session.proxmox import resolve_proxmox_target_session

_GENERATED_ROUTE_TAG_PREFIX = "proxmox / live-generated"
_GENERATED_ROUTE_NAME_PREFIX = "generated_proxmox_route__"
_GENERATED_ROUTE_STATE_LOCK = threading.RLock()
_GENERATED_ROUTE_STATE: dict[str, Any] = {
    "route_names": set(),
    "versions": {},
    "alias_version_tag": DEFAULT_PROXMOX_OPENAPI_TAG,
}


def _schema_to_annotation(schema: dict[str, Any] | None) -> Any:
    if not isinstance(schema, dict):
        return Any

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
        return dict[str, Any]
    return Any


def _request_schema_without_path_params(
    path: str, operation: dict[str, Any]
) -> dict[str, Any] | None:
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


def _render_proxmox_path(path_template: str, path_values: dict[str, Any]) -> str:
    rendered = path_template
    for name, value in path_values.items():
        rendered = rendered.replace(f"{{{name}}}", str(value))
    return rendered


def _load_model_module(openapi_document: dict[str, Any], version_tag: str) -> ModuleType:
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


def _operation_response_model(model_module: ModuleType, operation_id: str) -> type | None:
    return getattr(model_module, f"{pascal_case(operation_id)}Response", None)


def _operation_request_model(
    model_module: ModuleType,
    path: str,
    operation: dict[str, Any],
    operation_id: str,
) -> type | None:
    request_schema = _request_schema_without_path_params(path=path, operation=operation)
    if not isinstance(request_schema, dict):
        return None
    if not request_schema.get("properties"):
        return None
    return getattr(model_module, f"{pascal_case(operation_id)}Request", None)


def _operation_parameters(operation: dict[str, Any]) -> list[dict[str, Any]]:
    parameters = operation.get("parameters")
    if not isinstance(parameters, list):
        return []
    return [parameter for parameter in parameters if isinstance(parameter, dict)]


def _build_generated_endpoint(
    *,
    openapi_path: str,
    method: str,
    operation: dict[str, Any],
    request_model: type | None,
    response_model: type | None,
) -> Any:
    operation_id = operation.get("operationId") or f"{method.lower()}_{openapi_path}"
    path_param_map: dict[str, str] = {}
    query_param_map: dict[str, str] = {}
    signature_parameters: list[inspect.Parameter] = [
        inspect.Parameter(
            "_database_session",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=Any,
            default=Depends(get_session),
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
        python_name = slugify_identifier(original_name)
        if python_name in used_parameter_names:
            python_name = unique_parameter_name(f"op_{python_name}")
        annotation = _schema_to_annotation(schema)
        alias: str | None = original_name if python_name != original_name else None
        if location == "query" and original_name in control_query_aliases:
            alias = f"op_{original_name}"

        if location == "path":
            path_param_map[python_name] = original_name
            signature_parameters.append(
                inspect.Parameter(
                    python_name,
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=annotation,
                    default=Path(
                        ...,
                        description=description,
                        alias=alias,
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

    async def generated_endpoint(**kwargs: Any) -> Any:
        database_session = kwargs.pop("_database_session")
        request_body = kwargs.pop("request_body", None)
        source = kwargs.pop("source", "database")
        name = kwargs.pop("target_name", None)
        domain = kwargs.pop("target_domain", None)
        ip_address = kwargs.pop("target_ip_address", None)

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

        payload: dict[str, Any] = {}
        if request_body is not None:
            payload.update(request_body.model_dump(by_alias=True, exclude_none=True))
        payload.update(query_values)

        try:
            if method.upper() == "GET":
                result = handler(**query_values)
            else:
                result = handler(**payload)
        except ProxboxException:
            raise
        except Exception as error:
            raise ProxboxException(
                message=f"Generated Proxmox proxy request failed for {method.upper()} {openapi_path}.",
                detail=f"Operation ID: {operation_id}",
                python_exception=str(error),
            )

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
        return_annotation=response_model or dict[str, Any],
    )
    return generated_endpoint


def _remove_generated_routes(app: FastAPI, route_names: set[str]) -> None:
    if not route_names:
        return
    app.router.routes = [
        route for route in app.router.routes if getattr(route, "name", None) not in route_names
    ]


def _build_version_route_specs(
    *,
    version_tag: str,
    document: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    model_module = _load_model_module(document, version_tag=version_tag)
    route_specs: list[dict[str, Any]] = []
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
            route_specs.append(
                {
                    "path": f"/proxmox/api2/{version_tag}/json{openapi_path}",
                    "endpoint": endpoint,
                    "methods": [method.upper()],
                    "name": base_route_name,
                    "summary": operation.get("summary"),
                    "description": operation.get("description"),
                    "response_model": response_model,
                    "tags": [f"{_GENERATED_ROUTE_TAG_PREFIX} / {version_tag}"],
                }
            )
            version_route_names.add(base_route_name)

            if version_tag == DEFAULT_PROXMOX_OPENAPI_TAG:
                alias_route_name = f"{base_route_name}__alias"
                route_specs.append(
                    {
                        "path": f"/proxmox/api2/json{openapi_path}",
                        "endpoint": endpoint,
                        "methods": [method.upper()],
                        "name": alias_route_name,
                        "summary": operation.get("summary"),
                        "description": operation.get("description"),
                        "response_model": response_model,
                        "tags": [f"{_GENERATED_ROUTE_TAG_PREFIX} / {version_tag}"],
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
    openapi_document: dict[str, Any] | None = None,
    openapi_documents: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    if openapi_documents is not None:
        return dict(sorted(openapi_documents.items()))
    if openapi_document is not None:
        target_version = version_tag or DEFAULT_PROXMOX_OPENAPI_TAG
        return {target_version: openapi_document}
    if version_tag is not None:
        return {version_tag: load_proxmox_generated_openapi(version_tag=version_tag)}

    versions = available_proxmox_openapi_versions()
    return {
        discovered_version: load_proxmox_generated_openapi(version_tag=discovered_version)
        for discovered_version in versions
    }


def register_generated_proxmox_routes(
    app: FastAPI,
    *,
    version_tag: str | None = None,
    openapi_document: dict[str, Any] | None = None,
    openapi_documents: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Register runtime-generated live Proxmox proxy routes on the FastAPI app."""

    with _GENERATED_ROUTE_STATE_LOCK:
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

        route_specs: list[dict[str, Any]] = []
        version_state: dict[str, dict[str, Any]] = {}
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

        app.openapi_schema = None
        _GENERATED_ROUTE_STATE["route_names"] = all_route_names
        _GENERATED_ROUTE_STATE["versions"] = version_state

        return {
            "message": "Generated Proxmox live routes registered.",
            "mounted_versions": sorted(version_state.keys()),
            "alias_version_tag": _GENERATED_ROUTE_STATE["alias_version_tag"],
            "route_count": len(route_specs),
            "versions": {
                mounted_version: {
                    "route_count": state["route_count"],
                    "path_count": state["path_count"],
                    "method_count": state["method_count"],
                    "schema_version": state["schema_version"],
                }
                for mounted_version, state in sorted(version_state.items())
            },
        }


def generated_proxmox_route_state() -> dict[str, Any]:
    """Return metadata about the currently mounted generated Proxmox route set."""

    with _GENERATED_ROUTE_STATE_LOCK:
        versions = _GENERATED_ROUTE_STATE["versions"]
        return {
            "mounted_versions": sorted(versions.keys()),
            "alias_version_tag": _GENERATED_ROUTE_STATE["alias_version_tag"],
            "route_count": len(_GENERATED_ROUTE_STATE["route_names"]),
            "versions": {
                mounted_version: {
                    "route_count": state["route_count"],
                    "path_count": state["path_count"],
                    "method_count": state["method_count"],
                    "schema_version": state["schema_version"],
                }
                for mounted_version, state in sorted(versions.items())
            },
        }


def clear_generated_proxmox_routes(app: FastAPI) -> None:
    """Remove all mounted runtime-generated Proxmox routes from the app."""

    with _GENERATED_ROUTE_STATE_LOCK:
        _remove_generated_routes(app, set(_GENERATED_ROUTE_STATE["route_names"]))
        _GENERATED_ROUTE_STATE["route_names"] = set()
        _GENERATED_ROUTE_STATE["versions"] = {}
        app.openapi_schema = None
