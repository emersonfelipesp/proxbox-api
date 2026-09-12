"""Build-only inventory consistency checks, using no application imports."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
from typing import Protocol


class BuildConfig(Protocol):
    config_file_path: str


ARTIFACTS = frozenset(
    {
        "mounted-operations.json",
        "mounted-operations.en.md",
        "mounted-operations.pt-BR.md",
        "mounted-operations.schema.json",
        "operation-coverage.schema.json",
    }
)


def _regular(path: Path) -> Path:
    if any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file():
        raise ValueError("Inventory evidence must be a regular nonsymlink file")
    if path.stat().st_size > 96 * 1024 * 1024:
        raise ValueError("Inventory evidence exceeds byte limit")
    return path


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate inventory key")
        result[key] = value
    return result


def _json(path: Path) -> dict:
    value = json.loads(_regular(path).read_text(encoding="utf-8"), object_pairs_hook=_pairs)
    if type(value) is not dict:
        raise ValueError("Inventory document must be an object")
    return value


def _sha(path: Path) -> str:
    return hashlib.sha256(_regular(path).read_bytes()).hexdigest()


def _source_paths(root: Path) -> set[str]:
    paths = list((root / "proxbox_api").rglob("*.py"))
    paths += list((root / "proxbox_api/generated/proxmox").glob("*/openapi.json"))
    paths += list((root / "scripts").glob("*inventory*.py"))
    paths += [
        root / "pyproject.toml",
        root / "uv.lock",
        root / "contracts/operation-inventory-inputs.json",
    ]
    return {path.relative_to(root).as_posix() for path in paths if "__pycache__" not in path.parts}


def _check_sources(root: Path, inventory: dict) -> None:
    sources = inventory["provenance"]["sources"]
    if type(sources) is not list:
        raise ValueError("Missing source manifest")
    names = set()
    for item in sources:
        if set(item) != {"owner", "path", "sha256"} or item["owner"] != "repository":
            raise ValueError("Invalid source identity")
        path = root / item["path"]
        if not path.resolve().is_relative_to(root) or item["path"] in names:
            raise ValueError("Escaped or duplicate source identity")
        names.add(item["path"])
        if _sha(path) != item["sha256"]:
            raise ValueError("Inventory source digest is stale")
    if names != _source_paths(root):
        raise ValueError("Inventory source set is stale")


def _callable_file(root: Path, identity: dict) -> Path:
    if set(identity) != {"owner", "path", "sha256"}:
        raise ValueError("Invalid callable source identity")
    relative = identity["path"]
    if relative.startswith("/") or "\\" in relative:
        raise ValueError("Invalid callable source path")
    if any(part in ("", ".", "..") for part in relative.split("/")):
        raise ValueError("Noncanonical callable source path")
    base = root
    if identity["owner"] != "repository":
        base = Path(
            str(importlib.metadata.distribution(identity["owner"]).locate_file(""))
        ).absolute()
    path = base / relative
    if not path.resolve().is_relative_to(base):
        raise ValueError("Callable source escaped its owner")
    return _regular(path)


def _check_callables(root: Path, inventory: dict) -> None:
    actual = {}
    for operation in inventory["operations"].values():
        identity = operation["handler"]["source"]
        path = _callable_file(root, identity)
        if path not in actual:
            actual[path] = _sha(path)
        if actual[path] != identity["sha256"]:
            raise ValueError("Callable source identity is stale")


def verify_artifacts(root: Path) -> None:
    """Fail closed for missing, changed, escaped, or stale committed evidence."""
    contracts = root / "contracts"
    integrity = _json(contracts / "operation-inventory-integrity.json")
    if set(integrity) != ARTIFACTS:
        raise ValueError("Incomplete artifact integrity manifest")
    for name, expected in integrity.items():
        if _sha(contracts / name) != expected:
            raise ValueError("Inventory artifact digest is stale")
    inventory = _json(contracts / "mounted-operations.json")
    if type(inventory.get("schema_version")) is not int or inventory["schema_version"] != 1:
        raise ValueError("Unsupported inventory schema")
    _check_sources(root, inventory)
    _check_callables(root, inventory)
    framework = inventory["provenance"]["framework_sources"]
    if [row["owner"] for row in framework] != ["fastapi", "starlette"]:
        raise ValueError("Incomplete framework source identity")
    for row in framework:
        if row["path"] != row["owner"] + "/routing.py":
            raise ValueError("Invalid framework source path")
        path = Path(str(importlib.metadata.distribution(row["owner"]).locate_file(row["path"])))
        if _sha(path) != row["sha256"]:
            raise ValueError("Framework source identity is stale")


def on_pre_build(config: BuildConfig) -> None:
    """Validate before snippets render, without importing proxbox_api or routes."""
    root = Path(config.config_file_path).absolute().parent
    verify_artifacts(root)
