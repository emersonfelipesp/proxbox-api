"""Runtime-generated FastAPI routes for the Proxmox API contract."""

from __future__ import annotations

import inspect
import json
import os
import secrets
import stat
import sys
import threading
from collections import OrderedDict
from copy import copy, deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path as FilePath
from types import ModuleType
from typing import Literal

from fastapi import Body, Depends, FastAPI, Path, Query
from fastapi.routing import APIRoute, request_response

from proxbox_api.database import get_async_session
from proxbox_api.exception import ProxboxException
from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.proxmox_codegen.pydantic_generator import (
    build_pydantic_models_from_openapi,
)
from proxbox_api.proxmox_codegen.security import (
    MAX_AGGREGATE_DOCUMENT_BYTES,
    MAX_AGGREGATE_MODELS,
    MAX_AGGREGATE_ROUTES,
    MAX_ELIGIBLE_VERSIONS,
    MAX_SCHEMA_DEPTH,
    OpenAPIDocumentValidation,
    SchemaLimitError,
    SchemaShapeError,
    SchemaValidationError,
    count_openapi_models,
    resolve_contained,
    snapshot_openapi_document,
    validate_openapi_document_limits,
    validate_version_tag,
)
from proxbox_api.proxmox_codegen.utils import extract_path_params, pascal_case, slugify_identifier
from proxbox_api.proxmox_to_netbox.proxmox_schema import (
    DEFAULT_PROXMOX_OPENAPI_TAG,
    RUNTIME_GENERATED_ROUTE_CACHE_PROVENANCE_FILENAME,
    available_proxmox_sdk_versions,
    bundled_openapi_document_validation,
    load_proxmox_generated_openapi,
    provenance_verified_artifact_bytes,
    proxmox_generated_route_cache_path,
)
from proxbox_api.runtime_settings import runtime_codegen_enabled
from proxbox_api.session.proxmox import resolve_proxmox_target_session

_GENERATED_ROUTE_TAG_PREFIX = "proxmox / live-generated"
_GENERATED_ROUTE_NAME_PREFIX = "generated_proxmox_route__"
_GENERATED_ROUTE_CACHE_FORMAT = 2
_GENERATED_ROUTE_STATE_LOCK = threading.RLock()
_GENERATED_ROUTE_STATE: dict[str, object] = {
    "route_names": set(),
    "versions": {},
    "alias_version_tag": DEFAULT_PROXMOX_OPENAPI_TAG,
    "cache_path": None,
    "cache_enabled": True,
    "loaded_from_cache": False,
}


@dataclass(frozen=True)
class _CachedRegistrationPlan:
    routes: tuple[APIRoute, ...]
    version_state: dict[str, dict[str, object]]
    route_names: frozenset[str]


# In-process cache of generated Pydantic model modules, keyed by version tag.
# Building a module (model construction + model_rebuild across the full Proxmox API
# surface) costs several seconds, and the ASGI lifespan re-runs route
# registration on every app startup. The test suite opens a TestClient(app)
# context manager per test, so without memoization each test re-paid that
# rebuild (~25s on some CPython patch levels), making the suite time out.
# Production starts once per process, so reusing an already-built module here
# does not change its behavior.
_MODEL_MODULE_CACHE_MAX_SIZE = MAX_ELIGIBLE_VERSIONS + 2
_MODEL_MODULE_CACHE: OrderedDict[tuple[str, str], ModuleType] = OrderedDict()
_REGISTRATION_PLAN_CACHE_MAX_SIZE = 2
_REGISTRATION_PLAN_CACHE: OrderedDict[
    tuple[tuple[tuple[str, str], ...], tuple[object, ...]],
    _CachedRegistrationPlan,
] = OrderedDict()


def _validated_document_snapshot(
    document: dict[str, object],
) -> tuple[dict[str, object], str, int]:
    cached = bundled_openapi_document_validation(document)
    if cached is not None:
        return snapshot_openapi_document(document, cached)

    snapshot, digest, byte_count = snapshot_openapi_document(document)
    validate_openapi_document_limits(snapshot)
    return snapshot, digest, byte_count


