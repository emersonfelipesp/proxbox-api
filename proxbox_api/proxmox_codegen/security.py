"""Validation and path-containment helpers for Proxmox code generation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Iterator

VERSION_TAG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_VERSION_TAG_RE = re.compile(VERSION_TAG_PATTERN)

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_SCHEMA_DEPTH = 32
MAX_PATHS = 4096
MAX_OPERATIONS = 16384
MAX_PROPERTIES_PER_SCHEMA = 512
MAX_MODELS = 8192
MAX_STRING_LENGTH = 4096
MAX_ELIGIBLE_VERSIONS = 8
MAX_AGGREGATE_DOCUMENT_BYTES = 32 * 1024 * 1024
MAX_AGGREGATE_MODELS = 16384
MAX_AGGREGATE_ROUTES = 32768

_HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put", "trace"})
_NESTED_SCHEMA_KEYS = ("additionalProperties", "items", "not")
_NESTED_SCHEMA_LIST_KEYS = ("allOf", "anyOf", "oneOf", "prefixItems")


class SchemaValidationError(ValueError):
    """Base class for rejected generated OpenAPI documents."""


class SchemaLimitError(SchemaValidationError):
    """Raised when a generated OpenAPI document exceeds a resource limit."""


class SchemaShapeError(SchemaValidationError):
    """Raised when a generated OpenAPI document has an unsafe shape."""


@dataclass(frozen=True)
class OpenAPIDocumentValidation:
    """Bind a validated document to its deterministic digest and byte count."""

    digest: str
    byte_count: int
    _document: dict[str, object] = field(repr=False, compare=False)

    def applies_to(self, document: dict[str, object]) -> bool:
        """Return whether this receipt belongs to the exact validated object."""

        return self._document is document


def validate_version_tag(tag: str) -> str:
    """Return a valid codegen version tag or raise ``ValueError``."""

    if not isinstance(tag, str) or not _VERSION_TAG_RE.fullmatch(tag) or tag in {".", ".."}:
        raise ValueError(
            "version_tag must start with an ASCII letter or digit and contain only "
            "ASCII letters, digits, '.', '_', or '-' (maximum 64 characters)."
        )
    return tag


def resolve_contained(base: Path, *parts: str) -> Path:
    """Resolve a descendant path and reject paths outside the resolved base."""

    resolved_base = base.resolve()
    resolved_path = resolved_base.joinpath(*parts).resolve()
    if not resolved_path.is_relative_to(resolved_base):
        raise ValueError(f"Resolved path escapes the configured base directory: {resolved_path}")
    return resolved_path


def _iter_document_nodes(document: object) -> Iterator[tuple[str | None, object]]:
    stack: list[tuple[str | None, object]] = [(None, document)]
    seen: set[int] = set()
    while stack:
        key, value = stack.pop()
        yield key, value
        if isinstance(value, dict):
            if id(value) in seen:
                continue
            seen.add(id(value))
            stack.extend((str(child_key), child) for child_key, child in value.items())
        elif isinstance(value, list):
            if id(value) in seen:
                continue
            seen.add(id(value))
            stack.extend((key, child) for child in value)


def _schema_roots(document: dict[str, object]) -> Iterator[dict[str, object]]:
    seen: set[int] = set()
    for key, value in _iter_document_nodes(document):
        if key != "schema" or not isinstance(value, dict) or id(value) in seen:
            continue
        seen.add(id(value))
        yield value

    components = document.get("components")
    schemas = components.get("schemas") if isinstance(components, dict) else None
    if isinstance(schemas, dict):
        for value in schemas.values():
            if isinstance(value, dict) and id(value) not in seen:
                seen.add(id(value))
                yield value


def _nested_schemas(schema: dict[str, object]) -> Iterator[dict[str, object]]:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        yield from (value for value in properties.values() if isinstance(value, dict))
    for key in _NESTED_SCHEMA_KEYS:
        value = schema.get(key)
        if isinstance(value, dict):
            yield value
    for key in _NESTED_SCHEMA_LIST_KEYS:
        value = schema.get(key)
        if isinstance(value, list):
            yield from (item for item in value if isinstance(item, dict))


def iter_openapi_schemas(document: dict[str, object]) -> Iterator[dict[str, object]]:
    """Iterate every distinct schema object without recursive traversal."""

    stack = list(_schema_roots(document))
    seen: set[int] = set()
    while stack:
        schema = stack.pop()
        if id(schema) in seen:
            continue
        seen.add(id(schema))
        yield schema
        stack.extend(_nested_schemas(schema))


def _validate_schema_tree(root: dict[str, object]) -> None:
    stack: list[tuple[dict[str, object], int]] = [(root, 1)]
    seen_depths: dict[int, int] = {}
    while stack:
        schema, depth = stack.pop()
        if depth > MAX_SCHEMA_DEPTH:
            raise SchemaLimitError(
                f"OpenAPI schema depth exceeds MAX_SCHEMA_DEPTH ({MAX_SCHEMA_DEPTH})."
            )
        previous_depth = seen_depths.get(id(schema))
        if previous_depth is not None and previous_depth <= depth:
            continue
        seen_depths[id(schema)] = depth
        properties = schema.get("properties")
        if isinstance(properties, dict) and len(properties) > MAX_PROPERTIES_PER_SCHEMA:
            raise SchemaLimitError(
                "OpenAPI schema property count exceeds "
                f"MAX_PROPERTIES_PER_SCHEMA ({MAX_PROPERTIES_PER_SCHEMA})."
            )
        stack.extend((child, depth + 1) for child in _nested_schemas(schema))


def _validate_schema_limits(document: dict[str, object]) -> None:
    for root in _schema_roots(document):
        _validate_schema_tree(root)


def _require_object(container: dict[str, object], key: str, *, context: str) -> None:
    if key in container and not isinstance(container[key], dict):
        raise SchemaShapeError(f"OpenAPI {context}.{key} must be an object.")


def _validate_schema_object_fields(schema: dict[str, object]) -> None:
    schema_type = schema.get("type")
    if schema_type is not None and not isinstance(schema_type, str):
        raise SchemaShapeError("OpenAPI schema type must be a string.")

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, dict):
            raise SchemaShapeError("OpenAPI schema properties must be an object.")
        if any(not isinstance(name, str) for name in properties):
            raise SchemaShapeError("OpenAPI schema property names must be strings.")
        if any(not isinstance(value, dict) for value in properties.values()):
            raise SchemaShapeError("OpenAPI schema properties must contain schema objects.")


def _validate_schema_list_fields(schema: dict[str, object]) -> None:
    required = schema.get("required")
    if required is not None and (
        not isinstance(required, list) or any(not isinstance(name, str) for name in required)
    ):
        raise SchemaShapeError("OpenAPI schema required must be a list of strings.")

    if "items" in schema and not isinstance(schema["items"], dict):
        raise SchemaShapeError("OpenAPI schema items must be an object.")
    if schema.get("enum") is not None and not isinstance(schema["enum"], list):
        raise SchemaShapeError("OpenAPI schema enum must be a list.")
    for key in _NESTED_SCHEMA_LIST_KEYS:
        value = schema.get(key)
        if value is not None and (
            not isinstance(value, list) or any(not isinstance(item, dict) for item in value)
        ):
            raise SchemaShapeError(f"OpenAPI schema {key} must be a list of schema objects.")


def _validate_schema_shape(schema: dict[str, object]) -> None:
    _validate_schema_object_fields(schema)
    _validate_schema_list_fields(schema)


def _validate_media_type_container(container: dict[str, object], *, context: str) -> None:
    _require_object(container, "content", context=context)
    content = container.get("content")
    if not isinstance(content, dict):
        return
    for media_type, media in content.items():
        if not isinstance(media_type, str) or not isinstance(media, dict):
            raise SchemaShapeError(f"OpenAPI {context}.content must contain media objects.")
        _require_object(media, "schema", context=f"{context}.content.{media_type}")


def _validate_parameter_shapes(operation: dict[str, object]) -> None:
    parameters = operation.get("parameters")
    if parameters is None:
        return
    if not isinstance(parameters, list) or any(not isinstance(item, dict) for item in parameters):
        raise SchemaShapeError("OpenAPI operation parameters must be a list of objects.")
    for parameter in parameters:
        _require_object(parameter, "schema", context="operation parameter")
        required = parameter.get("required")
        if required is not None and not isinstance(required, bool):
            raise SchemaShapeError("OpenAPI operation parameter required must be a boolean.")


def _validate_operation_shape(operation: dict[str, object]) -> None:
    _validate_parameter_shapes(operation)
    request_body = operation.get("requestBody")
    if request_body is not None:
        if not isinstance(request_body, dict):
            raise SchemaShapeError("OpenAPI operation requestBody must be an object.")
        _validate_media_type_container(request_body, context="operation requestBody")
    responses = operation.get("responses")
    if responses is None:
        return
    if not isinstance(responses, dict):
        raise SchemaShapeError("OpenAPI operation responses must be an object.")
    for status, response in responses.items():
        if not isinstance(status, str) or not isinstance(response, dict):
            raise SchemaShapeError("OpenAPI operation responses must contain response objects.")
        _validate_media_type_container(response, context=f"operation response {status}")


def _validate_operation_shapes(document: dict[str, object]) -> None:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise SchemaShapeError("OpenAPI paths must be an object.")
    for path, path_item in paths.items():
        if not isinstance(path, str) or not isinstance(path_item, dict):
            raise SchemaShapeError("OpenAPI paths must contain path-item objects.")
        for method, operation in path_item.items():
            if not isinstance(method, str) or method.lower() not in _HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                raise SchemaShapeError("OpenAPI operations must be objects.")
            _validate_operation_shape(operation)


def _validate_schema_shapes(document: dict[str, object]) -> None:
    components = document.get("components")
    if components is not None:
        if not isinstance(components, dict):
            raise SchemaShapeError("OpenAPI components must be an object.")
        _require_object(components, "schemas", context="components")
        schemas = components.get("schemas")
        if isinstance(schemas, dict) and any(
            not isinstance(value, dict) for value in schemas.values()
        ):
            raise SchemaShapeError("OpenAPI components.schemas must contain schema objects.")
    _validate_operation_shapes(document)
    for schema in iter_openapi_schemas(document):
        _validate_schema_shape(schema)


def validate_openapi_model_shapes(document: dict[str, object]) -> None:
    """Reject malformed containers and scalars consumed by model construction."""

    if not isinstance(document, dict):
        raise SchemaShapeError("OpenAPI document must be an object.")
    _validate_schema_shapes(document)


def _validate_path_and_operation_limits(document: dict[str, object]) -> int:
    paths = document.get("paths")
    if not isinstance(paths, dict):
        return 0
    if len(paths) > MAX_PATHS:
        raise SchemaLimitError(f"OpenAPI path count exceeds MAX_PATHS ({MAX_PATHS}).")
    operation_count = sum(
        1
        for path_item in paths.values()
        if isinstance(path_item, dict)
        for method in path_item
        if isinstance(method, str) and method.lower() in _HTTP_METHODS
    )
    if operation_count > MAX_OPERATIONS:
        raise SchemaLimitError(
            f"OpenAPI operation count exceeds MAX_OPERATIONS ({MAX_OPERATIONS})."
        )
    return operation_count


def _operation_schema(operation: dict[str, object], *keys: str) -> dict[str, object] | None:
    value: object = operation
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value if isinstance(value, dict) else None


def _schema_model_count(schema: dict[str, object] | None) -> int:
    if schema is None:
        return 0
    items = schema.get("items")
    return (
        2
        if schema.get("type") == "array"
        and isinstance(items, dict)
        and items.get("type") == "object"
        and items.get("properties")
        else 1
    )


def count_openapi_models(document: dict[str, object]) -> int:
    """Return the number of operation-derived models before construction."""

    paths = document.get("paths")
    if not isinstance(paths, dict):
        return 0
    model_count = 0
    for path_item in paths.values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if not isinstance(method, str) or method.lower() not in _HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                continue
            model_count += _schema_model_count(
                _operation_schema(operation, "requestBody", "content", "application/json", "schema")
            )
            model_count += _schema_model_count(
                _operation_schema(
                    operation, "responses", "200", "content", "application/json", "schema"
                )
            )
    return model_count


def _validate_model_limit(document: dict[str, object]) -> None:
    if count_openapi_models(document) > MAX_MODELS:
        raise SchemaLimitError(f"OpenAPI generated model count exceeds MAX_MODELS ({MAX_MODELS}).")


def _validate_string_limits(document: dict[str, object]) -> None:
    for key, value in _iter_document_nodes(document):
        if key in {"description", "title"} and isinstance(value, str):
            if len(value) > MAX_STRING_LENGTH:
                raise SchemaLimitError(
                    f"OpenAPI {key} exceeds MAX_STRING_LENGTH ({MAX_STRING_LENGTH})."
                )
        if key == "enum" and isinstance(value, list):
            if any(isinstance(item, str) and len(item) > MAX_STRING_LENGTH for item in value):
                raise SchemaLimitError(
                    f"OpenAPI enum value exceeds MAX_STRING_LENGTH ({MAX_STRING_LENGTH})."
                )


def openapi_document_bytes(document: dict[str, object]) -> bytes:
    """Return deterministic document bytes or a typed validation error."""

    try:
        return json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except RecursionError as error:
        raise SchemaLimitError(
            f"OpenAPI document nesting exceeds MAX_SCHEMA_DEPTH ({MAX_SCHEMA_DEPTH})."
        ) from error
    except (RuntimeError, TypeError, ValueError) as error:
        raise SchemaShapeError("OpenAPI document must be JSON serializable.") from error


def snapshot_openapi_document(
    document: dict[str, object],
    validation: OpenAPIDocumentValidation | None = None,
) -> tuple[dict[str, object], str, int]:
    """Return a private canonical snapshot and the digest of its exact bytes."""

    if validation is not None and not validation.applies_to(document):
        raise SchemaShapeError("OpenAPI validation receipt does not match the document.")

    encoded = openapi_document_bytes(document)
    digest = sha256(encoded).hexdigest()
    byte_count = len(encoded)
    if validation is not None and (
        byte_count != validation.byte_count or digest != validation.digest
    ):
        raise SchemaShapeError(
            "OpenAPI validation receipt does not match the current document content."
        )
    if byte_count > MAX_DOCUMENT_BYTES:
        raise SchemaLimitError(
            f"OpenAPI document exceeds MAX_DOCUMENT_BYTES ({MAX_DOCUMENT_BYTES})."
        )

    try:
        snapshot = json.loads(encoded)
    except RecursionError as error:  # pragma: no cover - json.dumps has the same depth bound
        raise SchemaLimitError(
            f"OpenAPI document nesting exceeds MAX_SCHEMA_DEPTH ({MAX_SCHEMA_DEPTH})."
        ) from error
    except (UnicodeError, ValueError) as error:  # pragma: no cover - encoded by json.dumps above
        raise SchemaShapeError("OpenAPI document must be JSON serializable.") from error
    if not isinstance(snapshot, dict):  # pragma: no cover - encoded input is a dictionary
        raise SchemaShapeError("OpenAPI document must be an object.")
    return snapshot, digest, byte_count


def _validate_document_bytes(document: dict[str, object]) -> bytes:
    encoded = openapi_document_bytes(document)
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise SchemaLimitError(
            f"OpenAPI document exceeds MAX_DOCUMENT_BYTES ({MAX_DOCUMENT_BYTES})."
        )
    return encoded


def _validate_openapi_document_uncached(
    document: dict[str, object],
) -> OpenAPIDocumentValidation:
    """Run the complete resource validation in its security-sensitive order."""

    _validate_schema_limits(document)
    _validate_path_and_operation_limits(document)
    _validate_model_limit(document)
    _validate_string_limits(document)
    encoded = _validate_document_bytes(document)
    return OpenAPIDocumentValidation(
        digest=sha256(encoded).hexdigest(),
        byte_count=len(encoded),
        _document=document,
    )


def validate_openapi_document_limits(
    document: dict[str, object],
) -> OpenAPIDocumentValidation:
    """Reject OpenAPI documents that exceed bounded runtime resources."""

    if not isinstance(document, dict):
        raise SchemaShapeError("OpenAPI document must be an object.")
    return _validate_openapi_document_uncached(document)
