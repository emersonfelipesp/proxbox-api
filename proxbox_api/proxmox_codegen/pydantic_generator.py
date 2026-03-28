"""Pydantic v2 model generator from generated OpenAPI schema."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from proxbox_api.proxmox_codegen.utils import extract_path_params, pascal_case, slugify_identifier


def _python_type(schema: dict[str, Any] | None) -> str:
    if not isinstance(schema, dict):
        return "Any"
    schema_type = schema.get("type")
    if schema_type == "string":
        return "str"
    if schema_type == "integer":
        return "int"
    if schema_type == "number":
        return "float"
    if schema_type == "boolean":
        return "bool"
    if schema_type == "array":
        item_type = _python_type(schema.get("items", {}))
        return f"list[{item_type}]"
    if schema_type == "object":
        return "dict[str, Any]"
    return "Any"


def _generate_model_from_schema(model_name: str, schema: dict[str, Any]) -> str:
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required", [])) if isinstance(schema, dict) else set()

    lines = [f"class {model_name}(BaseModel):"]
    if not properties:
        lines.append("    value: dict[str, Any] | None = None")
        return "\n".join(lines)

    for prop_name, prop_schema in sorted(properties.items()):
        if not isinstance(prop_schema, dict):
            prop_schema = {}
        field_name = slugify_identifier(prop_name)
        field_type = _python_type(prop_schema)
        is_required = prop_name in required
        default_expr = "..." if is_required else "None"
        alias_expr = f', alias="{prop_name}"' if field_name != prop_name else ""
        description = prop_schema.get("description")
        description_expr = (
            f", description={description!r}" if isinstance(description, str) and description else ""
        )

        lines.append(
            f"    {field_name}: {field_type}{'' if is_required else ' | None'} = Field({default_expr}{alias_expr}{description_expr})"
        )

    return "\n".join(lines)


def _request_schema_for_operation(path: str, operation: dict[str, Any]) -> dict[str, Any] | None:
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


def generate_pydantic_models_from_openapi(openapi: dict[str, Any]) -> str:
    """Generate a Python module with Pydantic v2 schemas for request/response payloads."""

    lines: list[str] = [
        '"""Generated Pydantic v2 schemas from Proxmox OpenAPI output."""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Any",
        "",
        "from pydantic import BaseModel, ConfigDict, Field",
        "",
        "",
        "class ProxmoxBaseModel(BaseModel):",
        "    model_config = ConfigDict(populate_by_name=True, extra='allow')",
        "",
    ]

    seen_models: set[str] = set()

    for path, path_item in sorted((openapi.get("paths") or {}).items()):
        if not isinstance(path_item, dict):
            continue

        for method, operation in sorted(path_item.items()):
            if not isinstance(operation, dict):
                continue
            if method.upper() not in {"GET", "POST", "PUT", "DELETE"}:
                continue

            operation_id = operation.get("operationId") or f"{method}_{path}"
            base_name = pascal_case(operation_id)

            req_schema = _request_schema_for_operation(path=path, operation=operation)
            if isinstance(req_schema, dict):
                req_model_name = f"{base_name}Request"
                if req_model_name not in seen_models:
                    seen_models.add(req_model_name)
                    lines.append(
                        _generate_model_from_schema(req_model_name, req_schema).replace(
                            "(BaseModel)", "(ProxmoxBaseModel)"
                        )
                    )
                    lines.append("")

            resp_schema = (
                operation.get("responses", {})
                .get("200", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema")
            )
            if isinstance(resp_schema, dict):
                resp_model_name = f"{base_name}Response"
                if resp_model_name not in seen_models:
                    seen_models.add(resp_model_name)
                    lines.append(
                        _generate_model_from_schema(resp_model_name, resp_schema).replace(
                            "(BaseModel)", "(ProxmoxBaseModel)"
                        )
                    )
                    lines.append("")

    if not seen_models:
        lines.append("class GeneratedPlaceholder(ProxmoxBaseModel):")
        lines.append("    value: str = 'no-models-generated'")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
