"""CLI for Proxmox OpenAPI schema management.

Provides commands to list bundled schema versions, check version compatibility,
and generate new schemas from the Proxmox API Viewer.

Usage::

    proxbox-schema list
    proxbox-schema generate 8.4
    proxbox-schema generate 8.4 --workers 5
    proxbox-schema status
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _list_versions() -> int:
    """Print all available bundled Proxmox OpenAPI schema versions."""
    from proxbox_api.proxmox_to_netbox.proxmox_schema import available_proxmox_sdk_versions

    versions = available_proxmox_sdk_versions()
    if not versions:
        print("No bundled Proxmox OpenAPI schemas found.")
        return 1

    print(f"Available Proxmox OpenAPI schema versions ({len(versions)}):\n")
    for version in versions:
        from proxbox_api.proxmox_to_netbox.proxmox_schema import proxmox_generated_openapi_path

        path = proxmox_generated_openapi_path(version_tag=version)
        size_mb = path.stat().st_size / (1024 * 1024) if path.exists() else 0
        print(f"  {version:>10}   {size_mb:.1f} MB   {path}")
    return 0


def _status() -> int:
    """Print schema version summary and generation task statuses."""
    from proxbox_api.proxmox_to_netbox.proxmox_schema import available_proxmox_sdk_versions
    from proxbox_api.schema_version_manager import get_all_generation_statuses

    versions = available_proxmox_sdk_versions()
    tasks = get_all_generation_statuses()

    print(f"Bundled versions: {', '.join(versions) if versions else '(none)'}")
    if tasks:
        print("\nGeneration tasks:")
        for tag, info in tasks.items():
            status = info.get("status", "unknown")
            error = info.get("error")
            line = f"  {tag}: {status}"
            if error:
                line += f" ({error})"
            print(line)
    else:
        print("No active or recent generation tasks.")
    return 0


def _generate(args: argparse.Namespace) -> int:
    """Generate OpenAPI schema for a specific Proxmox version tag."""
    from proxbox_api.proxmox_codegen.pipeline import generate_proxmox_codegen_bundle
    from proxbox_api.schema_version_manager import has_schema_for_release

    version_tag = args.version_tag

    if has_schema_for_release(version_tag) and not args.force:
        print(f"Schema for version '{version_tag}' already exists.")
        print("Use --force to regenerate it.")
        return 0

    output_dir = Path(args.output_dir)
    print(f"Generating Proxmox OpenAPI schema for version '{version_tag}'...")
    print(f"Output directory: {output_dir / version_tag}")
    print(f"Source URL: {args.source_url}")
    print(f"Workers: {args.workers}")
    print()
    print("This may take several minutes. The pipeline crawls the Proxmox API Viewer,")
    print("parses all endpoints, and generates OpenAPI + Pydantic artifacts.")
    print()

    try:
        bundle = generate_proxmox_codegen_bundle(
            output_dir=output_dir,
            source_url=args.source_url,
            version_tag=version_tag,
            worker_count=max(1, args.workers),
            retry_count=max(0, args.retry_count),
            retry_backoff_seconds=max(0.0, args.retry_backoff),
            checkpoint_every=max(1, args.checkpoint_every),
        )
    except Exception as error:
        print(f"\nGeneration failed: {error}", file=sys.stderr)
        return 1

    viewer = bundle.capture.get("viewer", {})
    completeness = bundle.capture.get("completeness", {})

    print()
    print(f"Generation completed for Proxmox {bundle.version_tag}")
    print(f"  Endpoints:  {bundle.endpoint_count}")
    print(f"  Operations: {bundle.operation_count}")
    if viewer.get("duration_seconds"):
        print(f"  Duration:   {viewer['duration_seconds']:.1f}s")
    fallback = completeness.get("fallback_method_count", 0)
    if fallback:
        print(f"  Fallback methods (from apidoc.js): {fallback}")
    print(f"  Output:     {output_dir / version_tag}")
    print()
    print("Schema is ready. Restart the app or call POST /proxmox/viewer/routes/refresh")
    print("to register the new routes at runtime.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build command-line parser for Proxmox schema management."""
    from proxbox_api.proxmox_codegen.apidoc_parser import PROXMOX_API_VIEWER_URL

    parser = argparse.ArgumentParser(
        prog="proxbox-schema",
        description="Manage Proxmox OpenAPI schema versions for proxbox-api.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # list
    subparsers.add_parser(
        "list",
        help="List all available bundled Proxmox OpenAPI schema versions.",
    )

    # status
    subparsers.add_parser(
        "status",
        help="Show schema availability and any active generation tasks.",
    )

    # generate
    gen_parser = subparsers.add_parser(
        "generate",
        help="Generate an OpenAPI schema for a Proxmox version tag.",
    )
    gen_parser.add_argument(
        "version_tag",
        help="Version tag (e.g. '8.4'). Used as subdirectory name and OpenAPI info.version.",
    )
    gen_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Regenerate even if the schema already exists for this version.",
    )
    gen_parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).resolve().parent / "generated" / "proxmox"),
        help="Base output directory (default: proxbox_api/generated/proxmox).",
    )
    gen_parser.add_argument(
        "--source-url",
        default=PROXMOX_API_VIEWER_URL,
        help="Proxmox API Viewer URL to crawl.",
    )
    gen_parser.add_argument(
        "--workers",
        default=10,
        type=int,
        help="Number of async Playwright workers (default: 10).",
    )
    gen_parser.add_argument(
        "--retry-count",
        default=2,
        type=int,
        help="Retry attempts per endpoint (default: 2).",
    )
    gen_parser.add_argument(
        "--retry-backoff",
        default=0.35,
        type=float,
        help="Base backoff in seconds between retries (default: 0.35).",
    )
    gen_parser.add_argument(
        "--checkpoint-every",
        default=50,
        type=int,
        help="Write checkpoint after this many processed endpoints (default: 50).",
    )

    return parser


def main() -> int:
    """Entry point for the proxbox-schema CLI."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "list":
        return _list_versions()
    if args.command == "status":
        return _status()
    if args.command == "generate":
        return _generate(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
