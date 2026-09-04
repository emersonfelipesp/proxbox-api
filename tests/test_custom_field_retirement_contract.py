"""Source contract for the retired NetBox custom-field integration."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "proxbox_api"
GENERATED_ROOT = PACKAGE_ROOT / "generated"
RUST_SOURCE_ROOT = ROOT / "proxbox-reconcile-rs" / "src"
SIDECAR_WRITER = PACKAGE_ROOT / "services" / "sync" / "sync_state_writer.py"
SIDECAR_WRITE_CALLS = {
    "write_virtual_machine_sync_state",
}

FORBIDDEN_SOURCE_FRAGMENTS = (
    "proxbox_api.services.custom_fields",
    "CreateCustomFieldsDep",
    "NetBoxCustomFieldSyncState",
    "custom_fields_enabled",
    "custom_fields_request_delay",
    "legacy_custom_field",
    "extras.custom_fields",
    "/api/extras/custom-fields/",
    "/extras/custom-fields/create",
    "/custom-fields/reconcile",
)


def _runtime_sources() -> list[Path]:
    assert PACKAGE_ROOT.is_dir(), f"package root is missing: {PACKAGE_ROOT}"
    sources = [
        path for path in PACKAGE_ROOT.rglob("*.py") if not path.is_relative_to(GENERATED_ROOT)
    ]
    assert sources, f"no Python sources found below {PACKAGE_ROOT}"
    return sorted(sources)


def _runtime_rust_sources() -> list[Path]:
    assert RUST_SOURCE_ROOT.is_dir(), f"Rust source root is missing: {RUST_SOURCE_ROOT}"
    sources = sorted(RUST_SOURCE_ROOT.rglob("*.rs"))
    assert sources, f"no Rust sources found below {RUST_SOURCE_ROOT}"
    return sources


def test_legacy_custom_field_surface_is_absent() -> None:
    """Reject legacy definitions, routes, settings, payloads, and response reads."""
    violations: list[str] = []

    for path in _runtime_sources():
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT)
        tree = ast.parse(source, filename=str(relative))

        for fragment in FORBIDDEN_SOURCE_FRAGMENTS:
            if fragment in source:
                violations.append(f"{relative}: forbidden source fragment {fragment!r}")

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "custom_fields":
                violations.append(
                    f"{relative}:{node.lineno}: constructs or reads a custom_fields key"
                )
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "custom_fields"
            ):
                violations.append(
                    f"{relative}:{node.lineno}: declares a custom_fields payload field"
                )
            if isinstance(node, ast.arg) and node.arg == "custom_fields" and path != SIDECAR_WRITER:
                violations.append(
                    f"{relative}:{node.lineno}: threads a custom_fields route/service argument"
                )
            if isinstance(node, ast.Call):
                called_name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else None
                )
                for keyword in node.keywords:
                    if keyword.arg == "custom_fields" and called_name not in SIDECAR_WRITE_CALLS:
                        violations.append(
                            f"{relative}:{node.lineno}: constructs a custom_fields payload"
                        )

    for path in _runtime_rust_sources():
        source = path.read_text(encoding="utf-8")
        assert source.strip(), f"runtime source is empty: {path.relative_to(ROOT)}"
        runtime_source = source.partition("#[cfg(test)]")[0]
        relative = path.relative_to(ROOT)
        for fragment in ('"custom_fields"', "cf_proxmox", "insert_custom_fields"):
            if fragment in runtime_source:
                violations.append(f"{relative}: forbidden Rust source fragment {fragment!r}")

    assert not violations, "Legacy custom-field surface remains:\n" + "\n".join(violations)


def test_deleted_custom_field_routes_are_absent_from_openapi() -> None:
    """Keep both removed route contracts out of the generated API document."""
    from proxbox_api.main import app

    paths = app.openapi()["paths"]
    assert "/extras/custom-fields/reconcile" not in paths
    assert "/extras/extras/custom-fields/create" not in paths
