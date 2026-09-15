"""CLI for Proxmox OpenAPI schema management.

Provides commands to list bundled schema versions, check version compatibility,
and generate new schemas from the Proxmox API Viewer.

Usage::

    proxbox-schema list
    proxbox-schema generate 8.4
    proxbox-schema generate 8.4 --workers 5
    proxbox-schema quarantine-legacy
    proxbox-schema status
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _list_versions(*, include_user: bool = False) -> int:
    """Print available bundled schemas and explicitly requested user schemas."""
    from proxbox_api.proxmox_to_netbox.proxmox_schema import (
        available_proxmox_sdk_versions,
        has_bundled_proxmox_schema,
    )

    versions = available_proxmox_sdk_versions(include_user=include_user)
    if not versions:
        print("No bundled Proxmox OpenAPI schemas found.")
        return 1

    print(f"Available Proxmox OpenAPI schema versions ({len(versions)}):\n")
    for version in versions:
        from proxbox_api.proxmox_to_netbox.proxmox_schema import proxmox_generated_openapi_path

        path = proxmox_generated_openapi_path(version_tag=version, allow_user=include_user)
        size_mb = path.stat().st_size / (1024 * 1024) if path.exists() else 0
        artifact_kind = "bundled" if has_bundled_proxmox_schema(version) else "user-generated"
        print(f"  {version:>10}   {size_mb:.1f} MB   [{artifact_kind}]   {path}")
    return 0


def _partition_versions(versions: list[str]) -> tuple[list[str], list[str]]:
    """Separate immutable bundled tags from opted-in user-generated tags."""
    from proxbox_api.proxmox_to_netbox.proxmox_schema import has_bundled_proxmox_schema

    bundled = [version for version in versions if has_bundled_proxmox_schema(version)]
    user = [version for version in versions if version not in bundled]
    return bundled, user


def _status(*, include_user: bool = False) -> int:
    """Print schema version summary and generation task statuses."""
    from proxbox_api.proxmox_to_netbox.proxmox_schema import available_proxmox_sdk_versions
    from proxbox_api.schema_version_manager import get_all_generation_statuses

    versions = available_proxmox_sdk_versions(include_user=include_user)
    bundled, user = _partition_versions(versions) if include_user else (versions, [])
    tasks = get_all_generation_statuses()

    print(f"Bundled versions: {', '.join(bundled) if bundled else '(none)'}")
    if include_user:
        print(f"User-generated versions: {', '.join(user) if user else '(none)'}")
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
    from proxbox_api.proxmox_codegen.pipeline import (
        codegen_output_directory,
        generate_proxmox_codegen_bundle,
        is_default_codegen_source,
    )
    from proxbox_api.proxmox_to_netbox.proxmox_schema import (
        get_user_generated_dir,
        has_bundled_proxmox_schema,
    )
    from proxbox_api.schema_version_manager import has_schema_for_release

    version_tag = args.version_tag

    if has_bundled_proxmox_schema(version_tag):
        print(
            f"Bundled schema version '{version_tag}' is immutable; choose a new version tag.",
            file=sys.stderr,
        )
        return 1
    if has_schema_for_release(version_tag, allow_user=True) and not args.force:
        print(f"Schema for version '{version_tag}' already exists.")
        print("Use --force to regenerate it.")
        return 0

    output_dir = Path(args.output_dir) if args.output_dir else get_user_generated_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir = codegen_output_directory(
        output_dir,
        source_url=args.source_url,
        version_tag=version_tag,
    )
    print(f"Generating Proxmox OpenAPI schema for version '{version_tag}'...")
    print(f"Output directory: {artifact_dir}")
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
    print(f"  Output:     {artifact_dir}")
    print()
    if is_default_codegen_source(args.source_url):
        print("Schema is ready for offline inspection.")
        print("Start the development app with PROXBOX_RUNTIME_CODEGEN_ENABLED=true to discover it.")
    else:
        print("Custom-source artifacts are inspection-only and cannot register runtime routes.")
    return 0


def _quarantine_legacy() -> int:
    """Quarantine legacy persisted Python and unprovenanced cache artifacts."""

    from proxbox_api.proxmox_to_netbox.proxmox_schema import (
        quarantine_legacy_codegen_artifacts,
    )

    quarantined = quarantine_legacy_codegen_artifacts()
    if not quarantined:
        print("No legacy Proxmox codegen artifacts required quarantine.")
        return 0
    print(f"Quarantined {len(quarantined)} legacy Proxmox codegen artifact(s):")
    for path in quarantined:
        print(f"  {path}")
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
    list_parser = subparsers.add_parser(
        "list",
        help="List all available bundled Proxmox OpenAPI schema versions.",
    )
    list_parser.add_argument(
        "--include-user",
        action="store_true",
        help="Include provenance-verified user artifacts (requires runtime codegen opt-in).",
    )

    # status
    status_parser = subparsers.add_parser(
        "status",
        help="Show schema availability and any active generation tasks.",
    )
    status_parser.add_argument(
        "--include-user",
        action="store_true",
        help="Include provenance-verified user artifacts (requires runtime codegen opt-in).",
    )

    subparsers.add_parser(
        "quarantine-legacy",
        help="Quarantine persisted Python models and unprovenanced runtime route caches.",
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
        default=None,
        help="Base output directory (default: ~/.local/share/proxbox/generated/proxmox or PROXBOX_GENERATED_DIR).",
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


def _validated_include_user(args: argparse.Namespace) -> bool | None:
    """Validate the explicit user-artifact discovery request."""
    if not getattr(args, "include_user", False):
        return False

    from proxbox_api.runtime_settings import runtime_codegen_enabled

    if runtime_codegen_enabled():
        return True
    print(
        "--include-user requires PROXBOX_RUNTIME_CODEGEN_ENABLED=true.",
        file=sys.stderr,
    )
    return None


def main() -> int:
    """Entry point for the proxbox-schema CLI."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    include_user = _validated_include_user(args)
    if include_user is None:
        return 2
    if args.command == "list":
        return _list_versions(include_user=include_user)
    if args.command == "status":
        return _status(include_user=include_user)
    if args.command == "quarantine-legacy":
        return _quarantine_legacy()
    if args.command == "generate":
        return _generate(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
