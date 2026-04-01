"""Model validation enhancements for Pydantic code generation."""

from __future__ import annotations

from typing import Any


def generate_field_validators(model_name: str, schema: dict[str, Any]) -> list[str]:
    """Generate Pydantic v2 field validators for common validation patterns.

    Args:
        model_name: Name of the model being generated
        schema: OpenAPI schema for the model

    Returns:
        List of validator method source lines
    """
    validators = []
    properties = schema.get("properties", {}) or {}

    for prop_name, prop_schema in properties.items():
        if not isinstance(prop_schema, dict):
            continue

        # Skip if no validation needed
        if not any(
            k in prop_schema
            for k in ["pattern", "minLength", "maxLength", "minimum", "maximum", "enum"]
        ):
            continue

        validator_lines = _generate_field_validator(prop_name, prop_schema)
        if validator_lines:
            validators.extend(validator_lines)

    return validators


def _generate_field_validator(prop_name: str, prop_schema: dict[str, Any]) -> list[str]:
    """Generate a single field validator.

    Args:
        prop_name: Property name
        prop_schema: Property schema

    Returns:
        Validator method source lines
    """
    lines = []
    schema_type = prop_schema.get("type")

    # String validators
    if schema_type == "string":
        pattern = prop_schema.get("pattern")
        min_length = prop_schema.get("minLength")
        max_length = prop_schema.get("maxLength")
        enum_vals = prop_schema.get("enum")

        if pattern or min_length or max_length or enum_vals:
            validator_name = f"validate_{prop_name}"
            lines.append(f"    @field_validator('{prop_name}')")
            lines.append(f"    @classmethod")
            lines.append(f"    def {validator_name}(cls, v: str | None) -> str | None:")
            lines.append(f"        if v is None:")
            lines.append(f"            return None")

            if pattern:
                lines.append(f"        import re")
                lines.append(f"        if not re.match(r'{pattern}', v):")
                lines.append(
                    f"            raise ValueError(f'{{prop_name}} does not match pattern: {pattern}')"
                )

            if min_length:
                lines.append(f"        if len(v) < {min_length}:")
                lines.append(
                    f"            raise ValueError(f'{{prop_name}} must be at least {min_length} characters')"
                )

            if max_length:
                lines.append(f"        if len(v) > {max_length}:")
                lines.append(
                    f"            raise ValueError(f'{{prop_name}} must be at most {max_length} characters')"
                )

            if enum_vals:
                allowed = ", ".join(repr(v) for v in enum_vals)
                lines.append(f"        if v not in ({allowed}):")
                lines.append(
                    f"            raise ValueError(f'{{prop_name}} must be one of: {allowed}')"
                )

            lines.append(f"        return v")
            lines.append("")

    # Numeric validators
    elif schema_type in ("integer", "number"):
        minimum = prop_schema.get("minimum")
        maximum = prop_schema.get("maximum")
        enum_vals = prop_schema.get("enum")

        if minimum is not None or maximum is not None or enum_vals:
            validator_name = f"validate_{prop_name}"
            lines.append(f"    @field_validator('{prop_name}')")
            lines.append(f"    @classmethod")
            lines.append(
                f"    def {validator_name}(cls, v: int | float | None) -> int | float | None:"
            )
            lines.append(f"        if v is None:")
            lines.append(f"            return None")

            if minimum is not None:
                lines.append(f"        if v < {minimum}:")
                lines.append(f"            raise ValueError(f'{{prop_name}} must be >= {minimum}')")

            if maximum is not None:
                lines.append(f"        if v > {maximum}:")
                lines.append(f"            raise ValueError(f'{{prop_name}} must be <= {maximum}')")

            if enum_vals:
                allowed = ", ".join(repr(v) for v in enum_vals)
                lines.append(f"        if v not in ({allowed}):")
                lines.append(
                    f"            raise ValueError(f'{{prop_name}} must be one of: {allowed}')"
                )

            lines.append(f"        return v")
            lines.append("")

    return lines


def add_model_docstring(model_name: str, schema: dict[str, Any]) -> str:
    """Generate a docstring for a generated model.

    Args:
        model_name: Model name
        schema: OpenAPI schema

    Returns:
        Docstring with description and field documentation
    """
    lines = [f'    """']

    # Add description
    description = schema.get("description")
    if description:
        lines.append(f"    {description}")
    else:
        lines.append(f"    {model_name} model from Proxmox API.")

    lines.append(f"    ")

    # Add field documentation
    properties = schema.get("properties", {}) or {}
    if properties:
        lines.append(f"    Fields:")
        for prop_name, prop_schema in sorted(properties.items()):
            if isinstance(prop_schema, dict):
                prop_desc = prop_schema.get("description", prop_name)
                lines.append(f"        {prop_name}: {prop_desc}")

    lines.append(f'    """')
    return "\n".join(lines)
