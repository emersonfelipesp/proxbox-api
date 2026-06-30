"""Tests for SDK dependency wiring."""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROXBOX_API_VERSION = "0.0.19"
PROXMOX_SDK_VERSION = "0.0.12"
NETBOX_SDK_VERSION = "0.0.10"


def _pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _lockfile() -> dict:
    return tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))


def test_proxmox_sdk_mock_entrypoint_is_importable() -> None:
    import proxmox_sdk.mock_main

    assert proxmox_sdk.mock_main.app is not None


def test_certified_stack_dependency_pins_are_declared() -> None:
    project = _pyproject()["project"]

    assert project["version"] == PROXBOX_API_VERSION
    assert f"proxmox-sdk=={PROXMOX_SDK_VERSION}" in project["dependencies"]
    assert f"netbox-sdk=={NETBOX_SDK_VERSION}" in project["dependencies"]
    assert f"proxmox-sdk[pbs]=={PROXMOX_SDK_VERSION}" in project["optional-dependencies"]["pbs"]
    assert f"proxmox-sdk[pdm]=={PROXMOX_SDK_VERSION}" in project["optional-dependencies"]["pdm"]


def test_certified_stack_dependency_pins_are_locked() -> None:
    packages = {package["name"]: package for package in _lockfile()["package"]}
    proxbox_api = packages["proxbox-api"]

    assert proxbox_api["version"] == PROXBOX_API_VERSION
    assert packages["proxmox-sdk"]["version"] == PROXMOX_SDK_VERSION
    assert packages["netbox-sdk"]["version"] == NETBOX_SDK_VERSION

    locked_specs = {
        item["name"]: item["specifier"]
        for item in proxbox_api["metadata"]["requires-dist"]
        if item["name"] in {"proxmox-sdk", "netbox-sdk"} and "extras" not in item
    }
    assert locked_specs == {
        "proxmox-sdk": f"=={PROXMOX_SDK_VERSION}",
        "netbox-sdk": f"=={NETBOX_SDK_VERSION}",
    }

    extra_specs = {
        tuple(item.get("extras", [])): item["specifier"]
        for item in proxbox_api["metadata"]["requires-dist"]
        if item["name"] == "proxmox-sdk" and item.get("extras")
    }
    assert extra_specs == {
        ("pbs",): f"=={PROXMOX_SDK_VERSION}",
        ("pdm",): f"=={PROXMOX_SDK_VERSION}",
    }
