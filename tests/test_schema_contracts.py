"""Schema contract tests for generated API artifacts."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from proxbox_api.main import app
from proxbox_api.proxmox_to_netbox import netbox_schema
from proxbox_api.proxmox_to_netbox.proxmox_schema import (
    load_proxmox_generated_openapi,
    proxmox_generated_openapi_path,
    proxmox_operation_schema,
)
from tests.fixtures import NETBOX_OPENAPI_SNAPSHOT


def test_custom_openapi_contains_embedded_generated_proxmox_schema():
    schema = app.openapi()
    assert schema["info"]["x-proxmox-generated-openapi"]["source"].endswith(
        "proxbox_api/generated/proxmox/latest/openapi.json"
    )
    assert "x-proxmox-generated-openapi" in schema


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
