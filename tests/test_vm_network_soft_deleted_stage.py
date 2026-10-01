"""VM interface and IP stages must not touch decommissioned or soft-deleted VMs."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from proxbox_api.routes.virtualization.virtual_machines import sync_vm


def _snapshot_vm(record_id: int, vmid: int, **extra: object) -> dict[str, object]:
    return {
        "id": record_id,
        "name": f"vm-{vmid}",
        "proxmox_vm_id": vmid,
        "proxmox_endpoint_id": 1,
        "cluster": {"id": 3, "name": "lab"},
        "status": {"value": "active"},
        **extra,
    }


def _stage_inputs() -> dict[str, object]:
    return {
        "netbox_session": SimpleNamespace(client=object()),
        "pxs": [SimpleNamespace(name="lab", db_endpoint_id=1, session=object())],
        "cluster_status": [
            SimpleNamespace(name="lab", mode="cluster", node_list=[SimpleNamespace(name="pve01")])
        ],
        "cluster_resources": [
            {
                "lab": [
                    {"type": "qemu", "name": f"vm-{vmid}", "node": "pve01", "vmid": vmid}
                    for vmid in (101, 102)
                ]
            }
        ],
        "tag": SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
    }


def _install_stage_fakes(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: list[dict[str, object]],
) -> list[object]:
    """Serve ``snapshot`` as the NetBox VM list; return the VMIDs whose config is fetched."""
    fetched_vmids: list[object] = []

    async def _inline_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def _load_snapshot(_nb: object, **_kwargs: object) -> list[dict[str, object]]:
        return list(snapshot)

    async def _hydrate(_nb: object, vms: list[dict[str, object]], **_kwargs: object):
        return vms

    async def _get_vm_config(**kwargs: object) -> dict[str, object]:
        fetched_vmids.append(kwargs.get("vmid"))
        return {}

    monkeypatch.setattr(asyncio, "to_thread", _inline_to_thread)
    monkeypatch.setattr(sync_vm, "_load_netbox_virtual_machine_snapshot", _load_snapshot)
    monkeypatch.setattr(sync_vm, "hydrate_vm_identities_from_sidecars", _hydrate)
    monkeypatch.setattr(sync_vm, "get_vm_config", _get_vm_config)
    monkeypatch.setattr(sync_vm, "resolve_vm_sync_concurrency", lambda: 1)
    return fetched_vmids


_STAGES = [
    pytest.param(sync_vm.create_only_vm_interfaces, "VM interface", id="interfaces"),
    pytest.param(sync_vm.create_only_vm_ip_addresses, "VM IP address", id="ip-addresses"),
]

_SOFT_DELETED_MARKERS = [
    {"status": {"value": "decommissioning", "label": "Decommissioning"}},
    {"status": "decommissioning"},
    {"tags": [{"slug": "proxbox-soft-deleted"}]},
    {"tags": ["proxbox-soft-deleted"]},
]


@pytest.mark.parametrize(("stage", "stage_label"), _STAGES)
@pytest.mark.parametrize("marker", _SOFT_DELETED_MARKERS)
def test_vm_network_stages_do_not_fetch_config_for_soft_deleted_vms(
    monkeypatch, proxbox_log_capture, stage, stage_label, marker
) -> None:
    fetched_vmids = _install_stage_fakes(
        monkeypatch,
        [_snapshot_vm(7, 101, **marker), _snapshot_vm(8, 102)],
    )

    asyncio.run(stage(**_stage_inputs()))

    assert fetched_vmids == [102]
    assert (
        f"Skipping 1 decommissioned or soft-deleted VM(s) during {stage_label} sync"
        in proxbox_log_capture.messages(logging.INFO)
    )


@pytest.mark.parametrize(("stage", "stage_label"), _STAGES)
def test_vm_network_stages_still_process_every_live_vm(
    monkeypatch, proxbox_log_capture, stage, stage_label
) -> None:
    fetched_vmids = _install_stage_fakes(monkeypatch, [_snapshot_vm(7, 101), _snapshot_vm(8, 102)])

    asyncio.run(stage(**_stage_inputs()))

    assert sorted(str(vmid) for vmid in fetched_vmids) == ["101", "102"]
    assert not any(
        "decommissioned or soft-deleted" in message
        for message in proxbox_log_capture.messages(logging.INFO)
    )
