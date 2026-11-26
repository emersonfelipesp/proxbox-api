#!/usr/bin/env python3
"""Generate the proxbox-api/netbox-proxbox REST contract fixture from OpenAPI."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("openapi", type=Path, help="NetBox OpenAPI JSON document")
    parser.add_argument("output", type=Path, help="fixture JSON to write")
    parser.add_argument("--plugin-version", required=True)
    parser.add_argument("--plugin-wheel-sha256", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--netbox-version", required=True)
    parser.add_argument("--generation-command", required=True)
    parser.add_argument("--generation-date", default=date.today().isoformat())
    return parser.parse_args()


def _resolve_reference(document: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ValueError(f"cannot resolve non-local OpenAPI reference: {reference}")
    value: Any = document
    for encoded in reference[2:].split("/"):
        part = encoded.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"cannot resolve OpenAPI reference: {reference}")
        value = value[part]
    if not isinstance(value, dict):
        raise ValueError(f"OpenAPI reference does not resolve to an object: {reference}")
    return value


def _resolve_object(document: dict[str, Any], value: Any, kind: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"OpenAPI {kind} must be an object")
    reference = value.get("$ref")
    if reference is None:
        return value
    if not isinstance(reference, str):
        raise ValueError(f"OpenAPI {kind} $ref must be a string")
    return _resolve_reference(document, reference)


def _parameter_names(
    document: dict[str, Any], operation: dict[str, Any], path_item: dict[str, Any]
) -> list[str]:
    parameters = [*path_item.get("parameters", []), *operation.get("parameters", [])]
    return sorted(
        {
            str(resolved["name"])
            for parameter in parameters
            if (resolved := _resolve_object(document, parameter, "parameter"))
            and resolved.get("in") == "query"
            and isinstance(resolved.get("name"), str)
        }
    )


_HTTP_METHODS = ("delete", "get", "head", "options", "patch", "post", "put")


def _plugin_contract(document: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    """Return every plugin path with each served method and its query parameters."""
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI document has no paths object")
    contract: dict[str, dict[str, list[str]]] = {}
    for path, raw_path_item in sorted(paths.items()):
        if not str(path).startswith("/api/plugins/proxbox/"):
            continue
        path_item = _resolve_object(document, raw_path_item, "path item")
        methods = {
            method.upper(): _parameter_names(document, operation, path_item)
            for method in _HTTP_METHODS
            if isinstance(operation := path_item.get(method), dict)
        }
        if methods:
            contract[str(path)] = methods
    if not any("GET" in methods for methods in contract.values()):
        raise ValueError("OpenAPI document contains no proxbox plugin GET operations")
    return contract


def generate_fixture(arguments: argparse.Namespace) -> dict[str, Any]:
    """Return a validated fixture document from parsed command-line arguments."""
    document = json.loads(arguments.openapi.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("OpenAPI document root must be an object")
    return {
        "metadata": {
            "plugin_version": arguments.plugin_version,
            "plugin_wheel_sha256": arguments.plugin_wheel_sha256,
            "image": arguments.image,
            "image_digest": arguments.image_digest,
            "netbox_version": arguments.netbox_version,
            "generation_date": arguments.generation_date,
            "generation_command": arguments.generation_command,
        },
        "paths": _plugin_contract(document),
    }


def main() -> int:
    """Write the deterministic JSON fixture."""
    arguments = _arguments()
    fixture = generate_fixture(arguments)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
