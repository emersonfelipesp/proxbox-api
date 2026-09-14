"""Regression guards for security-sensitive dependency resolutions."""

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.version import Version

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
NEXTJS_UI_ROOT = REPOSITORY_ROOT / "nextjs-ui"
SEMVER_PATTERN = re.compile(
    r"^(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _stable_semver(value: str) -> tuple[int, int, int]:
    match = SEMVER_PATTERN.fullmatch(value)
    assert match is not None, f"Invalid semantic version: {value}"
    assert match.group("prerelease") is None, f"Prerelease is not patched: {value}"
    return tuple(int(match.group(part)) for part in ("major", "minor", "patch"))


def _locked_npm_versions(packages: dict[str, Any], name: str) -> list[str]:
    suffix = f"node_modules/{name}"
    versions = [
        package["version"]
        for path, package in packages.items()
        if path == suffix or path.endswith(f"/{suffix}")
    ]
    assert versions, f"No locked instances found for {name}"
    return versions


def _assert_npm_floor(
    packages: dict[str, Any],
    name: str,
    compatible_line: tuple[int, ...],
    floor: tuple[int, int, int],
) -> None:
    for value in _locked_npm_versions(packages, name):
        version = _stable_semver(value)
        assert version[: len(compatible_line)] == compatible_line
        assert version >= floor


def _requirement_for_extra(pyproject: dict[str, Any], extra: str, package_name: str) -> Requirement:
    requirements = [
        Requirement(value) for value in pyproject["project"]["optional-dependencies"][extra]
    ]
    matches = [requirement for requirement in requirements if requirement.name == package_name]
    assert len(matches) == 1
    return matches[0]


def _locked_python_versions(lockfile: dict[str, Any], package_name: str) -> list[Version]:
    versions = [
        Version(package["version"])
        for package in lockfile["package"]
        if package["name"] == package_name
    ]
    assert versions, f"No locked instances found for {package_name}"
    return versions


def _assert_requirement_floor(requirement: Requirement, floor: Version) -> None:
    lower_bounds = []
    for specifier in requirement.specifier:
        if specifier.operator not in {">=", ">", "~=", "=="} or "*" in specifier.version:
            continue
        version = Version(specifier.version)
        if not version.is_prerelease:
            lower_bounds.append(version)
    assert lower_bounds, f"{requirement.name} has no explicit stable lower bound"
    assert max(lower_bounds) >= floor


def test_nextjs_dependency_graph_uses_security_patched_versions() -> None:
    package = _read_json(NEXTJS_UI_ROOT / "package.json")
    lockfile = _read_json(NEXTJS_UI_ROOT / "package-lock.json")
    packages = lockfile["packages"]

    next_version = package["dependencies"]["next"]
    eslint_config_version = package["devDependencies"]["eslint-config-next"]
    assert next_version == eslint_config_version
    assert _stable_semver(next_version) >= (16, 3, 3)

    patched_floors = {
        "next": ((16,), (16, 3, 3)),
        "eslint-config-next": ((16,), (16, 3, 3)),
        "sharp": ((0, 35), (0, 35, 4)),
        "js-yaml": ((4,), (4, 3, 2)),
        "browserslist": ((4,), (4, 28, 7)),
        "baseline-browser-mapping": ((2,), (2, 11, 0)),
        "nanoid": ((3,), (3, 3, 18)),
        "@humanfs/node": ((0, 16), (0, 16, 8)),
        "postcss": ((8,), (8, 5, 23)),
    }
    for name, (compatible_line, floor) in patched_floors.items():
        _assert_npm_floor(packages, name, compatible_line, floor)

    brace_floors = {1: (1, 1, 18), 5: (5, 0, 9)}
    for value in _locked_npm_versions(packages, "brace-expansion"):
        version = _stable_semver(value)
        assert version[0] in brace_floors
        assert version >= brace_floors[version[0]]


def test_documentation_toolchain_uses_patched_mkdocs_material() -> None:
    pyproject = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lockfile = tomllib.loads((REPOSITORY_ROOT / "uv.lock").read_text(encoding="utf-8"))

    requirement = _requirement_for_extra(pyproject, "docs", "mkdocs-material")
    floor = Version("9.7.7")
    _assert_requirement_floor(requirement, floor)

    for version in _locked_python_versions(lockfile, "mkdocs-material"):
        assert not version.is_prerelease
        assert version in requirement.specifier
        assert version >= floor
