"""Tests for live NetBox version detection and capability gates."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from proxbox_api.netbox_version import detect_netbox_version, parse_netbox_version
from proxbox_api.services.sync.vm_create import (
    create_or_update_virtual_machine,
    ensure_vm_type,
)


class _StatusResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.data = payload


class _StatusClient:
    def __init__(self, version: str) -> None:
        self.version = version
        self.calls: list[tuple[str, str]] = []

    async def request(self, method: str, path: str, **kwargs: object) -> _StatusResponse:
        self.calls.append((method, path))
        return _StatusResponse({"netbox-version": self.version})


def _netbox_api(version: str) -> SimpleNamespace:
    return SimpleNamespace(client=_StatusClient(version))


async def _empty_sync_state_sidecars(*args: object, **kwargs: object) -> list[object]:
    return []


async def _skip_vm_sync_state_write(*args: object, **kwargs: object) -> None:
    return None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4.6.0", (4, 6, 0)),
        ("4.6.4", (4, 6, 4)),
        ("4.6.0-beta2", (4, 6, 0)),
        ("v4.5.9", (4, 5, 9)),
        ("4.5", (4, 5, 0)),
        (None, (0, 0, 0)),
        ("not-a-version", (0, 0, 0)),
    ],
)
def test_parse_netbox_version(raw: str | None, expected: tuple[int, int, int]) -> None:
    assert parse_netbox_version(raw) == expected


@pytest.mark.asyncio
async def test_detect_netbox_version_caches_status_result() -> None:
    nb = _netbox_api("4.6.4")

    assert await detect_netbox_version(nb) == (4, 6, 4)
    assert await detect_netbox_version(nb) == (4, 6, 4)
    assert nb.client.calls == [("GET", "/api/status/")]


@pytest.mark.asyncio
async def test_ensure_vm_type_skips_virtual_machine_type_before_netbox_46(monkeypatch) -> None:
    nb = _netbox_api("4.5.9")

    async def _unexpected_reconcile(*args: object, **kwargs: object) -> object:
        raise AssertionError("VirtualMachineType should not be reconciled before NetBox 4.6")

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_create.rest_reconcile_async",
        _unexpected_reconcile,
    )

    assert await ensure_vm_type(nb, "qemu", [{"id": 7}]) is None
    assert nb.client.calls == [("GET", "/api/status/")]


@pytest.mark.asyncio
async def test_ensure_vm_type_reconciles_virtual_machine_type_on_netbox_46(monkeypatch) -> None:
    nb = _netbox_api("4.6.4")
    captured: dict[str, object] = {}

    async def _fake_reconcile(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(id=18)

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_create.rest_reconcile_async",
        _fake_reconcile,
    )

    result = await ensure_vm_type(nb, "qemu", [{"id": 7}])

    assert getattr(result, "id") == 18
    assert captured["args"][1] == "/api/virtualization/virtual-machine-types/"
    assert captured["kwargs"]["lookup"] == {"slug": "qemu-virtual-machine"}


@pytest.mark.asyncio
async def test_create_or_update_vm_uses_legacy_vm_type_payload_before_netbox_46(
    monkeypatch,
) -> None:
    nb = _netbox_api("4.5.9")
    captured: dict[str, object] = {}

    async def _fake_reconcile(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"id": 101}

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_create.rest_reconcile_async",
        _fake_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.sync_state_reader.rest_list_async",
        _empty_sync_state_sidecars,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_create.write_virtual_machine_sync_state",
        _skip_vm_sync_state_write,
    )
    monkeypatch.setattr(
        "proxbox_api.services.custom_fields.get_plugin_bool",
        lambda *, settings_key, default=False: (
            True if settings_key == "custom_fields_enabled" else default
        ),
    )

    await create_or_update_virtual_machine(
        nb,
        proxmox_resource={
            "vmid": 101,
            "name": "vm-101",
            "node": "pve01",
            "type": "qemu",
            "status": "running",
            "maxcpu": 2,
            "maxmem": 2 * 1024 * 1024 * 1024,
            "maxdisk": 0,
        },
        proxmox_config={},
        cluster_id=1,
        device_id=2,
        role_id=3,
        tag_id=7,
        tag_refs=[{"id": 7}],
        cluster_name="cluster-a",
        virtual_machine_type_id=None,
    )

    kwargs = captured["kwargs"]
    payload = kwargs["payload"]
    assert payload["role"] == 3
    assert "virtual_machine_type" not in payload
    assert payload["custom_fields"]["proxmox_vm_type"] == "qemu"
    assert "virtual_machine_type" not in kwargs["patchable_fields"]
    assert nb.client.calls == [("GET", "/api/status/")]


@pytest.mark.asyncio
async def test_create_or_update_vm_ignores_stale_vm_type_id_before_netbox_46(
    monkeypatch,
) -> None:
    nb = _netbox_api("4.5.9")
    captured: dict[str, object] = {}

    async def _fake_reconcile(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"id": 103}

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_create.rest_reconcile_async",
        _fake_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.sync_state_reader.rest_list_async",
        _empty_sync_state_sidecars,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_create.write_virtual_machine_sync_state",
        _skip_vm_sync_state_write,
    )
    monkeypatch.setattr(
        "proxbox_api.services.custom_fields.get_plugin_bool",
        lambda *, settings_key, default=False: (
            True if settings_key == "custom_fields_enabled" else default
        ),
    )

    await create_or_update_virtual_machine(
        nb,
        proxmox_resource={
            "vmid": 103,
            "name": "vm-103",
            "node": "pve01",
            "type": "qemu",
            "status": "running",
            "maxcpu": 2,
            "maxmem": 2 * 1024 * 1024 * 1024,
            "maxdisk": 0,
        },
        proxmox_config={},
        cluster_id=1,
        device_id=2,
        role_id=3,
        tag_id=7,
        tag_refs=[{"id": 7}],
        cluster_name="cluster-a",
        virtual_machine_type_id=18,
    )

    payload = captured["kwargs"]["payload"]
    assert payload["role"] == 3
    assert "virtual_machine_type" not in payload
    assert payload["custom_fields"]["proxmox_vm_type"] == "qemu"
    assert "virtual_machine_type" not in captured["kwargs"]["patchable_fields"]
    assert nb.client.calls == [("GET", "/api/status/")]


@pytest.mark.asyncio
async def test_create_or_update_vm_uses_native_vm_type_payload_on_netbox_46(
    monkeypatch,
) -> None:
    nb = _netbox_api("4.6.4")
    captured: dict[str, object] = {}

    async def _fake_reconcile(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return {"id": 102}

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_create.rest_reconcile_async",
        _fake_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.sync_state_reader.rest_list_async",
        _empty_sync_state_sidecars,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_create.write_virtual_machine_sync_state",
        _skip_vm_sync_state_write,
    )
    monkeypatch.setattr(
        "proxbox_api.services.custom_fields.get_plugin_bool",
        lambda *, settings_key, default=False: (
            True if settings_key == "custom_fields_enabled" else default
        ),
    )

    await create_or_update_virtual_machine(
        nb,
        proxmox_resource={
            "vmid": 102,
            "name": "vm-102",
            "node": "pve01",
            "type": "lxc",
            "status": "running",
            "maxcpu": 2,
            "maxmem": 1024 * 1024 * 1024,
            "maxdisk": 0,
        },
        proxmox_config={},
        cluster_id=1,
        device_id=2,
        role_id=3,
        tag_id=7,
        tag_refs=[{"id": 7}],
        cluster_name="cluster-a",
        virtual_machine_type_id=18,
    )

    kwargs = captured["kwargs"]
    payload = kwargs["payload"]
    assert payload["virtual_machine_type"] == 18
    assert "role" not in payload
    assert payload["custom_fields"]["proxmox_vm_type"] == "lxc"
    assert "virtual_machine_type" in kwargs["patchable_fields"]
    assert nb.client.calls == [("GET", "/api/status/")]


@pytest.mark.asyncio
async def test_ensure_vm_type_uses_preresolved_version_without_network_call(monkeypatch) -> None:
    # When a pre-resolved netbox_version is supplied, detect_netbox_version must not
    # be called (the /api/status/ endpoint must never be reached).
    nb = _netbox_api("4.6.4")

    async def _fake_reconcile(*args: object, **kwargs: object) -> object:
        return SimpleNamespace(id=19)

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_create.rest_reconcile_async",
        _fake_reconcile,
    )

    result = await ensure_vm_type(nb, "qemu", [{"id": 7}], netbox_version=(4, 6, 0))

    assert getattr(result, "id") == 19
    # No network call — the version was pre-resolved.
    assert nb.client.calls == []


@pytest.mark.asyncio
async def test_ensure_vm_type_preresolved_below_threshold_skips_without_network_call(
    monkeypatch,
) -> None:
    # Pre-resolved version below NetBox 4.6 threshold: function returns None,
    # the reconcile path is not reached, and no network call occurs.
    nb = _netbox_api("4.5.9")

    async def _unexpected_reconcile(*args: object, **kwargs: object) -> object:
        raise AssertionError("VirtualMachineType should not be reconciled below NetBox 4.6")

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_create.rest_reconcile_async",
        _unexpected_reconcile,
    )

    result = await ensure_vm_type(nb, "qemu", [{"id": 7}], netbox_version=(4, 5, 9))

    assert result is None
    assert nb.client.calls == []
