"""Relative source identities and unchanged-lock environment verification."""

from __future__ import annotations

import hashlib
import importlib.metadata
import inspect
import platform
import re
import sys
import sysconfig
import tomllib
from pathlib import Path

from .schema import (
    CallableIdentity,
    Dependency,
    InventoryError,
    Provenance,
    Source,
    canonical,
    parse,
)


def regular_file(path: Path) -> Path:
    """Reject symlinks, including directory components, before reading evidence."""
    if any(part.is_symlink() for part in (path, *path.parents)) or not path.is_file():
        raise InventoryError(f"Evidence is not a regular nonsymlink file: {path.name}")
    return path


def source(path: Path, root: Path, owner: str = "repository") -> Source:
    """Bind regular file bytes to a stable owner-relative path."""
    regular_file(path)
    return Source(
        owner=owner,
        path=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def callable_identity(endpoint: object, root: Path) -> CallableIdentity:
    """Locate real callable code, including callable-object mount applications."""
    if not callable(endpoint):
        raise InventoryError("Route endpoint is not callable")
    function = inspect.unwrap(endpoint)
    if not hasattr(function, "__code__"):
        function = type(function).__call__
    code = getattr(function, "__code__", None)
    if code is None:
        raise InventoryError("Callable has no inspectable source")
    path = Path(code.co_filename).absolute()
    module = function.__module__
    environment_root = Path(sys.prefix).resolve()
    if path.is_relative_to(root) and not path.is_relative_to(environment_root):
        identity = source(path, root)
    else:
        package = module.split(".", 1)[0]
        package_module = sys.modules[package]
        package_file = package_module.__file__
        if package_file is None:
            raise InventoryError("Callable package has no inspectable file")
        package_root = Path(package_file).absolute().parent.parent
        identity = source(path, package_root, package)
    return CallableIdentity(
        module=module, qualname=function.__qualname__, line=code.co_firstlineno, source=identity
    )


def source_paths(root: Path) -> list[Path]:
    """Enumerate the complete first-party extraction closure, not a commit hash."""
    paths = list((root / "proxbox_api").rglob("*.py"))
    paths += list((root / "proxbox_api/generated/proxmox").glob("*/openapi.json"))
    paths += list((root / "scripts").glob("*inventory*.py"))
    paths += [root / "pyproject.toml", root / "uv.lock"]
    return sorted(path for path in paths if "__pycache__" not in path.parts)


def normalize_name(name: str) -> str:
    """Apply the package-name normalization used by Python distribution metadata."""
    return re.sub(r"[-_.]+", "-", name).lower()


def editable_origin(distribution: importlib.metadata.Distribution, root: Path) -> str:
    """Accept only the selected source metadata and its matching editable install."""
    location = Path(str(distribution.locate_file(""))).resolve()
    raw = distribution.read_text("direct_url.json")
    if location == root and raw is None:
        regular_file(root / "proxbox_api.egg-info/PKG-INFO")
        return "source"
    expected = {"dir_info": {"editable": True}, "url": root.as_uri()}
    if location == Path(sysconfig.get_path("purelib")).resolve() and raw is not None:
        if canonical(parse(raw.encode("utf-8"))) == canonical(expected):
            return "installed"
    raise InventoryError("Editable metadata does not identify the selected source")


def dependencies(root: Path) -> list[Dependency]:
    """Refuse installed distributions that do not match the unchanged lock."""
    lock = tomllib.loads(regular_file(root / "uv.lock").read_text())
    expected: dict[str, set[str]] = {}
    for item in lock["package"]:
        expected.setdefault(normalize_name(item["name"]), set()).add(item["version"])
    installed: dict[str, str] = {}
    project_origins: set[str] = set()
    search_paths = list(dict.fromkeys(str(Path(path).resolve()) for path in sys.path))
    for distribution in importlib.metadata.distributions(path=search_paths):
        name = normalize_name(distribution.metadata["Name"])
        if distribution.version not in expected.get(name, set()):
            raise InventoryError(f"Installed distribution differs from lock: {name}")
        if name == "proxbox-api":
            origin = editable_origin(distribution, root)
            if origin in project_origins:
                raise InventoryError("Repeated editable metadata origin")
            project_origins.add(origin)
        elif name in installed:
            raise InventoryError("Repeated installed distribution")
        if name in installed and installed[name] != distribution.version:
            raise InventoryError("Conflicting editable versions")
        installed[name] = distribution.version
    required = {"fastapi", "starlette", "pydantic", "proxmox-sdk", "netbox-sdk", "proxbox-api"}
    if not required.issubset(installed) or project_origins != {"source", "installed"}:
        raise InventoryError("Required locked distributions are missing")
    return [Dependency(name=name, version=version) for name, version in sorted(installed.items())]


def imported_sources(root: Path) -> dict[str, Source]:
    """Reject first-party imports outside the selected source tree."""
    result = {}
    for name, module in sorted(sys.modules.copy().items()):
        filename = getattr(module, "__file__", None)
        if not name.startswith("proxbox_api") or not filename:
            continue
        path = Path(filename).absolute()
        if not path.is_relative_to(root / "proxbox_api"):
            raise InventoryError("First-party import escaped selected repository")
        result[name] = source(path, root)
    return result


def provenance(root: Path) -> Provenance:
    """Capture stable source, import, interpreter and installed-lock evidence."""
    return Provenance(
        python=platform.python_version(),
        dependencies=dependencies(root),
        sources=[source(path, root) for path in source_paths(root)],
        framework_sources=[
            distribution_source(name, f"{name}/routing.py") for name in ("fastapi", "starlette")
        ],
        imported_modules=imported_sources(root),
    )


def distribution_source(owner: str, path: str) -> Source:
    """Bind framework adapter code to exact installed file bytes."""
    base = Path(str(importlib.metadata.distribution(owner).locate_file(""))).absolute()
    return source(base / path, base, owner)
