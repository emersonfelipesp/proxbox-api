"""Individual VM sync paths must restore the status of a re-adopted soft-deleted VM."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from proxbox_api.constants import SOFT_DELETE_TAG_SLUG
from proxbox_api.services.sync import orphan_sweep, sync_state_reader
from proxbox_api.services.sync.individual import vm_sync as individual_vm_sync
from proxbox_api.services.sync.individual.vm_sync import sync_vm_individual
from proxbox_api.services.sync.vm_create import create_or_update_virtual_machine
from tests.test_netbox_version import (
    _empty_sync_state_sidecars,
    _netbox_api,
    _skip_vm_sync_state_write,
)


def _decommissioned_vm(record_id: int) -> dict[str, object]:
    return {
        "id": record_id,
        "name": "vm",
        "status": {"value": "decommissioning"},
        "tags": [{"id": 7, "slug": "proxbox"}, {"id": 9, "slug": SOFT_DELETE_TAG_SLUG}],
    }


def _capture_marker_patch(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    patches: list[dict[str, object]] = []

    async def _patch(
        _nb: object, _path: str, _record_id: int, payload: dict[str, object]
    ) -> dict[str, object]:
        patches.append(payload)
        return payload

    monkeypatch.setattr(orphan_sweep, "rest_patch_async", _patch)
    return patches


def _assert_restored(patches: list[dict[str, object]], status: str) -> None:
    assert len(patches) == 1
    assert patches[0]["status"] == status
    assert patches[0]["tags"] == [{"id": 7}]


@pytest.mark.asyncio
async def test_create_or_update_vm_restores_status_for_reappeared_vm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patches = _capture_marker_patch(monkeypatch)

    async def _reconcile(*_args: object, **_kwargs: object) -> object:
        return _decommissioned_vm(101)

    monkeypatch.setattr("proxbox_api.services.sync.vm_create.rest_reconcile_async", _reconcile)
    monkeypatch.setattr(
        "proxbox_api.services.sync.sync_state_reader.rest_list_async", _empty_sync_state_sidecars
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_create.write_virtual_machine_sync_state",
        _skip_vm_sync_state_write,
    )

    await create_or_update_virtual_machine(
        _netbox_api("4.5.9"),
        proxmox_resource={
            "vmid": 101,
            "name": "vm-101",
            "node": "pve01",
            "type": "qemu",
            "status": "running",
            "maxcpu": 2,
            "maxmem": 2 * 1024**3,
            "maxdisk": 0,
        },
        proxmox_config={},
        cluster_id=1,
        device_id=2,
        role_id=3,
        tag_id=7,
        tag_refs=[{"id": 7}],
        cluster_name="cluster-a",
    )

    _assert_restored(patches, "active")


@pytest.mark.asyncio
async def test_sync_vm_individual_restores_status_for_reappeared_vm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patches = _capture_marker_patch(monkeypatch)

    async def _deps(self, cluster_name, node_name, vm_type):
        return tuple(SimpleNamespace(id=10 + i) for i in range(9))

    async def _config(*_a, **_k):
        return {"onboot": 1}

    async def _resource(*_a, **_k):
        return {
            "vmid": 101,
            "name": "db01",
            "node": "pve01",
            "type": "qemu",
            "status": "running",
            "maxcpu": 4,
            "maxmem": 8_000_000_000,
            "maxdisk": 120_000_000_000,
        }

    async def _empty(*_a, **_k):
        return []

    async def _reconcile(*_a, **_k):
        return _decommissioned_vm(55)

    async def _none(*_a, **_k):
        return {"id": 1}

    monkeypatch.setattr(
        individual_vm_sync.BaseIndividualSyncService, "_get_or_create_vm_dependencies", _deps
    )
    monkeypatch.setattr(individual_vm_sync, "get_vm_config_individual", _config)
    monkeypatch.setattr(individual_vm_sync, "get_vm_resource_individual", _resource)
    monkeypatch.setattr(individual_vm_sync, "rest_list_async", _empty)
    monkeypatch.setattr(sync_state_reader, "rest_list_async", _empty)
    monkeypatch.setattr(individual_vm_sync, "rest_reconcile_async", _reconcile)
    monkeypatch.setattr(individual_vm_sync, "write_virtual_machine_sync_state", _none)
    monkeypatch.setattr(individual_vm_sync, "stamp_vm_last_run_id", _none)
    sync_state_reader.reset_sidecar_reader_availability_cache()

    await sync_vm_individual(
        nb=object(),
        px=SimpleNamespace(name="lab"),
        tag=SimpleNamespace(id=7),
        cluster_name="lab",
        node="pve01",
        vm_type="qemu",
        vmid=101,
    )

    _assert_restored(patches, "active")