def _model_module_cache_key(
    openapi_document: dict[str, object],
    version_tag: str,
    validation: OpenAPIDocumentValidation | None = None,
) -> tuple[str, str]:
    if validation is None:
        _, digest, _ = _validated_document_snapshot(openapi_document)
    else:
        _, digest, _ = snapshot_openapi_document(openapi_document, validation)
    return version_tag, digest


def _cache_model_module(cache_key: tuple[str, str], module: ModuleType) -> None:
    _MODEL_MODULE_CACHE[cache_key] = module
    while len(_MODEL_MODULE_CACHE) > _MODEL_MODULE_CACHE_MAX_SIZE:
        _, evicted = _MODEL_MODULE_CACHE.popitem(last=False)
        if sys.modules.get(evicted.__name__) is evicted:
            sys.modules.pop(evicted.__name__, None)


def _schema_to_annotation(schema: dict[str, object] | None) -> object:
    array_depth = 0
    while isinstance(schema, dict) and schema.get("type") == "array":
        array_depth += 1
        if array_depth > MAX_SCHEMA_DEPTH:
            raise SchemaLimitError(
                f"OpenAPI schema depth exceeds MAX_SCHEMA_DEPTH ({MAX_SCHEMA_DEPTH})."
            )
        schema = schema.get("items")

    schema_type = schema.get("type") if isinstance(schema, dict) else None
    if schema_type == "string":
        annotation: object = str
    elif schema_type == "integer":
        annotation = int
    elif schema_type == "number":
        annotation = float
    elif schema_type == "boolean":
        annotation = bool
    elif schema_type == "object":
        annotation = dict[str, object]
    else:
        annotation = object
    for _ in range(array_depth):
        annotation = list[annotation]
    return annotation


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


def _load_model_module(
    openapi_document: dict[str, object],
    version_tag: str,
    validation: OpenAPIDocumentValidation | None = None,
) -> ModuleType:
    if validation is None:
        snapshot, digest, byte_count = _validated_document_snapshot(openapi_document)
    else:
        snapshot, digest, byte_count = snapshot_openapi_document(
            openapi_document,
            validation,
        )
    return _load_model_module_from_snapshot(
        snapshot,
        version_tag=version_tag,
        digest=digest,
        byte_count=byte_count,
    )


def _load_model_module_from_snapshot(
    openapi_document: dict[str, object],
    *,
    version_tag: str,
    digest: str,
    byte_count: int,
) -> ModuleType:
    cache_key = (version_tag, digest)
    with _GENERATED_ROUTE_STATE_LOCK:
        cached_module = _MODEL_MODULE_CACHE.pop(cache_key, None)
        if cached_module is not None:
            _MODEL_MODULE_CACHE[cache_key] = cached_module
            return cached_module
        module = ModuleType(
            f"proxbox_api.generated.proxmox.runtime_{version_tag.replace('.', '_')}_{digest[:16]}"
        )
        models = build_pydantic_models_from_openapi(
            openapi_document,
            validation=OpenAPIDocumentValidation(
                digest=digest,
                byte_count=byte_count,
                _document=openapi_document,
            ),
        )
        for model in models.values():
            model.__module__ = module.__name__
        module.__dict__.update(models)
        sys.modules[module.__name__] = module
        try:
            for value in module.__dict__.values():
                if (
                    isinstance(value, type)
                    and getattr(value, "__module__", None) == module.__name__
                    and hasattr(value, "model_rebuild")
                ):
                    value.model_rebuild(_types_namespace=module.__dict__)
        except Exception:
            if sys.modules.get(module.__name__) is module:
                sys.modules.pop(module.__name__, None)
            raise
        _cache_model_module(cache_key, module)
        return module


def _version_sort_key(version_tag: str) -> tuple[int, str]:
    return (0 if version_tag == DEFAULT_PROXMOX_OPENAPI_TAG else 1, version_tag)


