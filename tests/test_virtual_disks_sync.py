"""Regression tests for virtual disk synchronization."""

import asyncio

from proxbox_api.services.sync.virtual_disks import create_virtual_disks


def test_create_virtual_disks_uses_custom_fields_proxmox_vm_id(monkeypatch):
    calls = {"get_vm_config": []}
    reconciled = []

    async def _fake_rest_list(_nb, _path):
        if _path == "/api/virtualization/virtual-machines/":
            return [
                {
                    "id": 7,
                    "name": "vm-101",
                    "cluster": {"name": "cluster-a"},
                    "custom_fields": {"proxmox_vm_id": 101},
                }
            ]
        if _path == "/api/plugins/proxbox/storage/":
            return [
                {
                    "id": 42,
                    "cluster": {"name": "cluster-a"},
                    "name": "local-lvm",
                    "backups": [],
                }
            ]
        return []

    async def _fake_get_vm_config(**kwargs):
        calls["get_vm_config"].append(kwargs)
        return {"scsi0": "local-lvm:vm-101-disk-0,size=20G"}

    async def _fake_reconcile(_nb, _path, lookup, payload, **kwargs):
        reconciled.append((lookup, payload))
        return {"created": True}

    monkeypatch.setattr(
        "proxbox_api.services.sync.virtual_disks.rest_list_async",
        _fake_rest_list,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.virtual_disks.get_vm_config",
        _fake_get_vm_config,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.virtual_disks.rest_reconcile_async",
        _fake_reconcile,
    )

    result = asyncio.run(
        create_virtual_disks(
            netbox_session=object(),
            pxs=[],
            cluster_status=[],
            cluster_resources=[
                {"cluster-a": [{"type": "qemu", "name": "vm-101", "vmid": "101", "node": "pve01"}]}
            ],
            tag=None,
            use_websocket=False,
            use_css=False,
        )
    )

    assert result == {"count": 1, "created": 1, "updated": 0, "skipped": 0}
    assert calls["get_vm_config"] == [
        {
            "pxs": [],
            "cluster_status": [],
            "node": "pve01",
            "type": "qemu",
            "vmid": "101",
        }
    ]
    assert len(reconciled) == 1
    assert reconciled[0][1]["storage"] == 42
    assert reconciled[0][0] == {"virtual_machine_id": 7, "name": "scsi0"}
