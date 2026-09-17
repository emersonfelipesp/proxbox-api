"""Schema contract tests for generated API artifacts."""

from __future__ import annotations

import importlib.util
from collections.abc import Iterable
from pathlib import Path

import pytest
from pydantic import ValidationError

from proxbox_api.main import app
from proxbox_api.proxmox_to_netbox import netbox_schema
from proxbox_api.proxmox_to_netbox.proxmox_schema import (
    load_proxmox_generated_openapi,
    proxmox_generated_openapi_path,
    proxmox_operation_schema,
)
from proxbox_api.schemas.proxmox import ProxmoxSessionSchema
from tests.fixtures import NETBOX_OPENAPI_SNAPSHOT

OpenAPIDocument = dict[str, object]
PUBLIC_PLUGIN_NAMESPACES = frozenset({"bng", "gpon", "proxbox"})
REPRESENTATIVE_PUBLIC_PATHS = (
    "/api/virtualization/virtual-machines/",
    "/api/dcim/devices/",
    "/api/ipam/prefixes/",
)
VIRTUAL_MACHINE_SCHEMA_REFS = frozenset(
    {
        "#/components/schemas/PaginatedVirtualMachineWithConfigContextList",
        "#/components/schemas/VirtualMachineWithConfigContext",
        "#/components/schemas/WritableVirtualMachineWithConfigContextRequest",
    }
)


def _is_local_json_pointer(ref: str) -> bool:
    return ref.startswith("#/")


def _decode_json_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _resolve_local_json_pointer(document: OpenAPIDocument, pointer: str) -> object | None:
    if not _is_local_json_pointer(pointer):
        return None
    current: object = document
    for raw_token in pointer[2:].split("/"):
        if not raw_token:
            return None
        token = _decode_json_pointer_token(raw_token)
        if not isinstance(current, dict) or token not in current:
            return None
        current = current[token]
    return current


def _iter_local_json_pointers(node: object) -> Iterable[str]:
    stack: list[object] = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            ref = current.get("$ref")
            if isinstance(ref, str) and _is_local_json_pointer(ref):
                yield ref
            stack.extend(current.values())
            continue
        if isinstance(current, list):
            stack.extend(current)


def _collect_dangling_local_references(document: OpenAPIDocument) -> list[str]:
    seen: set[str] = set()
    dangling: list[str] = []
    for ref in _iter_local_json_pointers(document):
        if ref in seen:
            continue
        seen.add(ref)
        if _resolve_local_json_pointer(document, ref) is None:
            dangling.append(ref)
    return dangling


def _plugin_namespace(path: str) -> str | None:
    parts = path.split("/")
    if len(parts) > 3 and parts[1:3] == ["api", "plugins"]:
        return parts[3]
    return None


def _assert_representative_public_paths(paths: dict[str, object]) -> None:
    for path in REPRESENTATIVE_PUBLIC_PATHS:
        assert path in paths


def _assert_no_private_control_plane_surface(document: OpenAPIDocument) -> None:
    paths = document.get("paths")
    assert isinstance(paths, dict)
    plugin_namespaces = {
        namespace for path in paths if (namespace := _plugin_namespace(path)) is not None
    }
    assert plugin_namespaces <= PUBLIC_PLUGIN_NAMESPACES

    components = document.get("components")
    assert isinstance(components, dict)
    schemas = components.get("schemas")
    assert isinstance(schemas, dict)
    assert not any("Backend" in name for name in schemas)


def _assert_virtual_machine_operation_schemas(document: OpenAPIDocument) -> None:
    paths = document.get("paths")
    assert isinstance(paths, dict)
    vm_path = paths["/api/virtualization/virtual-machines/"]
    assert isinstance(vm_path, dict)

    refs = set(_iter_local_json_pointers(vm_path))
    missing = VIRTUAL_MACHINE_SCHEMA_REFS - refs
    assert not missing, f"missing VM schema refs: {sorted(missing)}"

    for ref in VIRTUAL_MACHINE_SCHEMA_REFS:
        assert _resolve_local_json_pointer(document, ref) is not None


def _assert_all_local_json_pointers_resolve(document: OpenAPIDocument) -> None:
    dangling = _collect_dangling_local_references(document)
    assert dangling == []


def test_bundled_netbox_openapi_cache_is_sanitized_public_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for issue #439: bundled NetBox OpenAPI stays a public contract."""

    monkeypatch.delenv("PROXBOX_NETBOX_OPENAPI_PERSIST", raising=False)
    monkeypatch.setattr(netbox_schema, "_in_memory_openapi_cache", None)

    bundled_path = netbox_schema.netbox_openapi_cache_path()
    assert bundled_path.is_file()

    document = netbox_schema.load_netbox_openapi_cache()
    assert isinstance(document, dict)
    assert document.get("openapi") == "3.0.3"

    paths = document.get("paths")
    assert isinstance(paths, dict)

    _assert_representative_public_paths(paths)
    _assert_no_private_control_plane_surface(document)
    _assert_virtual_machine_operation_schemas(document)
    _assert_all_local_json_pointers_resolve(document)


def test_custom_openapi_contains_embedded_generated_proxmox_schema():
    schema = app.openapi()
    assert schema["info"]["x-proxmox-generated-openapi"]["source"].endswith(
        "proxbox_api/generated/proxmox/latest/openapi.json"
    )
    assert "x-proxmox-generated-openapi" in schema["info"]


def test_generated_proxmox_sdk_snapshot_is_available():
    document = load_proxmox_generated_openapi()
    assert proxmox_generated_openapi_path().exists()
    assert document["openapi"] == "3.1.0"
    assert "/cluster/resources" in document["paths"]
    assert (
        proxmox_operation_schema(
            "/cluster/resources",
            "GET",
            openapi=document,
        )
        is not None
    )


