"""Tests for Proxmox-to-NetBox normalization and schema contract helpers."""

from pathlib import Path

from proxbox_api.proxmox_to_netbox.mappers.virtual_machine import (
    map_proxmox_vm_to_netbox_vm_body,
)
from proxbox_api.proxmox_to_netbox.netbox_schema import resolve_netbox_schema_contract
from proxbox_api.proxmox_to_netbox.proxmox_schema import load_proxmox_generated_openapi


def test_map_proxmox_vm_to_netbox_vm_body():
    resource = {
        "vmid": 101,
        "name": "db-vm-01",
        "node": "pve01",
        "status": "running",
        "type": "qemu",
        "maxcpu": 4,
        "maxmem": 8_589_934_592,
        "maxdisk": 107_374_182_400,
    }
    config = {
        "onboot": 1,
        "agent": 1,
        "unprivileged": 0,
        "searchdomain": "lab.local",
    }

    body = map_proxmox_vm_to_netbox_vm_body(
        resource=resource,
        config=config,
        cluster_id=11,
        device_id=22,
        role_id=33,
        tag_ids=[7],
    )

    assert body["name"] == "db-vm-01"
    assert body["status"] == "active"
    assert body["cluster"] == 11
    assert body["device"] == 22
    assert body["role"] == 33
    assert body["vcpus"] == 4
    assert body["memory"] > 0
    assert body["disk"] > 0
    assert body["custom_fields"]["proxmox_vm_id"] == 101
    assert body["custom_fields"]["proxmox_start_at_boot"] is True


def test_load_proxmox_generated_openapi_present():
    document = load_proxmox_generated_openapi()
    assert isinstance(document, dict)
    assert "paths" in document


def test_netbox_schema_contract_resolves_source():
    resolved = resolve_netbox_schema_contract()
    assert resolved.get("source") in {"live", "cache", "fallback"}

    if resolved.get("source") == "fallback":
        contract = resolved.get("contract") or {}
        assert "required_fields" in contract
        assert "endpoint" in contract


def test_generated_netbox_cache_package_exists():
    package_path = Path(__file__).resolve().parent / "generated" / "netbox"
    assert package_path.exists()
