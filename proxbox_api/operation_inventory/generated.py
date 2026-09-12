"""Join mounted generated registrations to their independent OpenAPI identities."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import cast

from .provenance import regular_file, source
from .schema import Generated, InventoryError, Operation, digest, parse

METHODS = ("DELETE", "GET", "POST", "PUT")


def load_documents(root: Path, versions: list[str]) -> dict[str, dict]:
    """Read only bundled artifacts and reject absent/extra generated versions."""
    directory = root / "proxbox_api/generated/proxmox"
    actual = {path.parent.name for path in directory.glob("*/openapi.json")}
    if actual != set(versions):
        raise InventoryError("Bundled generated version set differs from manifest")
    documents = {}
    for version in versions:
        path = regular_file(directory / version / "openapi.json")
        document = parse(path.read_bytes())
        if type(document) is not dict or not cast(dict[str, object], document).get("paths"):
            raise InventoryError("Generated OpenAPI document is missing paths")
        documents[version] = document
    return documents


def _operation_rows(document: dict) -> Iterator[tuple[str, str, dict]]:
    paths = document["paths"]
    if type(paths) is not dict:
        raise InventoryError("Malformed generated paths")
    for path, item in sorted(paths.items()):
        if type(path) is not str or not path.startswith("/") or type(item) is not dict:
            raise InventoryError("Malformed generated path item")
        for method, operation in sorted(cast(dict[str, object], item).items()):
            if method.upper() not in METHODS:
                continue
            if (
                type(operation) is not dict
                or type(cast(dict[str, object], operation).get("operationId")) is not str
            ):
                raise InventoryError("Malformed generated operation")
            yield path, method.upper(), operation


def identities(root: Path, documents: dict[str, dict]) -> dict[str, Generated]:
    """Retain distinct upstream identities for explicit versions and latest aliases."""
    result = {}
    for version, document in documents.items():
        schema_source = source(root / f"proxbox_api/generated/proxmox/{version}/openapi.json", root)
        schema_version = document.get("info", {}).get("version")
        for path, method, operation in _operation_rows(document):
            name = (
                f"generated_proxmox_route__{version}__{method.lower()}__{operation['operationId']}"
            )
            entry = Generated.model_validate(
                {
                    "version": version,
                    "schema_version": schema_version,
                    "alias": False,
                    "upstream_path": path,
                    "upstream_method": method,
                    "operation_id": operation["operationId"],
                    "operation_sha256": digest(operation),
                    "schema_source": schema_source,
                }
            )
            if name in result:
                raise InventoryError("Generated operation names collide")
            result[name] = entry
            if version == "latest":
                result[name + "__alias"] = entry.model_copy(update={"alias": True})
    return result


def bind(operation: Operation, expected: dict[str, Generated]) -> Operation:
    """Require exact generated name/method membership before binding source metadata."""
    generated = operation.path.startswith("/proxmox/api2/")
    if not generated:
        return operation
    identity = expected.get(operation.name)
    if identity is None or operation.methods != [identity.upstream_method]:
        raise InventoryError("Unexpected generated registration")
    prefix = "/proxmox/api2" if identity.alias else f"/proxmox/api2/{identity.version}"
    if not operation.path.startswith(prefix + "/"):
        raise InventoryError("Generated version prefix disagrees")
    actual_path = re.sub(r"\{[^{}]+\}", "{}", operation.path)
    expected_path = re.sub(r"\{[^{}]+\}", "{}", prefix + identity.upstream_path)
    if actual_path != expected_path:
        raise InventoryError("Generated upstream path disagrees")
    return Operation.model_validate({**operation.model_dump(), "generated": identity.model_dump()})
