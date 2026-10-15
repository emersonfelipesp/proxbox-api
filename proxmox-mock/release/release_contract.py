"""Validate the standalone mock's immutable package-first release inputs."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

PACKAGE = "proxmox-mock-api"
PLATFORMS = {"linux/amd64", "linux/arm64"}
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:\.post[0-9]+)?")
BASE_IMAGE_RE = re.compile(r"python:3\.13\.[0-9]+-slim-bookworm@sha256:[0-9a-f]{64}")


class ReleaseContractError(ValueError):
    """Raised when a release input is incomplete, mutable, or inconsistent."""


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or DIGEST_RE.fullmatch(value) is None:
        raise ReleaseContractError(f"{label} must be a populated lowercase SHA256 digest")
    return value


def _require_image(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None or ":latest" in value:
        raise ReleaseContractError(f"{label} must be an immutable digest-pinned image")
    return value


def project_version(pyproject: Path) -> str:
    """Read the package version from the standalone project metadata."""
    with pyproject.open("rb") as stream:
        value = tomllib.load(stream)["project"]
    if value.get("name") != PACKAGE or VERSION_RE.fullmatch(value.get("version", "")) is None:
        raise ReleaseContractError("standalone project metadata is invalid")
    return value["version"]


def load_manifest(path: Path) -> dict[str, Any]:
    """Load a TOML manifest without accepting an absent or non-table document."""
    with path.open("rb") as stream:
        value = tomllib.load(stream)
    if not isinstance(value, dict):
        raise ReleaseContractError("release manifest must be a TOML table")
    return value


def validate_manifest(manifest: dict[str, Any], expected_version: str) -> None:
    """Fail closed unless every platform release binding is complete."""
    if manifest.get("schema") != 1 or manifest.get("package") != PACKAGE:
        raise ReleaseContractError("release manifest identity is invalid")
    if manifest.get("version") != expected_version:
        raise ReleaseContractError("release manifest version does not match the package")
    tools = manifest.get("tools", {})
    uv_version = tools.get("uv_version")
    if not isinstance(uv_version, str) or VERSION_RE.fullmatch(uv_version) is None:
        raise ReleaseContractError("tools.uv_version must be exact")
    if (
        not isinstance(tools.get("build_version"), str)
        or VERSION_RE.fullmatch(tools["build_version"]) is None
    ):
        raise ReleaseContractError("tools.build_version must be exact")
    wheel = manifest.get("wheel", {})
    if wheel.get("filename") != f"proxmox_mock_api-{expected_version}-py3-none-any.whl":
        raise ReleaseContractError("wheel filename does not match the package version")
    _require_digest(wheel.get("sha256"), "wheel.sha256")
    _require_digest(manifest.get("requirements_sha256"), "requirements_sha256")
    platforms = manifest.get("platforms", {})
    if set(platforms) != PLATFORMS:
        raise ReleaseContractError("manifest must contain exactly linux/amd64 and linux/arm64")
    uv_pattern = re.compile(rf"ghcr\.io/astral-sh/uv:{re.escape(uv_version)}@sha256:[0-9a-f]{{64}}")
    for platform_name, row in platforms.items():
        _require_image(row.get("base_image"), BASE_IMAGE_RE, f"{platform_name}.base_image")
        _require_image(row.get("uv_image"), uv_pattern, f"{platform_name}.uv_image")
        _require_digest(row.get("wheel_graph_sha256"), f"{platform_name}.wheel_graph_sha256")
        _require_digest(
            row.get("installed_provenance_sha256"),
            f"{platform_name}.installed_provenance_sha256",
        )


def validate_release(
    manifest_path: Path, pyproject: Path, requested_version: str
) -> dict[str, Any]:
    """Bind a dispatch version to package metadata and a complete manifest."""
    version = project_version(pyproject)
    if requested_version != version:
        raise ReleaseContractError("requested version does not match the package version")
    manifest = load_manifest(manifest_path)
    validate_manifest(manifest, version)
    return manifest


def sha256_file(path: Path) -> str:
    """Hash one regular file."""
    if not path.is_file() or path.is_symlink():
        raise ReleaseContractError(f"release input is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wheelhouse_digest(path: Path) -> str:
    """Hash the complete, sorted dependency wheel inventory."""
    if not path.is_dir() or path.is_symlink():
        raise ReleaseContractError("wheelhouse must be a directory")
    rows = []
    for wheel in sorted(path.iterdir(), key=lambda item: item.name):
        if wheel.suffix != ".whl" or not wheel.is_file() or wheel.is_symlink():
            raise ReleaseContractError(f"unexpected wheelhouse member: {wheel.name}")
        rows.append(
            {"filename": wheel.name, "sha256": sha256_file(wheel), "size": wheel.stat().st_size}
        )
    if not rows:
        raise ReleaseContractError("wheelhouse must not be empty")
    encoded = json.dumps(rows, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def verify_context(
    manifest: dict[str, Any], platform_name: str, wheel: Path, requirements: Path, wheelhouse: Path
) -> None:
    """Authenticate the application wheel, lock export, and platform wheel graph."""
    if platform_name not in PLATFORMS:
        raise ReleaseContractError("unsupported target platform")
    if wheel.name != manifest["wheel"]["filename"]:
        raise ReleaseContractError("application wheel filename is not qualified")
    expected = (
        (wheel, manifest["wheel"]["sha256"], "application wheel"),
        (requirements, manifest["requirements_sha256"], "requirements export"),
    )
    for path, digest, label in expected:
        if sha256_file(path) != digest:
            raise ReleaseContractError(f"{label} digest does not match the manifest")
    graph = manifest["platforms"][platform_name]["wheel_graph_sha256"]
    if wheelhouse_digest(wheelhouse) != graph:
        raise ReleaseContractError("dependency wheel graph does not match the manifest")


def installed_provenance(platform_name: str) -> str:
    """Hash installed distributions and RECORD files for one target platform."""
    rows = []
    for distribution in importlib.metadata.distributions():
        name = re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower()
        record = distribution.read_text("RECORD")
        if record is None:
            raise ReleaseContractError(f"installed distribution has no RECORD: {name}")
        rows.append(
            {
                "name": name,
                "record_sha256": hashlib.sha256(record.encode()).hexdigest(),
                "version": distribution.version,
            }
        )
    payload = {
        "packages": sorted(rows, key=lambda row: row["name"]),
        "platform": platform_name,
        "python": ".".join(str(value) for value in sys.version_info[:3]),
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def verify_installed(manifest: dict[str, Any], platform_name: str) -> None:
    """Verify the installed package graph and reject editable application origins."""
    if importlib.metadata.version(PACKAGE) != manifest["version"]:
        raise ReleaseContractError("installed application version is not qualified")
    distribution = importlib.metadata.distribution(PACKAGE)
    direct_url = distribution.read_text("direct_url.json")
    if direct_url and json.loads(direct_url).get("dir_info", {}).get("editable", False):
        raise ReleaseContractError("editable application installs are forbidden")
    expected = manifest["platforms"][platform_name]["installed_provenance_sha256"]
    if installed_provenance(platform_name) != expected:
        raise ReleaseContractError("installed provenance does not match the manifest")


def wheel_filename(manifest: dict[str, Any]) -> str:
    """Return the application wheel name after manifest validation."""
    return manifest["wheel"]["filename"]


def build_parser() -> argparse.ArgumentParser:
    """Create the release contract command parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "validate",
            "image-ref",
            "wheel-filename",
            "verify-context",
            "application-requirement",
            "verify-installed",
        ],
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--pyproject", type=Path)
    parser.add_argument("--version")
    parser.add_argument("--platform", choices=sorted(PLATFORMS))
    parser.add_argument("--kind", choices=["base", "uv"])
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--requirements", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    return parser


def main() -> None:
    """Dispatch explicit validation and read-only release metadata operations."""
    args = build_parser().parse_args()
    manifest = load_manifest(args.manifest)
    if args.command == "validate":
        validate_release(args.manifest, args.pyproject, args.version)
    else:
        validate_manifest(manifest, manifest.get("version", ""))
    if args.command == "image-ref":
        key = {"base": "base_image", "uv": "uv_image"}[args.kind]
        print(manifest["platforms"][args.platform][key])
    elif args.command == "wheel-filename":
        print(wheel_filename(manifest))
    elif args.command == "verify-context":
        verify_context(manifest, args.platform, args.wheel, args.requirements, args.wheelhouse)
    elif args.command == "application-requirement":
        print(f"{PACKAGE}=={manifest['version']} --hash=sha256:{manifest['wheel']['sha256']}")
    elif args.command == "verify-installed":
        verify_installed(manifest, args.platform)


if __name__ == "__main__":
    main()
