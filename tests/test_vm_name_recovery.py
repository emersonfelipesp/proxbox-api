"""Regression tests for blank-name VM recovery and interface failure surfacing.

These cover the fix for VMs that fail to synchronize when they have many
interfaces, and for NetBox VM rows that end up with a blank ``name``:

1. A blank-name NetBox VM is still matchable by its ``proxmox_vm_id`` custom
   field instead of being rejected with HTTP 422.
2. Only a VM with neither a name nor a ``proxmox_vm_id`` is rejected.
3. A blank Proxmox VM name is normalized to a deterministic ``vm-<vmid>`` so a
   nameless NetBox record is never created in the first place.
4. Transient per-interface failures are retried and, when they persist, are
   counted and surfaced rather than silently swallowed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from proxbox_api.proxmox_to_netbox.models import ProxmoxVmResourceInput
from proxbox_api.routes.virtualization.virtual_machines import create_virtual_machines
from proxbox_api.routes.virtualization.virtual_machines.sync_vm import (
    create_virtual_machine_by_netbox_id,
)

_TAG = SimpleNamespace(id=1, name="Proxbox", slug="proxbox", color="ff5722")


def test_by_netbox_id_matches_by_vmid_when_name_blank(monkeypatch):
    """A blank-name NetBox VM must still match Proxmox by proxmox_vm_id."""
    captured: dict[str, object] = {}

    async def _fake_create_virtual_machines(**kwargs):
        captured["cluster_resources"] = kwargs["cluster_resources"]
        return [{"id": 551, "name": "real-name-from-proxmox"}]

    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.create_virtual_machines",
        _fake_create_virtual_machines,
    )

    # NetBox record has a blank name but a known proxmox_vm_id.
    vm_record = SimpleNamespace(
        serialize=lambda: {
            "id": 551,
            "name": "",
            "cluster": {"id": 10, "name": "cluster-a"},
            "custom_fields": {"proxmox_vm_id": 9551},
        }
    )
    fake_nb = SimpleNamespace(
        virtualization=SimpleNamespace(
            virtual_machines=SimpleNamespace(get=lambda id: vm_record if id == 551 else None)
        )
    )
    cluster_resources = [
        {"cluster-a": [{"type": "qemu", "name": "real-name-from-proxmox", "vmid": 9551}]},
        {"cluster-a": [{"type": "qemu", "name": "other", "vmid": 9999}]},
    ]

    result = asyncio.run(
        create_virtual_machine_by_netbox_id(
            netbox_vm_id=551,
            netbox_session=fake_nb,
            pxs=[],
            cluster_status=[],
            cluster_resources=cluster_resources,
            custom_fields=[],
            tag=_TAG,
        )
    )

    assert result == [{"id": 551, "name": "real-name-from-proxmox"}]
    # Only the vmid-matched resource is forwarded to the create flow.
    assert captured["cluster_resources"] == [
        {"cluster-a": [{"type": "qemu", "name": "real-name-from-proxmox", "vmid": 9551}]}
    ]


def test_by_netbox_id_raises_422_when_name_and_vmid_missing():
    """With neither a name nor a proxmox_vm_id, the VM cannot be matched."""
    vm_record = SimpleNamespace(
        serialize=lambda: {
            "id": 551,
            "name": "",
            "cluster": {"id": 10, "name": "cluster-a"},
            "custom_fields": {},
        }
    )
    fake_nb = SimpleNamespace(
        virtualization=SimpleNamespace(virtual_machines=SimpleNamespace(get=lambda id: vm_record))
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            create_virtual_machine_by_netbox_id(
                netbox_vm_id=551,
                netbox_session=fake_nb,
                pxs=[],
                cluster_status=[],
                cluster_resources=[],
                custom_fields=[],
                tag=_TAG,
            )
        )
    assert excinfo.value.status_code == 422
    assert "proxmox_vm_id" in str(excinfo.value.detail)


def test_proxmox_vm_resource_input_fills_blank_name():
    """A blank Proxmox name falls back to a deterministic vm-<vmid>."""
    resource = ProxmoxVmResourceInput(vmid=101, name="", node="pve01", type="qemu")
    assert resource.name == "vm-101"


def test_proxmox_vm_resource_input_keeps_real_name():
    """A real Proxmox name is left untouched."""
    resource = ProxmoxVmResourceInput(vmid=101, name="gateway", node="pve01", type="qemu")
    assert resource.name == "gateway"


def _full_vm_sync_scaffold(monkeypatch, interface_impl):
    """Wire up the heavy create_virtual_machines dependencies with mocks.

    ``interface_impl`` is the (async) ``_create_vm_interface_parallel`` stand-in.
    Returns the list of created VM records.
    """

    async def _fake_reconcile(*args, **kwargs):
        lookup = kwargs.get("lookup") or {}
        if lookup.get("cf_proxmox_vm_id") == 101:
            return {"id": 101, "name": "vm-101", "primary_ip4": None}
        return {"id": 1, "name": kwargs.get("payload", {}).get("name")}

    async def _fake_ensure(*args, **kwargs):
        return SimpleNamespace(id=1)

    async def _fake_rest_list(*args, **kwargs):
        return []

    async def _fake_create_vm_disk_parallel(**kwargs):
        return {"id": 88}

    async def _fake_task_history(**kwargs):
        return 0

    async def _fake_set_primary_ip(**kwargs):
        return True

    base = "proxbox_api.routes.virtualization.virtual_machines.sync_vm"
    monkeypatch.setattr(f"{base}.rest_reconcile_async", _fake_reconcile)
    monkeypatch.setattr(f"{base}.rest_list_async", _fake_rest_list)
    monkeypatch.setattr(
        f"{base}.get_vm_config",
        lambda **kwargs: {
            "onboot": 1,
            "agent": 1,
            "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0,ip=1.1.1.3/24",
        },
    )
    monkeypatch.setattr(
        f"{base}.get_qemu_guest_agent_network_interfaces",
        lambda *args, **kwargs: [],
    )
    for name in (
        "_ensure_cluster_type",
        "_ensure_cluster",
        "_ensure_manufacturer",
        "_ensure_device_type",
        "_ensure_site",
        "_ensure_device",
        "_ensure_proxmox_node_role",
        "ensure_vm_type",
    ):
        monkeypatch.setattr(f"{base}.{name}", _fake_ensure)
    monkeypatch.setattr(
        f"{base}.build_netbox_virtual_machine_payload",
        lambda **kwargs: {"name": "vm-101", "status": "active", "cluster": 1},
    )
    monkeypatch.setattr(f"{base}._create_vm_interface_parallel", interface_impl)
    monkeypatch.setattr(f"{base}._create_vm_disk_parallel", _fake_create_vm_disk_parallel)
    monkeypatch.setattr(f"{base}.sync_virtual_machine_task_history", _fake_task_history)
    monkeypatch.setattr("proxbox_api.services.sync.vm_network.set_primary_ip", _fake_set_primary_ip)

    return asyncio.run(
        create_virtual_machines(
            netbox_session=object(),
            pxs=[],
            cluster_status=[SimpleNamespace(name="cluster-a", mode="cluster")],
            cluster_resources=[
                {"cluster-a": [{"type": "qemu", "name": "vm-101", "vmid": 101, "node": "pve01"}]}
            ],
            custom_fields=[],
            tag=_TAG,
        )
    )


def test_interface_creation_retries_transient_failure(monkeypatch):
    """A transient interface failure is retried instead of dropped."""
    calls = {"count": 0}

    async def _flaky_interface(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("transient NetBox 503")
        return {
            "interface": {"id": 66, "name": kwargs["interface_name"]},
            "all_ips": [{"id": 77, "address": "1.1.1.3/24"}],
        }

    result = _full_vm_sync_scaffold(monkeypatch, _flaky_interface)

    assert result == [{"id": 101, "name": "vm-101", "primary_ip4": None}]
    # First attempt raised, second attempt (the retry) succeeded.
    assert calls["count"] == 2


def test_interface_creation_failure_does_not_abort_vm(monkeypatch):
    """A persistently failing interface is counted, not allowed to abort the VM."""

    async def _always_fail(**kwargs):
        raise RuntimeError("permanent NetBox error")

    # The VM record must still be returned even though its interface never syncs.
    result = _full_vm_sync_scaffold(monkeypatch, _always_fail)
    assert result == [{"id": 101, "name": "vm-101", "primary_ip4": None}]
