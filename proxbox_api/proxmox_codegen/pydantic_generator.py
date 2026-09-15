"""Pydantic v2 model generator from generated OpenAPI schema."""

from __future__ import annotations

import keyword
from copy import deepcopy
from dataclasses import dataclass
from typing import Iterator

from pydantic import BaseModel, ConfigDict, Field, RootModel, create_model

from proxbox_api.proxmox_codegen.security import (
    MAX_SCHEMA_DEPTH,
    OpenAPIDocumentValidation,
    SchemaLimitError,
    SchemaShapeError,
    iter_openapi_schemas,
    snapshot_openapi_document,
    validate_openapi_document_limits,
    validate_openapi_model_shapes,
)
from proxbox_api.proxmox_codegen.utils import extract_path_params, pascal_case, slugify_identifier

_PYDANTIC_RESERVED_FIELD_NAMES = {"schema"}
_PYDANTIC_MODEL_ATTRIBUTES = frozenset(dir(BaseModel)) | frozenset(dir(RootModel))


@dataclass(frozen=True, slots=True)
class _ResolvedPythonType:
    source: str
    annotation: object


def _resolved_schema(schema: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(schema, dict):
        return None
    one_of = schema.get("oneOf")
    if isinstance(one_of, list) and one_of:
        first = one_of[0]
        if isinstance(first, dict):
            return first
    return schema


def _resolve_python_type(schema: dict[str, object] | None) -> _ResolvedPythonType:
    array_depth = 0
    resolved = _resolved_schema(schema)
    while isinstance(resolved, dict) and resolved.get("type") == "array":
        array_depth += 1
        if array_depth > MAX_SCHEMA_DEPTH:
            raise SchemaLimitError(
                f"OpenAPI schema depth exceeds MAX_SCHEMA_DEPTH ({MAX_SCHEMA_DEPTH})."
            )
        resolved = _resolved_schema(resolved.get("items"))

    schema_type = resolved.get("type") if isinstance(resolved, dict) else None
    if schema_type == "null":
        result = _ResolvedPythonType("None", type(None))
    elif schema_type == "string":
        result = _ResolvedPythonType("str", str)
    elif schema_type == "integer":
        result = _ResolvedPythonType("int", int)
    elif schema_type == "number":
        result = _ResolvedPythonType("float", float)
    elif schema_type == "boolean":
        result = _ResolvedPythonType("bool", bool)
    elif schema_type == "object":
        result = _ResolvedPythonType("dict[str, object]", dict[str, object])
    else:
        result = _ResolvedPythonType("object", object)

    for _ in range(array_depth):
        result = _ResolvedPythonType(f"list[{result.source}]", list[result.annotation])
    return result


def _validated_identifier(value: str, *, kind: str) -> str:
    if value.isidentifier() and not keyword.iskeyword(value):
        return value
    raise SchemaShapeError(f"Generated {kind} is not a valid Python identifier: {value!r}")


def _field_name(prop_name: str) -> str:
    field_name = slugify_identifier(prop_name)
    if field_name in _PYDANTIC_RESERVED_FIELD_NAMES:
        field_name = f"{field_name}_"
    if (
        prop_name.strip().startswith("__")
        or field_name.startswith(("model_", "__"))
        or field_name in _PYDANTIC_MODEL_ATTRIBUTES
    ):
        raise SchemaShapeError(
            f"OpenAPI property name resolves to a reserved Pydantic field: {prop_name!r}."
        )
    return _validated_identifier(field_name, kind="field name")


def _validate_property_names(schema: dict[str, object]) -> None:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    normalized: dict[str, str] = {}
    for prop_name in properties:
        if not isinstance(prop_name, str):
            raise SchemaShapeError(f"OpenAPI property name must be a string: {prop_name!r}")
        field_name = _field_name(prop_name)
        previous = normalized.get(field_name)
        if previous is not None and previous != prop_name:
            raise SchemaShapeError(
                "OpenAPI property names collide after Python normalization: "
                f"{previous!r} and {prop_name!r}."
            )
        normalized[field_name] = prop_name


def _validate_model_schema_shapes(document: dict[str, object]) -> None:
    for schema in iter_openapi_schemas(document):
        _validate_property_names(schema)


def _prepared_model_schemas(
    document: dict[str, object],
    validation: OpenAPIDocumentValidation | None = None,
) -> list[tuple[str, dict[str, object]]]:
    document, _, _ = snapshot_openapi_document(document, validation)
    if validation is None:
        validate_openapi_document_limits(document)
    validate_openapi_model_shapes(document)
    model_schemas = list(_iter_model_schemas(document))
    _validate_model_schema_shapes(document)
    return model_schemas


def _field_description(schema: dict[str, object]) -> str | None:
    description = schema.get("description")
    return description if isinstance(description, str) and description else None


def _render_field_call(
    default: object,
    *,
    alias: str | None = None,
    description: str | None = None,
) -> str:
    arguments = [repr(default)]
    if alias is not None:
        arguments.append(f"alias={alias!r}")
    if description is not None:
        arguments.append(f"description={description!r}")
    return f"Field({', '.join(arguments)})"


def _generate_object_model(model_name: str, schema: dict[str, object]) -> str:
    model_name = _validated_identifier(model_name, kind="class name")
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required", [])) if isinstance(schema, dict) else set()

    if not isinstance(properties, dict) or not properties:
        return _generate_root_model(model_name, {"type": "object"})

    lines = [f"class {model_name}(BaseModel):"]

    for prop_name, prop_schema in sorted(properties.items()):
        if not isinstance(prop_name, str):
            raise SchemaShapeError(f"OpenAPI property name must be a string: {prop_name!r}")
        if not isinstance(prop_schema, dict):
            prop_schema = {}
        field_name = _field_name(prop_name)
        field_type = _resolve_python_type(prop_schema).source
        is_required = prop_name in required
        default = ... if is_required else None
        field_call = _render_field_call(
            default,
            alias=prop_name if field_name != prop_name else None,
            description=_field_description(prop_schema),
        )

        lines.append(
            f"    {field_name}: {field_type}{'' if is_required else ' | None'} = {field_call}"
        )

    return "\n".join(lines)


def _generate_root_model(model_name: str, schema: dict[str, object]) -> str:
    model_name = _validated_identifier(model_name, kind="class name")
    field_type = _resolve_python_type(schema).source
    field_call = _render_field_call(..., description=_field_description(schema))
    return "\n".join(
        [
            f"class {model_name}(RootModel[{field_type}]):",
            f"    root: {field_type} = {field_call}",
        ]
    )


def _generate_model_from_schema(model_name: str, schema: dict[str, object]) -> list[str]:
    schema = _resolved_schema(schema) or {}
    if (
        schema.get("type") == "array"
        and isinstance(schema.get("items"), dict)
        and schema["items"].get("type") == "object"
        and schema["items"].get("properties")
    ):
        item_model_name = _validated_identifier(
            f"{model_name}Item",
            kind="class name",
        )
        model_name = _validated_identifier(model_name, kind="class name")
        field_call = _render_field_call(..., description=_field_description(schema))
        return [
            _generate_object_model(item_model_name, schema["items"]),
            "\n".join(
                [
                    f"class {model_name}(RootModel[list[{item_model_name}]]):",
                    f"    root: list[{item_model_name}] = {field_call}",
                ]
            ),
        ]
    if schema.get("type") == "object":
        return [_generate_object_model(model_name, schema)]
    return [_generate_root_model(model_name, schema)]


def _request_schema_for_operation(
    path: str, operation: dict[str, object]
) -> dict[str, object] | None:
    """Return request-body schema excluding path parameters for runtime proxy models."""

    request_schema = (
        operation.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )
    if not isinstance(request_schema, dict):
        return None

    path_params = set(extract_path_params(path))
    if not path_params:
        return request_schema

    schema = deepcopy(request_schema)
    properties = schema.get("properties")
    if isinstance(properties, dict):
        schema["properties"] = {
            name: value for name, value in properties.items() if name not in path_params
        }
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [name for name in required if name not in path_params]
    return schema


def _response_schema_for_operation(operation: dict[str, object]) -> dict[str, object] | None:
    response_schema = (
        operation.get("responses", {})
        .get("200", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )
    return response_schema if isinstance(response_schema, dict) else None


def _model_names_for_schema(model_name: str, schema: dict[str, object]) -> tuple[str, ...]:
    resolved = _resolved_schema(schema) or {}
    if (
        resolved.get("type") == "array"
        and isinstance(resolved.get("items"), dict)
        and resolved["items"].get("type") == "object"
        and resolved["items"].get("properties")
    ):
        return model_name, f"{model_name}Item"
    return (model_name,)


def _iter_operations(
    openapi: dict[str, object],
) -> Iterator[tuple[str, str, dict[str, object]]]:
    paths = openapi.get("paths") or {}
    if not isinstance(paths, dict):
        return

    for path, path_item in sorted(paths.items()):
        if not isinstance(path, str) or not isinstance(path_item, dict):
            continue

        for method, operation in sorted(path_item.items()):
            if not isinstance(method, str) or not isinstance(operation, dict):
                continue
            if method.upper() not in {"GET", "POST", "PUT", "DELETE"}:
                continue
            yield path, method, operation


def _iter_model_schemas(
    openapi: dict[str, object],
) -> Iterator[tuple[str, dict[str, object]]]:
    seen_models: set[str] = set()

    for path, method, operation in _iter_operations(openapi):
        operation_id = operation.get("operationId") or f"{method}_{path}"
        if not isinstance(operation_id, str):
            raise SchemaShapeError(f"OpenAPI operationId must be a string: {operation_id!r}")
        base_name = pascal_case(operation_id)

        schemas = (
            (f"{base_name}Request", _request_schema_for_operation(path, operation)),
            (f"{base_name}Response", _response_schema_for_operation(operation)),
        )
        for model_name, schema in schemas:
            if not isinstance(schema, dict):
                continue
            names = _model_names_for_schema(model_name, schema)
            for name in names:
                _validated_identifier(name, kind="class name")
            duplicates = seen_models.intersection(names)
            if duplicates:
                duplicate = sorted(duplicates)[0]
                raise SchemaShapeError(f"Duplicate operation-derived model name: {duplicate!r}.")
            seen_models.update(names)
            yield model_name, schema


def _optional_annotation(annotation: object) -> object:
    return annotation | None


def _field_info(
    default: object,
    *,
    alias: str | None = None,
    description: str | None = None,
) -> object:
    return Field(default, alias=alias, description=description)


def _build_object_model(
    model_name: str,
    schema: dict[str, object],
    *,
    base_model: type[BaseModel],
) -> type[BaseModel]:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or not properties:
        return _build_root_model(model_name, {"type": "object"})

    required = set(schema.get("required", []))
    fields: dict[str, tuple[object, object]] = {}
    for prop_name, prop_schema in sorted(properties.items()):
        if not isinstance(prop_name, str):
            raise SchemaShapeError(f"OpenAPI property name must be a string: {prop_name!r}")
        if not isinstance(prop_schema, dict):
            prop_schema = {}
        field_name = _field_name(prop_name)
        resolved_type = _resolve_python_type(prop_schema).annotation
        is_required = prop_name in required
        annotation = resolved_type if is_required else _optional_annotation(resolved_type)
        fields[field_name] = (
            annotation,
            _field_info(
                ... if is_required else None,
                alias=prop_name if field_name != prop_name else None,
                description=_field_description(prop_schema),
            ),
        )

    return create_model(model_name, __base__=base_model, **fields)


def _build_root_model(
    model_name: str,
    schema: dict[str, object],
    *,
    annotation: object | None = None,
) -> type[BaseModel]:
    resolved_annotation = annotation or _resolve_python_type(schema).annotation
    root_base = RootModel[resolved_annotation]
    return create_model(
        model_name,
        __base__=root_base,
        root=(resolved_annotation, _field_info(..., description=_field_description(schema))),
    )


def _build_models_from_schema(
    model_name: str,
    schema: dict[str, object],
    *,
    base_model: type[BaseModel],
) -> dict[str, type[BaseModel]]:
    resolved = _resolved_schema(schema) or {}
    items = resolved.get("items")
    if (
        resolved.get("type") == "array"
        and isinstance(items, dict)
        and items.get("type") == "object"
        and items.get("properties")
    ):
        item_model_name = f"{model_name}Item"
        item_model = _build_object_model(item_model_name, items, base_model=base_model)
        root_model = _build_root_model(
            model_name,
            resolved,
            annotation=list[item_model],
        )
        return {item_model_name: item_model, model_name: root_model}
    if resolved.get("type") == "object":
        return {
            model_name: _build_object_model(model_name, resolved, base_model=base_model),
        }
    return {model_name: _build_root_model(model_name, resolved)}


def build_pydantic_models_from_openapi(
    document: dict[str, object],
    *,
    validation: OpenAPIDocumentValidation | None = None,
) -> dict[str, type[BaseModel]]:
    """Build Pydantic v2 schemas without evaluating generated Python source."""

    model_schemas = _prepared_model_schemas(document, validation)
    base_model = create_model(
        "ProxmoxBaseModel",
        __config__=ConfigDict(
            populate_by_name=True,
            extra="allow",
            protected_namespaces=(),
        ),
    )
    models: dict[str, type[BaseModel]] = {"ProxmoxBaseModel": base_model}
    for model_name, schema in model_schemas:
        models.update(_build_models_from_schema(model_name, schema, base_model=base_model))

    if len(models) == 1:
        models["GeneratedPlaceholder"] = create_model(
            "GeneratedPlaceholder",
            __base__=base_model,
            value=(str, "no-models-generated"),
        )
    return models


def generate_pydantic_models_from_openapi(openapi: dict[str, object]) -> str:
    """Generate a Python module with Pydantic v2 schemas for request/response payloads."""

    model_schemas = _prepared_model_schemas(openapi)
    lines: list[str] = [
        '"""Generated Pydantic v2 schemas from Proxmox OpenAPI output."""',
        "",
        "from __future__ import annotations",
        "",
        "import warnings",
        "",
        "from pydantic import BaseModel, ConfigDict, Field, RootModel",
        "",
        "warnings.filterwarnings('ignore', message='Field name \"schema\".*shadows an attribute.*')",
        "",
        "class ProxmoxBaseModel(BaseModel):",
        "    model_config = ConfigDict(populate_by_name=True, extra='allow', protected_namespaces=())",
        "",
    ]

    model_count = 0
    for model_name, schema in model_schemas:
        model_count += 1
        for block in _generate_model_from_schema(model_name, schema):
            lines.append(block.replace("(BaseModel)", "(ProxmoxBaseModel)"))
            lines.append("")

    if model_count == 0:
        lines.append("class GeneratedPlaceholder(ProxmoxBaseModel):")
        lines.append(f"    value: str = {'no-models-generated'!r}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
