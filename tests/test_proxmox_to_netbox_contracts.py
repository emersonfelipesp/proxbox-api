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
        # disk_mb must equal the aggregate of parsed VM-config disks so the
        # NetBox `disk == sum(virtualdisks.size)` aggregate validator passes
        # on update (issue #349). 100 GiB + 50 GiB = 153600 MiB.
        "scsi0": "local-lvm:vm-101-disk-0,size=100G",
        "scsi1": "local-lvm:vm-101-disk-1,size=50G",
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
    assert body["memory"] == 8192
    assert body["disk"] == 153600
    assert body["custom_fields"]["proxmox_vm_id"] == 101
    assert body["custom_fields"]["proxmox_start_at_boot"] is True


def test_map_proxmox_vm_to_netbox_vm_body_no_parseable_disks():
    """When VM config has no parseable disks, disk must be 0 (not maxdisk).

    Regression: issue #349 — the previous fallback to ``resource.maxdisk``
    caused the VM-level ``disk`` to disagree with the sum of created
    ``virtual-disks`` whenever passthrough/raw entries were silently dropped,
    failing NetBox 4.5+ aggregate validation on update.
    """
    resource = {
        "vmid": 102,
        "name": "raw-vm",
        "node": "pve01",
        "status": "running",
        "type": "qemu",
        "maxcpu": 2,
        "maxmem": 4_294_967_296,
        "maxdisk": 53_687_091_200,
    }
    config = {"onboot": 1}
    body = map_proxmox_vm_to_netbox_vm_body(
        resource=resource,
        config=config,
        cluster_id=11,
        device_id=22,
        role_id=33,
        tag_ids=[7],
    )
    assert body["disk"] == 0


def test_load_proxmox_generated_openapi_present():
    document = load_proxmox_generated_openapi(version_tag="latest")
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
    package_path = Path(__file__).resolve().parents[1] / "proxbox_api" / "generated" / "netbox"
    assert package_path.exists()