def _document_route_count(version_tag: str, document: dict[str, object]) -> int:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return 0
    operation_count = sum(
        1
        for path_item in paths.values()
        if isinstance(path_item, dict)
        for method, operation in path_item.items()
        if isinstance(method, str)
        and method.upper() in {"GET", "POST", "PUT", "DELETE"}
        and isinstance(operation, dict)
    )
    return operation_count * (2 if version_tag == DEFAULT_PROXMOX_OPENAPI_TAG else 1)


def _validate_aggregate_registration_limits(
    documents: dict[str, dict[str, object]],
) -> tuple[
    dict[str, dict[str, object]],
    dict[str, tuple[str, int]],
]:
    if len(documents) > MAX_ELIGIBLE_VERSIONS:
        raise SchemaLimitError(
            f"Eligible version count exceeds MAX_ELIGIBLE_VERSIONS ({MAX_ELIGIBLE_VERSIONS})."
        )

    aggregate_bytes = 0
    aggregate_models = 0
    aggregate_routes = 0
    snapshots: dict[str, dict[str, object]] = {}
    document_metadata: dict[str, tuple[str, int]] = {}
    for version_tag, document in documents.items():
        validate_version_tag(version_tag)
        snapshot, digest, byte_count = _validated_document_snapshot(document)
        snapshots[version_tag] = snapshot
        document_metadata[version_tag] = (digest, byte_count)
        aggregate_bytes += byte_count
        aggregate_models += count_openapi_models(snapshot)
        aggregate_routes += _document_route_count(version_tag, snapshot)
    if aggregate_bytes > MAX_AGGREGATE_DOCUMENT_BYTES:
        raise SchemaLimitError(
            "Aggregate OpenAPI bytes exceed "
            f"MAX_AGGREGATE_DOCUMENT_BYTES ({MAX_AGGREGATE_DOCUMENT_BYTES})."
        )
    if aggregate_models > MAX_AGGREGATE_MODELS:
        raise SchemaLimitError(
            f"Aggregate model count exceeds MAX_AGGREGATE_MODELS ({MAX_AGGREGATE_MODELS})."
        )
    if aggregate_routes > MAX_AGGREGATE_ROUTES:
        raise SchemaLimitError(
            f"Aggregate route count exceeds MAX_AGGREGATE_ROUTES ({MAX_AGGREGATE_ROUTES})."
        )
    return snapshots, document_metadata


def _generated_route_cache_path() -> FilePath:
    return proxmox_generated_route_cache_path()


def _generated_route_cache_provenance_path() -> FilePath:
    return resolve_contained(
        _generated_route_cache_path().parent,
        RUNTIME_GENERATED_ROUTE_CACHE_PROVENANCE_FILENAME,
    )


def _normalize_cached_documents(
    documents: object,
) -> dict[str, dict[str, object]] | None:
    if not isinstance(documents, dict) or not documents:
        return None

    normalized: dict[str, dict[str, object]] = {}
    for version_tag, document in documents.items():
        if not isinstance(version_tag, str) or not isinstance(document, dict):
            return None
        try:
            version_tag = validate_version_tag(version_tag)
        except ValueError:
            return None
        normalized[version_tag] = document
    return normalized


def _normalize_cached_alias(alias_version_tag: object) -> str | None:
    alias = alias_version_tag or DEFAULT_PROXMOX_OPENAPI_TAG
    if not isinstance(alias, str):
        return None
    try:
        return validate_version_tag(alias)
    except ValueError:
        return None


