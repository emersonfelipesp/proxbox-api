"""Tests asserting dns_name flows through the IP-address write paths."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from proxbox_api.schemas.sync import SyncOverwriteFlags
from proxbox_api.services.sync.network import (
    _resolve_vm_interface_ips,
    build_vm_interface_ip_payload,
    bulk_reconcile_vm_interface_ips,
)


def test_build_vm_interface_ip_payload_includes_dns_name():
    payload = build_vm_interface_ip_payload(
        address="192.0.2.10/24",
        interface_id=42,
        tag_refs=[{"slug": "proxbox"}],
        now=datetime(2026, 5, 7, tzinfo=timezone.utc),
        dns_name="my-vm.example.com",
    )
    assert payload["dns_name"] == "my-vm.example.com"


def test_build_vm_interface_ip_payload_defaults_dns_name_to_empty_string():
    payload = build_vm_interface_ip_payload(
        address="192.0.2.10/24",
        interface_id=42,
        tag_refs=[],
        now=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )
    assert payload["dns_name"] == ""


def test_bulk_reconcile_vm_interface_ips_gates_dns_name_with_overwrite_flag():
    async def _run(flag_value: bool) -> frozenset[str]:
        captured: dict[str, frozenset[str]] = {}

        class _FakeResult:
            records: list = []

        async def _fake_bulk(*args, **kwargs):
            captured["patchable_fields"] = kwargs["patchable_fields"]
            return _FakeResult()

        with patch(
            "proxbox_api.services.sync.network.rest_bulk_reconcile_async",
            new=AsyncMock(side_effect=_fake_bulk),
        ):
            flags = SyncOverwriteFlags(
                overwrite_ip_status=False,
                overwrite_ip_tags=False,
                overwrite_ip_custom_fields=False,
                overwrite_ip_address_dns_name=flag_value,
            )
            await bulk_reconcile_vm_interface_ips(
                nb=object(),
                ip_payloads=[
                    {
                        "address": "192.0.2.10/24",
                        "assigned_object_type": "virtualization.vminterface",
                        "assigned_object_id": 1,
                        "status": "active",
                        "dns_name": "host.example.com",
                        "tags": [],
                        "custom_fields": {},
                    }
                ],
                overwrite_flags=flags,
            )
        return captured["patchable_fields"]

    enabled = asyncio.run(_run(True))
    disabled = asyncio.run(_run(False))
    assert "dns_name" in enabled
    assert "dns_name" not in disabled


def test_resolve_vm_interface_ips_forwards_dns_name_to_payload():
    async def _run() -> dict:
        captured: dict = {}

        async def _fake_reconcile(*args, **kwargs):
            captured["payload"] = kwargs["payload"]
            return {"id": 99}

        with (
            patch(
                "proxbox_api.services.sync.network.rest_reconcile_async",
                new=AsyncMock(side_effect=_fake_reconcile),
            ),
            patch(
                "proxbox_api.services.sync.network.cleanup_stale_ips_for_interface",
                new=AsyncMock(return_value=0),
            ),
        ):
            await _resolve_vm_interface_ips(
                nb=object(),
                interface_config={"ip": "192.0.2.10/24"},
                guest_iface=None,
                tag_refs=[],
                interface_id=42,
                interface_name="ens18",
                now=datetime(2026, 5, 7, tzinfo=timezone.utc),
                create_ip=True,
                dns_name="my-vm.example.com",
            )
        return captured["payload"]

    payload = asyncio.run(_run())
    assert payload["dns_name"] == "my-vm.example.com"


def test_resolve_vm_interface_ips_passes_empty_dns_name_when_unset():
    async def _run() -> dict:
        captured: dict = {}

        async def _fake_reconcile(*args, **kwargs):
            captured["payload"] = kwargs["payload"]
            return {"id": 99}

        with (
            patch(
                "proxbox_api.services.sync.network.rest_reconcile_async",
                new=AsyncMock(side_effect=_fake_reconcile),
            ),
            patch(
                "proxbox_api.services.sync.network.cleanup_stale_ips_for_interface",
                new=AsyncMock(return_value=0),
            ),
        ):
            await _resolve_vm_interface_ips(
                nb=object(),
                interface_config={"ip": "192.0.2.10/24"},
                guest_iface=None,
                tag_refs=[],
                interface_id=42,
                interface_name="ens18",
                now=datetime(2026, 5, 7, tzinfo=timezone.utc),
                create_ip=True,
            )
        return captured["payload"]

    payload = asyncio.run(_run())
    assert payload["dns_name"] == ""
