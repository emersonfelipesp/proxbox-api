from __future__ import annotations

from types import SimpleNamespace

import pytest

from proxbox_api.exception import ProxboxException
from proxbox_api.services.sync.individual import vm_sync


def _patch(monkeypatch, resource):
    async def _config(*_a, **_k):
        return {"onboot": 1}

    async def _resource(*_a, **_k):
        if isinstance(resource, Exception):
            raise resource
        return resource

    async def _no_existing(*_a, **_k):
        return None

    monkeypatch.setattr(vm_sync, "get_vm_config_individual", _config)
    monkeypatch.setattr(vm_sync, "get_vm_resource_individual", _resource)
    monkeypatch.setattr(vm_sync, "_lookup_existing_vm_for_dry_run", _no_existing)


async def _run():
    return await vm_sync.sync_vm_individual(
        nb=object(),
        px=SimpleNamespace(name="lab"),
        tag=SimpleNamespace(id=7),
        cluster_name="lab",
        node="pve01",
        vm_type="qemu",
        vmid=101,
        dry_run=True,
    )


@pytest.mark.asyncio
async def test_valid_resource_is_used(monkeypatch):
    _patch(
        monkeypatch,
        {"name": "db01", "status": "running", "maxcpu": 4, "maxmem": 8, "maxdisk": 9},
    )
    result = await _run()
    assert result["proxmox_resource"]["name"] == "db01"
    assert result["proxmox_resource"]["maxcpu"] == 4


@pytest.mark.asyncio
async def test_empty_resource_raises_instead_of_placeholder(monkeypatch):
    _patch(monkeypatch, {})
    with pytest.raises(ProxboxException, match="placeholder"):
        await _run()


@pytest.mark.asyncio
async def test_resource_lookup_error_raises(monkeypatch):
    _patch(monkeypatch, RuntimeError("boom"))
    with pytest.raises(ProxboxException, match="placeholder"):
        await _run()
