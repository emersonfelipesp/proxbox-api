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


def test_build_vm_interface_ip_payload_strips_zone_id():
    """A zone-scoped global IPv6 reaches NetBox without the %eth0 suffix."""
    payload = build_vm_interface_ip_payload(
        address="2001:db8::1%eth0/64",
        interface_id=42,
        tag_refs=[],
        now=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )
    assert payload is not None
    assert payload["address"] == "2001:db8::1/64"


def test_build_vm_interface_ip_payload_drops_link_local():
    """fe80::… is dropped (returns None) when the toggle is on (default)."""
    payload = build_vm_interface_ip_payload(
        address="fe80::1%eth0/64",
        interface_id=42,
        tag_refs=[],
        now=datetime(2026, 5, 7, tzinfo=timezone.utc),
    )
    assert payload is None


def test_build_vm_interface_ip_payload_keeps_link_local_when_opted_in():
    """Operator opt-in keeps fe80::… (zone-stripped) for diagnostic VMs."""
    payload = build_vm_interface_ip_payload(
        address="fe80::1%eth0/64",
        interface_id=42,
        tag_refs=[],
        now=datetime(2026, 5, 7, tzinfo=timezone.utc),
        ignore_ipv6_link_local=False,
    )
    assert payload is not None
    assert payload["address"] == "fe80::1/64"


def test_build_vm_interface_ip_payload_drops_loopback():
    """Loopback addresses are always skipped, with or without prefix."""
    assert (
        build_vm_interface_ip_payload(
            address="127.0.0.1/8",
            interface_id=42,
            tag_refs=[],
            now=datetime(2026, 5, 7, tzinfo=timezone.utc),
        )
        is None
    )


def test_resolve_vm_interface_ips_emits_phase_summary_for_skipped_ips():
    """fe80::1%eth0 in guest agent yields a phase_summary with skipped=1."""

    async def _run() -> tuple[list[dict], list[tuple[str, dict]]]:
        from proxbox_api.utils.streaming import WebSocketSSEBridge

        bridge = WebSocketSSEBridge()
        seen_payloads: list[dict] = []

        async def _fake_reconcile(*args, **kwargs):
            seen_payloads.append(kwargs["payload"])
            return {"id": len(seen_payloads)}

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
                interface_config={},
                guest_iface={
                    "ip_addresses": [
                        {
                            "ip_address": "fe80::1%eth0",
                            "prefix": 64,
                            "ip_address_type": "ipv6",
                        },
                        {
                            "ip_address": "2001:db8::1%eth0",
                            "prefix": 64,
                            "ip_address_type": "ipv6",
                        },
                        {
                            "ip_address": "192.0.2.1",
                            "prefix": 24,
                            "ip_address_type": "ipv4",
                        },
                    ]
                },
                tag_refs=[],
                interface_id=42,
                interface_name="ens18",
                now=datetime(2026, 5, 7, tzinfo=timezone.utc),
                create_ip=True,
                bridge=bridge,
                vm_name="vm-100",
            )

        await bridge.close()
        frames: list[tuple[str, dict]] = []
        async for raw in bridge.iter_sse():
            lines = [line for line in raw.strip().splitlines() if line]
            event = lines[0].replace("event: ", "", 1)
            import json

            data = json.loads(lines[1].replace("data: ", "", 1))
            frames.append((event, data))
        return seen_payloads, frames

    payloads, frames = asyncio.run(_run())

    posted_addresses = {p["address"] for p in payloads}
    assert posted_addresses == {"2001:db8::1/64", "192.0.2.1/24"}

    summaries = [
        data
        for event, data in frames
        if event == "phase_summary" and data.get("phase") == "vm-ip-addresses"
    ]
    assert len(summaries) == 1
    assert summaries[0]["result"]["skipped"] == 1
    assert "vm-100.ens18" in summaries[0].get("message", "")


def test_resolve_vm_interface_ips_no_summary_when_nothing_skipped():
    """If no IPs are filtered out, no phase_summary frame is emitted."""

    async def _run() -> list[tuple[str, dict]]:
        from proxbox_api.utils.streaming import WebSocketSSEBridge

        bridge = WebSocketSSEBridge()

        async def _fake_reconcile(*args, **kwargs):
            return {"id": 1}

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
                interface_config={},
                guest_iface={
                    "ip_addresses": [
                        {
                            "ip_address": "192.0.2.1",
                            "prefix": 24,
                            "ip_address_type": "ipv4",
                        },
                    ]
                },
                tag_refs=[],
                interface_id=42,
                interface_name="ens18",
                now=datetime(2026, 5, 7, tzinfo=timezone.utc),
                create_ip=True,
                bridge=bridge,
            )

        await bridge.close()
        frames: list[tuple[str, dict]] = []
        async for raw in bridge.iter_sse():
            import json

            lines = [line for line in raw.strip().splitlines() if line]
            event = lines[0].replace("event: ", "", 1)
            data = json.loads(lines[1].replace("data: ", "", 1))
            frames.append((event, data))
        return frames

    frames = asyncio.run(_run())
    assert not [event for event, _ in frames if event == "phase_summary"]


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
