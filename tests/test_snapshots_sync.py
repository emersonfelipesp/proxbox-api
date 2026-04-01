"""Regression tests for VM snapshot synchronization."""

from __future__ import annotations

import asyncio

from proxbox_api.services.sync.snapshots import create_virtual_machine_snapshots


def test_create_virtual_machine_snapshots_uses_nested_custom_fields_proxmox_vm_id(
    monkeypatch,
):
    session = object()
    proxmox_session = object()
    calls = {"get_vm_snapshots": [], "rest_list_async": []}
    reconciled = []

    async def _fake_rest_list(_nb, _path, query=None):
        calls["rest_list_async"].append({"path": _path, "query": query})
        if _path == "/api/plugins/proxbox/storage/":
            return [
                {"id": 41, "cluster": "cluster-a", "name": "local-zfs"},
            ]
        if _path == "/api/virtualization/virtual-disks/":
            return [
                {
                    "id": 301,
                    "virtual_machine": 7,
                    "name": "local-zfs:vm-101-disk-0",
                }
            ]
        return [
            {
                "id": 7,
                "name": "vm-101",
                "custom_fields": {"proxmox_vm_id": 101},
            }
        ]

    def _fake_get_vm_snapshots(*, session, node, vm_type, vmid):
        calls["get_vm_snapshots"].append(
            {"session": session, "node": node, "vm_type": vm_type, "vmid": vmid}
        )
        return [
            {
                "name": "pre-upgrade",
                "description": "Before upgrade",
                "snaptime": 1712345678,
                "type": "qemu",
            }
        ]

    async def _fake_reconcile(_nb, _path, lookup, payload, **kwargs):
        reconciled.append((lookup, payload))
        return {"id": 99, **payload}

    monkeypatch.setattr(
        "proxbox_api.services.sync.snapshots.rest_list_async",
        _fake_rest_list,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.snapshots.get_vm_snapshots",
        _fake_get_vm_snapshots,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.snapshots.rest_reconcile_async",
        _fake_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.snapshots.rest_create_async",
        lambda *args, **kwargs: asyncio.sleep(0, result={"id": 1}),
    )

    result = asyncio.run(
        create_virtual_machine_snapshots(
            netbox_session=session,
            pxs=[type("P", (), {"session": proxmox_session, "name": "lab"})()],
            cluster_status=[],
            cluster_resources=[
                {"cluster-a": [{"type": "qemu", "name": "vm-101", "vmid": "101", "node": "pve01"}]}
            ],
            tag=None,
            use_websocket=False,
        )
    )

    assert result == {"count": 1, "created": 1, "updated": 0, "skipped": 0}
    assert calls["get_vm_snapshots"] == [
        {"session": proxmox_session, "node": "pve01", "vm_type": "qemu", "vmid": 101}
    ]
    assert calls["rest_list_async"][0] == {
        "path": "/api/virtualization/virtual-machines/",
        "query": {"limit": 500, "offset": 0},
    }
    assert calls["rest_list_async"][1] == {
        "path": "/api/plugins/proxbox/storage/",
        "query": None,
    }
    assert calls["rest_list_async"][2] == {
        "path": "/api/virtualization/virtual-disks/",
        "query": {"virtual_machine_id": 101, "ordering": "name"},
    }
    assert len(reconciled) == 1
    assert reconciled[0][0] == {"vmid": 101, "name": "pre-upgrade", "node": "pve01"}
    assert reconciled[0][1]["virtual_machine"] == 7
    assert reconciled[0][1]["proxmox_storage"] == 41
