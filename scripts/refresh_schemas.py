"""Refresh pinned Proxmox and NetBox schema artifacts used by tests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.request import urlopen

from proxbox_api.proxmox_codegen.apidoc_parser import PROXMOX_API_VIEWER_URL
from proxbox_api.proxmox_codegen.pipeline import generate_proxmox_codegen_bundle

DEFAULT_NETBOX_OPENAPI_URL = (
    "https://raw.githubusercontent.com/netbox-community/netbox/develop/contrib/openapi.json"
)


def _default_proxmox_output_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "proxbox_api" / "generated" / "proxmox"


def _default_netbox_output_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "proxbox_api"
        / "generated"
        / "netbox"
        / "openapi.json"
    )


def fetch_netbox_openapi(url: str) -> dict:
    with urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Refresh pinned Proxmox and NetBox schema artifacts."
    )
    parser.add_argument(
        "--skip-proxmox",
        action="store_true",
        help="Skip regenerating Proxmox viewer-derived artifacts.",
    )
    parser.add_argument(
        "--skip-netbox",
        action="store_true",
        help="Skip refreshing the pinned NetBox OpenAPI snapshot.",
    )
    parser.add_argument(
        "--proxmox-source-url",
        default=PROXMOX_API_VIEWER_URL,
        help="Proxmox API viewer URL used for schema generation.",
    )
    parser.add_argument(
        "--proxmox-version-tag",
        default="latest",
        help="Version tag used for generated Proxmox artifacts.",
    )
    parser.add_argument(
        "--netbox-openapi-url",
        default=DEFAULT_NETBOX_OPENAPI_URL,
        help="URL for the official NetBox OpenAPI JSON artifact.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if not args.skip_proxmox:
        generate_proxmox_codegen_bundle(
            output_dir=_default_proxmox_output_dir(),
            source_url=args.proxmox_source_url,
            version_tag=args.proxmox_version_tag,
        )

    if not args.skip_netbox:
        write_json(
            _default_netbox_output_path(),
            fetch_netbox_openapi(args.netbox_openapi_url),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
