"""Packaged OpenAPI artifact access for the standalone Proxmox mock package."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

DEFAULT_PROXMOX_OPENAPI_TAG = "latest"


def packaged_openapi_path(
    version_tag: str = DEFAULT_PROXMOX_OPENAPI_TAG,
) -> Path:
    """Return the packaged OpenAPI artifact path."""

    if version_tag != DEFAULT_PROXMOX_OPENAPI_TAG:
        raise FileNotFoundError(f"Unsupported packaged Proxmox OpenAPI version: {version_tag}")
    return Path(str(files("proxmox_mock.generated").joinpath("openapi.json")))


def load_packaged_openapi(
    version_tag: str = DEFAULT_PROXMOX_OPENAPI_TAG,
) -> dict[str, object]:
    """Load the packaged OpenAPI artifact."""

    path = packaged_openapi_path(version_tag=version_tag)
    return json.loads(path.read_text(encoding="utf-8"))
