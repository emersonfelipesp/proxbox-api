"""Regression tests for VM snapshot synchronization."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

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

    async def _fake_bulk_reconcile(_nb, _path, *, payloads, **kwargs):
        # Capture lookup info from the payloads for test verification
        if _path == "/api/plugins/proxbox/snapshots/":
            for payload in payloads:
                reconciled.append(
                    (
                        {
                            "vmid": payload.get("vmid"),
                            "name": payload.get("name"),
                            "node": payload.get("node"),
                        },
                        payload,
                    )
                )
        # Return bulk reconcile result with created snapshots
        from proxbox_api.netbox_rest import BulkReconcileResult

        return BulkReconcileResult(
            records=[{"id": 99, **payload} for payload in payloads],
            created=len(payloads),
            updated=0,
            unchanged=0,
            failed=0,
        )

    monkeypatch.setattr(
        "proxbox_api.services.sync.snapshots.rest_list_async",
        _fake_rest_list,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.snapshots.get_vm_snapshots",
        _fake_get_vm_snapshots,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.snapshots.rest_bulk_reconcile_async",
        _fake_bulk_reconcile,
    )

    px = type("P", (), {"session": proxmox_session, "name": "lab"})()
    result = asyncio.run(
        create_virtual_machine_snapshots(
            netbox_session=session,
            pxs=[px],
            cluster_status=[],
            cluster_resources=[
                {"cluster-a": [{"type": "qemu", "name": "vm-101", "vmid": "101", "node": "pve01"}]}
            ],
            tag=None,
            use_websocket=False,
        )
    )

    assert result == {"count": 1, "created": 1, "updated": 0, "skipped": 0, "deleted": 0}
    assert calls["get_vm_snapshots"] == [
        {"session": px, "node": "pve01", "vm_type": "qemu", "vmid": 101}
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
        "query": {"virtual_machine_id": 7, "ordering": "name"},
    }
    assert len(reconciled) == 1
    assert reconciled[0][0] == {"vmid": 101, "name": "pre-upgrade", "node": "pve01"}
    assert reconciled[0][1]["virtual_machine"] == 7
    assert reconciled[0][1]["proxmox_storage"] == 41


def test_create_virtual_machine_snapshots_scopes_fetch_by_endpoint(monkeypatch):
    endpoint_a = SimpleNamespace(db_endpoint_id=1, name="pve")
    endpoint_b = SimpleNamespace(db_endpoint_id=2, name="astro")
    calls = {"get_vm_snapshots": []}

    async def _fake_rest_list(_nb, _path, query=None):
        if _path == "/api/virtualization/virtual-machines/":
            return [
                {
                    "id": 7,
                    "name": "vm-105-a",
                    "cluster": {"name": "pve"},
                    "custom_fields": {
                        "proxmox_endpoint_id": 1,
                        "proxmox_vm_id": 105,
                        "proxmox_vm_type": "qemu",
                        "proxmox_node": "pve",
                    },
                },
                {
                    "id": 8,
                    "name": "vm-105-b",
                    "cluster": {"name": "astro"},
                    "custom_fields": {
                        "proxmox_endpoint_id": 2,
                        "proxmox_vm_id": 105,
                        "proxmox_vm_type": "qemu",
                        "proxmox_node": "astro",
                    },
                },
            ]
        if _path in {"/api/plugins/proxbox/storage/", "/api/virtualization/virtual-disks/"}:
            return []
        return []

    def _fake_get_vm_snapshots(*, session, node, vm_type, vmid):
        calls["get_vm_snapshots"].append(
            {"session": session, "node": node, "vm_type": vm_type, "vmid": vmid}
        )
        return [{"name": f"snap-{node}", "type": "qemu"}]

    async def _fake_bulk_reconcile(_nb, _path, *, payloads, **kwargs):
        from proxbox_api.netbox_rest import BulkReconcileResult

        return BulkReconcileResult(
            records=[{"id": index, **payload} for index, payload in enumerate(payloads, start=1)],
            created=len(payloads),
            updated=0,
            unchanged=0,
            failed=0,
        )

    monkeypatch.setattr("proxbox_api.services.sync.snapshots.rest_list_async", _fake_rest_list)
    monkeypatch.setattr(
        "proxbox_api.services.sync.snapshots.get_vm_snapshots",
        _fake_get_vm_snapshots,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.snapshots.rest_bulk_reconcile_async",
        _fake_bulk_reconcile,
    )

    result = asyncio.run(
        create_virtual_machine_snapshots(
            netbox_session=object(),
            pxs=[endpoint_a, endpoint_b],
            cluster_status=[],
            cluster_resources=[
                {"pve": [{"type": "qemu", "name": "vm-105-a", "vmid": "105", "node": "pve"}]},
                {"astro": [{"type": "qemu", "name": "vm-105-b", "vmid": "105", "node": "astro"}]},
            ],
            tag=None,
            use_websocket=False,
        )
    )

    assert result == {"count": 2, "created": 2, "updated": 0, "skipped": 0, "deleted": 0}
    assert calls["get_vm_snapshots"] == [
        {"session": endpoint_a, "node": "pve", "vm_type": "qemu", "vmid": 105},
        {"session": endpoint_b, "node": "astro", "vm_type": "qemu", "vmid": 105},
    ]