def test_generated_proxmox_pydantic_models_are_importable():
    base = Path(__file__).resolve().parents[1] / "proxbox_api" / "generated" / "proxmox"
    path = base / "latest" / "pydantic_models.py"
    if not path.exists():
        path = base / "pydantic_models.py"
    spec = importlib.util.spec_from_file_location("generated_proxmox_models", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    assert hasattr(module, "GetAccessResponse")
    assert hasattr(module, "GetAccessAclResponse")


@pytest.mark.parametrize(
    "field,value",
    [
        ("timeout", 0),
        ("timeout", 3601),
        ("max_retries", -1),
        ("max_retries", 101),
        ("retry_backoff", -0.1),
        ("retry_backoff", 300.1),
    ],
)
def test_proxmox_session_schema_rejects_out_of_bounds_values(field, value):
    with pytest.raises(ValidationError):
        ProxmoxSessionSchema(**{field: value})


def test_proxmox_session_schema_accepts_valid_bounds():
    schema = ProxmoxSessionSchema(timeout=30, max_retries=5, retry_backoff=1.5)
    assert schema.timeout == 30
    assert schema.max_retries == 5
    assert schema.retry_backoff == 1.5


def test_netbox_schema_resolution_prefers_live_then_cache_then_fallback(
    monkeypatch,
    tmp_path,
):
    cache_path = tmp_path / "openapi.json"
    monkeypatch.setattr(netbox_schema, "netbox_openapi_cache_path", lambda: cache_path)

    monkeypatch.setattr(
        netbox_schema,
        "fetch_live_netbox_openapi",
        lambda timeout=20: NETBOX_OPENAPI_SNAPSHOT,
    )
    live_resolved = netbox_schema.resolve_netbox_schema_contract()
    assert live_resolved["source"] == "live"
    assert cache_path.exists()

    monkeypatch.setattr(netbox_schema, "fetch_live_netbox_openapi", lambda timeout=20: None)
    cached_resolved = netbox_schema.resolve_netbox_schema_contract()
    assert cached_resolved["source"] == "cache"
    assert cached_resolved["openapi"]["paths"]

    cache_path.unlink()
    fallback_resolved = netbox_schema.resolve_netbox_schema_contract()
    assert fallback_resolved["source"] == "fallback"
    assert fallback_resolved["contract"]["required_fields"] == [
        "name",
        "status",
        "cluster",
    ]


def test_netbox_schema_resolution_in_memory_when_persistence_disabled(
    monkeypatch,
    tmp_path,
):
    """With PROXBOX_NETBOX_OPENAPI_PERSIST disabled, resolution never touches disk."""

    cache_path = tmp_path / "openapi.json"
    monkeypatch.setattr(netbox_schema, "netbox_openapi_cache_path", lambda: cache_path)
    monkeypatch.setattr(netbox_schema, "_in_memory_openapi_cache", None)
    monkeypatch.setenv("PROXBOX_NETBOX_OPENAPI_PERSIST", "false")

    assert netbox_schema.netbox_openapi_persistence_enabled() is False

    # Live fetch: the document must be retained in-memory, not written to disk.
    monkeypatch.setattr(
        netbox_schema,
        "fetch_live_netbox_openapi",
        lambda timeout=20: NETBOX_OPENAPI_SNAPSHOT,
    )
    live_resolved = netbox_schema.resolve_netbox_schema_contract()
    assert live_resolved["source"] == "live"
    assert not cache_path.exists()

    # Second resolution with no live endpoint reuses the in-memory document.
    monkeypatch.setattr(netbox_schema, "fetch_live_netbox_openapi", lambda timeout=20: None)
    cached_resolved = netbox_schema.resolve_netbox_schema_contract()
    assert cached_resolved["source"] == "cache"
    assert cached_resolved["openapi"]["paths"]
    assert not cache_path.exists()

    # Clearing the in-memory store falls back to the docs-derived contract.
    monkeypatch.setattr(netbox_schema, "_in_memory_openapi_cache", None)
    fallback_resolved = netbox_schema.resolve_netbox_schema_contract()
    assert fallback_resolved["source"] == "fallback"
    assert not cache_path.exists()


def test_netbox_openapi_persistence_resolves_env_over_plugin_setting(monkeypatch):
    """Persistence toggle resolves env override > plugin setting > default."""

    # Plugin setting disables persistence; no env override -> disabled.
    monkeypatch.delenv("PROXBOX_NETBOX_OPENAPI_PERSIST", raising=False)
    monkeypatch.setattr(
        netbox_schema.runtime_settings,
        "_load_settings",
        lambda: {"netbox_openapi_persist": False},
    )
    assert netbox_schema.netbox_openapi_persistence_enabled() is False

    # Env override wins over the plugin setting.
    monkeypatch.setenv("PROXBOX_NETBOX_OPENAPI_PERSIST", "true")
    assert netbox_schema.netbox_openapi_persistence_enabled() is True

    # Env can also force-disable regardless of the plugin setting.
    monkeypatch.setattr(
        netbox_schema.runtime_settings,
        "_load_settings",
        lambda: {"netbox_openapi_persist": True},
    )
    monkeypatch.setenv("PROXBOX_NETBOX_OPENAPI_PERSIST", "off")
    assert netbox_schema.netbox_openapi_persistence_enabled() is False

    # No env and no plugin value -> default enabled.
    monkeypatch.delenv("PROXBOX_NETBOX_OPENAPI_PERSIST", raising=False)
    monkeypatch.setattr(netbox_schema.runtime_settings, "_load_settings", lambda: None)
    assert netbox_schema.netbox_openapi_persistence_enabled() is True
