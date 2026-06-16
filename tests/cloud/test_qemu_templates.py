"""Tests for live QEMU Cloud-Init template discovery."""

from __future__ import annotations

from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.routes.cloud.qemu_templates import (
    _as_bool,
    _cloud_init_drive_keys,
    _template_from_record,
)


def test_truthy_template_flags_match_proxmox_shapes() -> None:
    assert _as_bool(True) is True
    assert _as_bool(1) is True
    assert _as_bool("1") is True
    assert _as_bool("true") is True
    assert _as_bool(False) is False
    assert _as_bool("0") is False


def test_cloud_init_drive_keys_detects_common_bus_slots() -> None:
    config = {
        "scsi0": "local-zfs:vm-9017-disk-0,size=8G",
        "ide2": "local:cloudinit",
        "cicustom": "user=local:snippets/vm-9017-user.yaml",
        "net0": "virtio=AA:BB:CC:DD:EE:FF",
    }

    assert _cloud_init_drive_keys(config) == ["ide2"]


def test_template_from_record_normalizes_live_template() -> None:
    endpoint = ProxmoxEndpoint(
        id=7,
        name="pve-10-0-30-71",
        ip_address="10.0.30.71",
        username="root@pam",
    )

    template = _template_from_record(
        endpoint=endpoint,
        cluster_name="prod-cluster-01",
        record={
            "type": "qemu",
            "vmid": 9017,
            "name": "pdns-auth-ubuntu-2404",
            "node": "pve01",
            "status": "stopped",
            "template": 1,
            "maxmem": 1073741824,
            "maxdisk": 8589934592,
        },
        config={"ide2": "local:cloudinit", "cicustom": "user=local:snippets/pdns.yaml"},
    )

    assert template is not None
    assert template.id == 9017
    assert template.endpoint_id == 7
    assert template.cluster_name == "prod-cluster-01"
    assert template.source_vmid == 9017
    assert template.target_node == "pve01"
    assert template.cloud_init is True
    assert template.cloud_init_drives == ["ide2"]