def _read_generated_route_cache() -> dict[str, object] | None:
    cache_path = _generated_route_cache_path()
    provenance_path = _generated_route_cache_provenance_path()
    cache_bytes = provenance_verified_artifact_bytes(cache_path, provenance_path)
    if cache_bytes is None:
        logger.warning(
            "Ignoring generated Proxmox route cache with missing or invalid provenance: %s",
            cache_path,
        )
        return None

    try:
        payload = json.loads(cache_bytes)
    except (RecursionError, UnicodeError, ValueError) as error:
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

    normalized_documents = _normalize_cached_documents(payload.get("documents"))
    if normalized_documents is None:
        logger.warning(
            "Ignoring invalid or over-limit generated Proxmox route cache: %s", cache_path
        )
        return None
    try:
        normalized_documents, _ = _validate_aggregate_registration_limits(normalized_documents)
    except SchemaValidationError as error:
        logger.warning("Ignoring invalid generated Proxmox route cache: %s", error)
        return None
    alias_version_tag = _normalize_cached_alias(payload.get("alias_version_tag"))
    if alias_version_tag is None:
        return None
    if not _cached_documents_match_artifacts(normalized_documents):
        logger.warning(
            "Ignoring generated Proxmox route cache that differs from authoritative artifacts: %s",
            cache_path,
        )
        return None

    return {
        "alias_version_tag": alias_version_tag,
        "documents": dict(
            sorted(normalized_documents.items(), key=lambda item: _version_sort_key(item[0]))
        ),
        "generated_at": payload.get("generated_at"),
        "source": "runtime-cache",
    }


def _cached_documents_match_artifacts(documents: dict[str, dict[str, object]]) -> bool:
    available_versions = set(available_proxmox_sdk_versions())
    if set(documents) != available_versions:
        return False
    for version_tag, document in documents.items():
        authoritative = load_proxmox_generated_openapi(version_tag=version_tag)
        if not authoritative or _model_module_cache_key(authoritative, version_tag) != (
            _model_module_cache_key(document, version_tag)
        ):
            return False
    return True


