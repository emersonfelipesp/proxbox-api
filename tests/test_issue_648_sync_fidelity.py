"""Focused regression tests for Gitea issue #648 sync fidelity."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from proxbox_api.proxmox_to_netbox.mappers.virtual_machine import (
    map_proxmox_vm_to_netbox_vm_body,
)
from proxbox_api.proxmox_to_netbox.models import NetBoxVirtualMachineCreateBody
from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.sync import ip_ownership
from proxbox_api.services.sync.network import bulk_reconcile_vm_interface_ips
from proxbox_api.services.sync.vm_helpers import normalize_current_virtual_machine_payload


async def _capture_ip_reconcile(
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing: dict[str, object],
    dns_name: str | None,
    flags: SyncOverwriteFlags,
) -> dict[str, object]:
    captured: dict[str, object] = {}

    async def fake_list(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return [existing]

    async def fake_reconcile(*_args: object, **kwargs: object) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(id=existing["id"])

    monkeypatch.setattr(ip_ownership, "rest_list_async", fake_list)
    monkeypatch.setattr(ip_ownership, "rest_reconcile_async", fake_reconcile)
    await ip_ownership._reconcile_interface_ip(
        object(),
        ip_addr="192.0.2.10/24",
        interface_id=42,
        tag_refs=[{"slug": "proxbox"}],
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        dns_name=dns_name,
        interface_name="net0",
        overwrite_flags=flags,
    )
    return captured


def _owned_ip() -> dict[str, object]:
    return {
        "id": 7,
        "address": "192.0.2.10/24",
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": 42,
        "dns_name": "operator.example",
    }


async def test_existing_dns_name_preserved_when_overwrite_disabled(monkeypatch):
    captured = await _capture_ip_reconcile(
        monkeypatch,
        existing=_owned_ip(),
        dns_name="guest.example",
        flags=SyncOverwriteFlags(overwrite_ip_address_dns_name=False),
    )
    assert "dns_name" not in captured["patchable_fields"]


async def test_existing_dns_name_preserved_when_hostname_unknown(monkeypatch):
    captured = await _capture_ip_reconcile(
        monkeypatch,
        existing=_owned_ip(),
        dns_name=None,
        flags=SyncOverwriteFlags(overwrite_ip_address_dns_name=True),
    )
    assert "dns_name" not in captured["patchable_fields"]


async def test_single_ip_distinguishes_unknown_from_explicit_empty_dns(monkeypatch):
    unknown = await _capture_ip_reconcile(
        monkeypatch,
        existing=_owned_ip(),
        dns_name=None,
        flags=SyncOverwriteFlags(overwrite_ip_address_dns_name=True),
    )
    explicit_empty = await _capture_ip_reconcile(
        monkeypatch,
        existing=_owned_ip(),
        dns_name="",
        flags=SyncOverwriteFlags(overwrite_ip_address_dns_name=True),
    )

    assert unknown["payload"]["dns_name"] == ""
    assert "dns_name" not in unknown["patchable_fields"]
    assert explicit_empty["payload"]["dns_name"] == ""
    assert "dns_name" in explicit_empty["patchable_fields"]


async def test_existing_dns_name_patched_when_hostname_known_and_enabled(monkeypatch):
    captured = await _capture_ip_reconcile(
        monkeypatch,
        existing=_owned_ip(),
        dns_name="guest.example",
        flags=SyncOverwriteFlags(overwrite_ip_address_dns_name=True),
    )
    assert "dns_name" in captured["patchable_fields"]
    assert captured["payload"]["dns_name"] == "guest.example"


async def test_single_ip_status_and_tags_follow_overwrite_flags(monkeypatch):
    captured = await _capture_ip_reconcile(
        monkeypatch,
        existing=_owned_ip(),
        dns_name="guest.example",
        flags=SyncOverwriteFlags(overwrite_ip_status=False, overwrite_ip_tags=False),
    )
    assert captured["patchable_fields"] == frozenset({"address", "dns_name"})


async def test_owned_ip_prefix_change_is_patchable_with_every_overwrite_disabled(monkeypatch):
    existing = _owned_ip()
    existing["address"] = "192.0.2.10/23"
    captured = await _capture_ip_reconcile(
        monkeypatch,
        existing=existing,
        dns_name="guest.example",
        flags=SyncOverwriteFlags(
            overwrite_ip_status=False,
            overwrite_ip_tags=False,
            overwrite_ip_address_dns_name=False,
        ),
    )
    assert captured["patchable_fields"] == frozenset({"address"})
    assert captured["payload"]["address"] == "192.0.2.10/24"
    assert captured["lookup"] == {"id": 7}


async def test_unassigned_ip_adoption_still_patches_assignment(monkeypatch):
    existing = _owned_ip()
    existing["assigned_object_type"] = None
    existing["assigned_object_id"] = None
    captured = await _capture_ip_reconcile(
        monkeypatch,
        existing=existing,
        dns_name=None,
        flags=SyncOverwriteFlags(
            overwrite_ip_status=False,
            overwrite_ip_tags=False,
            overwrite_ip_address_dns_name=False,
        ),
    )
    assert captured["patchable_fields"] == frozenset(
        {"address", "assigned_object_type", "assigned_object_id"}
    )


_RESOURCE = {
    "vmid": 101,
    "name": "guest-101",
    "node": "pve01",
    "status": "running",
    "type": "qemu",
    "maxcpu": 2,
    "maxmem": 2_147_483_648,
}


def _vm_body(config: dict[str, object]) -> dict[str, object]:
    return map_proxmox_vm_to_netbox_vm_body(
        resource=_RESOURCE,
        config=config,
        cluster_id=1,
        device_id=2,
        role_id=3,
        tag_ids=[4],
    )


def test_onboot_truthy_maps_to_netbox_on():
    assert _vm_body({"onboot": 1})["start_on_boot"] == "on"


def test_fetched_config_without_onboot_maps_to_netbox_off():
    assert _vm_body({"cores": 2})["start_on_boot"] == "off"


def test_empty_config_omits_unknown_start_on_boot():
    assert "start_on_boot" not in _vm_body({})


def test_start_on_boot_choice_object_normalizes_for_drift_comparison():
    current = normalize_current_virtual_machine_payload(
        {
            "name": "guest-101",
            "status": "active",
            "start_on_boot": {"value": "on", "label": "On"},
        }
    )
    model = NetBoxVirtualMachineCreateBody.model_validate(current)
    assert model.start_on_boot == "on"


async def test_bulk_ip_reconcile_preserves_first_group_when_second_fails(monkeypatch):
    calls = 0

    async def fake_bulk(*_args: object, **_kwargs: object) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("unknown-hostname group failed")
        return SimpleNamespace(records=[{"id": 11, "address": "192.0.2.11/24"}])

    monkeypatch.setattr(
        "proxbox_api.services.sync.network.rest_bulk_reconcile_async",
        fake_bulk,
    )

    records = await bulk_reconcile_vm_interface_ips(
        object(),
        [
            {"address": "192.0.2.11/24", "assigned_object_id": 1, "dns_name": "a.example"},
            {"address": "192.0.2.12/24", "assigned_object_id": 1, "dns_name": None},
        ],
    )

    assert calls == 2
    assert records == [{"id": 11, "address": "192.0.2.11/24"}]


async def test_bulk_ip_reconcile_deduplicates_lookup_across_dns_groups(monkeypatch):
    captured: list[list[dict[str, object]]] = []

    async def fake_bulk(*_args: object, **kwargs: object) -> SimpleNamespace:
        captured.append(kwargs["payloads"])
        return SimpleNamespace(records=[{"id": 12}])

    monkeypatch.setattr(
        "proxbox_api.services.sync.network.rest_bulk_reconcile_async",
        fake_bulk,
    )

    records = await bulk_reconcile_vm_interface_ips(
        object(),
        [
            {"address": "192.0.2.12/24", "assigned_object_id": 2, "dns_name": "a.example"},
            {"address": "192.0.2.12/24", "assigned_object_id": 2, "dns_name": None},
        ],
    )

    assert captured == [
        [{"address": "192.0.2.12/24", "assigned_object_id": 2, "dns_name": "a.example"}]
    ]
    assert records == [{"id": 12}]


async def test_bulk_ip_reconcile_never_allows_unknown_or_disabled_dns_patch(monkeypatch):
    captured: list[frozenset[str]] = []

    async def fake_bulk(*_args: object, **kwargs: object) -> SimpleNamespace:
        captured.append(kwargs["patchable_fields"])
        return SimpleNamespace(records=[])

    monkeypatch.setattr(
        "proxbox_api.services.sync.network.rest_bulk_reconcile_async",
        fake_bulk,
    )
    await bulk_reconcile_vm_interface_ips(
        object(),
        [
            {"address": "192.0.2.13/24", "assigned_object_id": 3, "dns_name": "a.example"},
            {"address": "192.0.2.14/24", "assigned_object_id": 3, "dns_name": None},
        ],
        overwrite_flags=SyncOverwriteFlags(overwrite_ip_address_dns_name=False),
    )

    assert captured == [frozenset({"status", "tags"}), frozenset({"status", "tags"})]


async def test_bulk_ip_reconcile_distinguishes_unknown_from_explicit_empty_dns(monkeypatch):
    captured: list[tuple[list[dict[str, object]], frozenset[str]]] = []

    async def fake_bulk(*_args: object, **kwargs: object) -> SimpleNamespace:
        captured.append((kwargs["payloads"], kwargs["patchable_fields"]))
        return SimpleNamespace(records=[])

    monkeypatch.setattr(
        "proxbox_api.services.sync.network.rest_bulk_reconcile_async",
        fake_bulk,
    )
    await bulk_reconcile_vm_interface_ips(
        object(),
        [
            {"address": "192.0.2.15/24", "assigned_object_id": 4, "dns_name": ""},
            {"address": "192.0.2.16/24", "assigned_object_id": 4, "dns_name": None},
        ],
        overwrite_flags=SyncOverwriteFlags(),
    )

    assert captured == [
        (
            [{"address": "192.0.2.15/24", "assigned_object_id": 4, "dns_name": ""}],
            frozenset({"status", "tags", "dns_name"}),
        ),
        (
            [{"address": "192.0.2.16/24", "assigned_object_id": 4, "dns_name": None}],
            frozenset({"status", "tags"}),
        ),
    ]