def _write_atomic_regular_file(path: FilePath, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            raise OSError(f"Refusing to replace symlinked generated artifact: {path}")
    except FileNotFoundError:
        pass
    temporary_path = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
    descriptor = os.open(
        temporary_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _write_generated_route_cache_provenance(
    cache_path: FilePath,
    generated_at: str,
    cache_digest: str,
) -> None:
    provenance_path = resolve_contained(
        cache_path.parent,
        RUNTIME_GENERATED_ROUTE_CACHE_PROVENANCE_FILENAME,
    )
    provenance_bytes = json.dumps(
        {
            "source_url": "proxbox://runtime-generated-route-cache",
            "generated_at": generated_at,
            "sha256": cache_digest,
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    _write_atomic_regular_file(provenance_path, provenance_bytes)


def _generated_route_cache_matches(cache_path: FilePath, cache_bytes: bytes) -> bool:
    provenance_path = resolve_contained(
        cache_path.parent,
        RUNTIME_GENERATED_ROUTE_CACHE_PROVENANCE_FILENAME,
    )
    existing = provenance_verified_artifact_bytes(cache_path, provenance_path)
    if existing is None:
        return False
    return secrets.compare_digest(sha256(existing).digest(), sha256(cache_bytes).digest())


def _write_generated_route_cache(
    *,
    documents: dict[str, dict[str, object]],
    alias_version_tag: str,
    validations: dict[str, OpenAPIDocumentValidation] | None = None,
) -> FilePath:
    alias_version_tag = validate_version_tag(alias_version_tag)
    documents = {
        validate_version_tag(version_tag): document for version_tag, document in documents.items()
    }
    if validations is None:
        documents, _ = _validate_aggregate_registration_limits(documents)
    else:
        if set(validations) != set(documents):
            raise SchemaShapeError("OpenAPI validations do not match the cache documents.")
        documents = {
            version_tag: snapshot_openapi_document(document, validations[version_tag])[0]
            for version_tag, document in documents.items()
        }
    return _write_generated_route_cache_from_snapshots(
        documents=documents,
        alias_version_tag=alias_version_tag,
    )


def _write_generated_route_cache_from_snapshots(
    *,
    documents: dict[str, dict[str, object]],
    alias_version_tag: str,
) -> FilePath:
    cache_path = _generated_route_cache_path()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_payload = {
        "cache_format": _GENERATED_ROUTE_CACHE_FORMAT,
        "alias_version_tag": alias_version_tag,
        "mounted_versions": sorted(documents.keys(), key=_version_sort_key),
        "documents": documents,
    }
    cache_bytes = json.dumps(
        cache_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if _generated_route_cache_matches(cache_path, cache_bytes):
        return cache_path
    generated_at = datetime.now(UTC).isoformat()
    _write_atomic_regular_file(cache_path, cache_bytes)
    _write_generated_route_cache_provenance(
        cache_path, generated_at, sha256(cache_bytes).hexdigest()
    )
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


def _unique_parameter_name(base: str, used_names: set[str]) -> str:
    candidate = base
    suffix = 1
    while candidate in used_names:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _operation_signature_parameter(
    parameter: dict[str, object], python_name: str
) -> inspect.Parameter:
    schema = parameter.get("schema") if isinstance(parameter.get("schema"), dict) else {}
    annotation = _schema_to_annotation(schema)
    description = parameter.get("description")
    if parameter.get("in") == "path":
        default = Path(..., description=description)
    else:
        required = bool(parameter.get("required"))
        if not required:
            annotation = annotation | None
        original_name = parameter["name"]
        alias = original_name if python_name != original_name else None
        if original_name in {"source", "target_name", "target_domain", "target_ip_address"}:
            alias = f"op_{original_name}"
        default = Query(... if required else None, description=description, alias=alias)
    return inspect.Parameter(
        python_name,
        inspect.Parameter.KEYWORD_ONLY,
        annotation=annotation,
        default=default,
    )


def _append_operation_parameters(
    operation: dict[str, object],
    signature_parameters: list[inspect.Parameter],
    path_param_name_map: dict[str, str],
) -> dict[str, str]:
    used_parameter_names = {param.name for param in signature_parameters}
    query_param_map: dict[str, str] = {}
    for parameter in _operation_parameters(operation):
        location = parameter.get("in")
        original_name = parameter.get("name")
        if not isinstance(original_name, str) or location not in {"path", "query"}:
            continue
        python_name = (
            path_param_name_map.get(original_name, slugify_identifier(original_name))
            if location == "path"
            else slugify_identifier(original_name)
        )
        if python_name in used_parameter_names:
            python_name = _unique_parameter_name(f"op_{python_name}", used_parameter_names)
        if location == "query":
            query_param_map[python_name] = original_name
        signature_parameters.append(_operation_signature_parameter(parameter, python_name))
        used_parameter_names.add(python_name)
    return query_param_map


def _build_generated_endpoint(
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

    query_param_map = _append_operation_parameters(
        operation, signature_parameters, path_param_name_map
    )

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
        _require_generated_read(method)
        database_session = kwargs.pop("_database_session")
        kwargs.pop("request_body", None)
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
            result = await resolve_async(resource.get(**query_values))
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


def _require_generated_read(method: str) -> None:
    """Reject unsupported dispatch before resolving a target or its credentials."""
    if method.upper() != "GET":
        raise ProxboxException(
            message="Generated Proxmox proxy routes are read-only.",
            detail="Use a typed, audited RPC procedure for mutation operations.",
            http_status_code=403,
        )


def _generated_operation_metadata(method: str, operation: dict[str, object]) -> dict[str, object]:
    """Retain mutation schemas for discovery while documenting their runtime denial."""
    if method.upper() == "GET":
        return {"description": operation.get("description")}
    notice = (
        "Disabled: generated Proxmox proxy routes are read-only. "
        "This operation returns HTTP 403 without resolving a target or its credentials. "
        "Use a typed, audited RPC procedure for mutation operations."
    )
    return {
        "description": f"{notice}\n\n{operation.get('description') or ''}".rstrip(),
        "deprecated": True,
        "responses": {403: {"description": notice}},
    }


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
    validation: OpenAPIDocumentValidation | None = None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    version_tag = validate_version_tag(version_tag)
    if validation is None:
        snapshot, digest, byte_count = _validated_document_snapshot(document)
    else:
        snapshot, digest, byte_count = snapshot_openapi_document(document, validation)
    return _build_version_route_specs_from_snapshot(
        version_tag=version_tag,
        document=snapshot,
        digest=digest,
        byte_count=byte_count,
    )


def _build_version_route_specs_from_snapshot(
    *,
    version_tag: str,
    document: dict[str, object],
    digest: str,
    byte_count: int,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    model_module = _load_model_module_from_snapshot(
        document,
        version_tag=version_tag,
        digest=digest,
        byte_count=byte_count,
    )
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
                    **_generated_operation_metadata(method, operation),
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
                        **_generated_operation_metadata(method, operation),
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
) -> tuple[dict[str, dict[str, object]], str]:
    if openapi_documents is not None:
        return (
            dict(sorted(openapi_documents.items(), key=lambda item: _version_sort_key(item[0]))),
            "explicit",
        )
    if openapi_document is not None:
        target_version = version_tag or DEFAULT_PROXMOX_OPENAPI_TAG
        return {target_version: openapi_document}, "explicit"
    if version_tag is not None:
        return (
            {version_tag: load_proxmox_generated_openapi(version_tag=version_tag)},
            "generated-artifacts",
        )

    if runtime_codegen_enabled():
        cached = _read_generated_route_cache()
        if cached is not None:
            return cached["documents"], "runtime-cache"

    versions = available_proxmox_sdk_versions()
    return (
        {
            discovered_version: load_proxmox_generated_openapi(version_tag=discovered_version)
            for discovered_version in sorted(versions, key=_version_sort_key)
        },
        "generated-artifacts",
    )


def _public_version_states(
    versions: dict[str, dict[str, object]],
) -> dict[str, dict[str, object]]:
    return {
        mounted_version: {
            "route_count": state["route_count"],
            "path_count": state["path_count"],
            "method_count": state["method_count"],
            "schema_version": state["schema_version"],
        }
        for mounted_version, state in sorted(
            versions.items(), key=lambda item: _version_sort_key(item[0])
        )
    }


def _registration_result(
    *,
    message: str,
    cache_source: str,
    route_count: int,
) -> dict[str, object]:
    versions = _GENERATED_ROUTE_STATE["versions"]
    return {
        "message": message,
        "mounted_versions": sorted(versions.keys(), key=_version_sort_key),
        "alias_version_tag": _GENERATED_ROUTE_STATE["alias_version_tag"],
        "cache_path": _GENERATED_ROUTE_STATE["cache_path"],
        "cache_source": cache_source,
        "route_count": route_count,
        "versions": _public_version_states(versions),
    }


def _existing_registration_result(
    app: FastAPI,
    *,
    version_tag: str | None,
    openapi_document: dict[str, object] | None,
    openapi_documents: dict[str, dict[str, object]] | None,
    force_rebuild: bool,
) -> dict[str, object] | None:
    if force_rebuild or any(
        value is not None for value in (version_tag, openapi_document, openapi_documents)
    ):
        return None
    route_names = set(_GENERATED_ROUTE_STATE["route_names"])
    existing_route_names = {getattr(route, "name", None) for route in app.routes}
    if not route_names or not route_names.issubset(existing_route_names):
        return None
    return _registration_result(
        message="Generated Proxmox live routes already registered.",
        cache_source="in-process",
        route_count=len(route_names),
    )


def _registration_plan_cache_key(
    app: FastAPI,
    document_metadata: dict[str, tuple[str, int]],
) -> tuple[tuple[tuple[str, str], ...], tuple[object, ...]] | None:
    if any(
        (
            app.router.prefix,
            app.router.responses,
            app.router.tags,
            app.router.dependencies,
            app.router.callbacks,
            app.router.deprecated,
            not app.router.include_in_schema,
        )
    ):
        return None
    documents_key = tuple(
        sorted(
            ((version_tag, digest) for version_tag, (digest, _) in document_metadata.items()),
            key=lambda item: _version_sort_key(item[0]),
        )
    )
    router_key: tuple[object, ...] = (
        id(app.router.route_class),
        id(app.router.default_response_class),
        id(app.router.generate_unique_id_function),
        id(app.router.strict_content_type),
    )
    return documents_key, router_key


def _cached_registration_plan(
    cache_key: tuple[tuple[tuple[str, str], ...], tuple[object, ...]],
) -> _CachedRegistrationPlan | None:
    cached = _REGISTRATION_PLAN_CACHE.pop(cache_key, None)
    if cached is not None:
        _REGISTRATION_PLAN_CACHE[cache_key] = cached
    return cached


def _cache_registration_plan(
    cache_key: tuple[tuple[tuple[str, str], ...], tuple[object, ...]],
    plan: _CachedRegistrationPlan,
) -> None:
    _REGISTRATION_PLAN_CACHE[cache_key] = plan
    while len(_REGISTRATION_PLAN_CACHE) > _REGISTRATION_PLAN_CACHE_MAX_SIZE:
        _REGISTRATION_PLAN_CACHE.popitem(last=False)


def _mounted_generated_routes(app: FastAPI, route_names: set[str]) -> tuple[APIRoute, ...]:
    return tuple(
        _registration_route_template(route)
        for route in app.router.routes
        if isinstance(route, APIRoute) and getattr(route, "name", None) in route_names
    )


def _registration_route_template(route: APIRoute) -> APIRoute:
    template = copy(route)
    template.dependency_overrides_provider = None
    template.app = request_response(template.get_route_handler())
    return template


def _build_registration_plan(
    documents: dict[str, dict[str, object]],
    document_metadata: dict[str, tuple[str, int]] | None = None,
) -> tuple[list[dict[str, object]], dict[str, dict[str, object]], set[str]]:
    if document_metadata is None:
        documents, document_metadata = _validate_aggregate_registration_limits(documents)
    route_specs: list[dict[str, object]] = []
    version_state: dict[str, dict[str, object]] = {}
    all_route_names: set[str] = set()
    for mounted_version, document in documents.items():
        if not document:
            raise ProxboxException(
                message="Generated Proxmox OpenAPI schema not found.",
                detail=f"Missing or unreadable schema for version tag '{mounted_version}'.",
            )
        digest, byte_count = document_metadata[mounted_version]
        version_specs, state = _build_version_route_specs_from_snapshot(
            version_tag=mounted_version,
            document=document,
            digest=digest,
            byte_count=byte_count,
        )
        route_specs.extend(version_specs)
        version_state[mounted_version] = state
        all_route_names.update(state["route_names"])
    return route_specs, version_state, all_route_names


def _mount_registration_plan(
    app: FastAPI,
    route_specs: list[dict[str, object]],
    route_names: set[str],
) -> None:
    previous_names = set(_GENERATED_ROUTE_STATE["route_names"])
    _remove_generated_routes(app, previous_names)
    for spec in route_specs:
        app.add_api_route(**spec)
    _prioritize_generated_routes(app, route_names)


def _mount_cached_registration_plan(app: FastAPI, plan: _CachedRegistrationPlan) -> None:
    _remove_generated_routes(app, set(_GENERATED_ROUTE_STATE["route_names"]))
    for cached_route in plan.routes:
        route = copy(cached_route)
        route.dependency_overrides_provider = app.router.dependency_overrides_provider
        route.app = request_response(route.get_route_handler())
        app.router.routes.append(route)
    _prioritize_generated_routes(app, set(plan.route_names))
    app.router._mark_routes_changed()


def _swap_registration_plan(
    app: FastAPI,
    route_specs: list[dict[str, object]],
    route_names: set[str],
) -> None:
    previous_routes = list(app.router.routes)
    previous_openapi_schema = app.openapi_schema
    try:
        _mount_registration_plan(app, route_specs, route_names)
        app.openapi_schema = None
    except Exception:
        app.router.routes = previous_routes
        app.openapi_schema = previous_openapi_schema
        raise


def _swap_cached_registration_plan(app: FastAPI, plan: _CachedRegistrationPlan) -> None:
    previous_routes = list(app.router.routes)
    previous_openapi_schema = app.openapi_schema
    try:
        _mount_cached_registration_plan(app, plan)
        app.openapi_schema = None
    except Exception:
        app.router.routes = previous_routes
        app.openapi_schema = previous_openapi_schema
        raise


def _store_registration_state(
    *,
    route_names: set[str],
    version_state: dict[str, dict[str, object]],
    cache_path: FilePath,
    cache_source: str,
) -> None:
    _GENERATED_ROUTE_STATE["route_names"] = route_names
    _GENERATED_ROUTE_STATE["versions"] = version_state
    _GENERATED_ROUTE_STATE["cache_path"] = str(cache_path)
    _GENERATED_ROUTE_STATE["cache_enabled"] = True
    _GENERATED_ROUTE_STATE["loaded_from_cache"] = cache_source == "runtime-cache"


def register_generated_proxmox_routes(
    app: FastAPI,
    *,
    version_tag: str | None = None,
    openapi_document: dict[str, object] | None = None,
    openapi_documents: dict[str, dict[str, object]] | None = None,
    force_rebuild: bool = False,
) -> dict[str, object]:
    """Register runtime-generated live Proxmox proxy routes on the FastAPI app.

    Pass ``force_rebuild=True`` to always rebuild and re-mount the full route set
    even when it is already registered in-process. Callers that generate a new
    schema version at runtime (see ``schema_version_manager``) must force the
    rebuild so the freshly generated routes are picked up; the ASGI lifespan uses
    the default (non-forced) call so repeated startups reuse the mounted set.
    """

    with _GENERATED_ROUTE_STATE_LOCK:
        existing = _existing_registration_result(
            app,
            version_tag=version_tag,
            openapi_document=openapi_document,
            openapi_documents=openapi_documents,
            force_rebuild=force_rebuild,
        )
        if existing is not None:
            return existing

        documents, cache_source = _load_documents_for_registration(
            version_tag=version_tag,
            openapi_document=openapi_document,
            openapi_documents=openapi_documents,
        )
        if not documents:
            raise ProxboxException(
                message="Generated Proxmox OpenAPI schema not found.",
                detail="Run /proxmox/viewer/generate first.",
            )

        documents, document_metadata = _validate_aggregate_registration_limits(documents)
        plan_cache_key = _registration_plan_cache_key(app, document_metadata)
        use_process_plan = not force_rebuild
        cached_plan = (
            _cached_registration_plan(plan_cache_key)
            if use_process_plan and plan_cache_key is not None
            else None
        )
        route_specs: list[dict[str, object]] = []
        if cached_plan is None:
            route_specs, version_state, all_route_names = _build_registration_plan(
                documents,
                document_metadata,
            )
        else:
            version_state = cached_plan.version_state
            all_route_names = set(cached_plan.route_names)
        cache_path = _write_generated_route_cache_from_snapshots(
            documents=dict(sorted(documents.items(), key=lambda item: _version_sort_key(item[0]))),
            alias_version_tag=_GENERATED_ROUTE_STATE["alias_version_tag"],
        )
        if cached_plan is None:
            _swap_registration_plan(app, route_specs, all_route_names)
            if use_process_plan and plan_cache_key is not None:
                _cache_registration_plan(
                    plan_cache_key,
                    _CachedRegistrationPlan(
                        routes=_mounted_generated_routes(app, all_route_names),
                        version_state=version_state,
                        route_names=frozenset(all_route_names),
                    ),
                )
        else:
            _swap_cached_registration_plan(app, cached_plan)
        _store_registration_state(
            route_names=all_route_names,
            version_state=version_state,
            cache_path=cache_path,
            cache_source=cache_source,
        )
        return _registration_result(
            message="Generated Proxmox live routes registered.",
            cache_source=cache_source,
            route_count=len(all_route_names),
        )


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
    cache_path.unlink(missing_ok=True)
    _generated_route_cache_provenance_path().unlink(missing_ok=True)
