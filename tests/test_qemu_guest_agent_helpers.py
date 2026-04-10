"""Tests for QEMU guest-agent helper functions."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from proxbox_api.services.proxmox_helpers import get_qemu_guest_agent_network_interfaces
from proxbox_api.services.sync.vm_helpers import all_guest_agent_ips


class _FakeNestedResource:
    def __init__(self, payload):
        self._payload = payload

    def get(self, **kwargs):
        return self._payload


class _FakeGuestAgentAccessor:
    def __init__(self, payload=None, error=None):
        self._payload = payload if payload is not None else {"result": []}
        self._error = error

    def __call__(self, command):
        if self._error:
            raise self._error
        assert command == "network-get-interfaces"
        return _FakeNestedResource(self._payload)

    def get(self, **kwargs):
        if self._error:
            raise self._error
        assert kwargs.get("command") == "network-get-interfaces"
        return self._payload


class _FakeQemuAccessor:
    def __init__(self, payload=None, error=None):
        self.agent = _FakeGuestAgentAccessor(payload=payload, error=error)


class _FakeNodesAccessor:
    def __init__(self, payload=None, error=None):
        self._payload = payload
        self._error = error

    def qemu(self, vmid):
        assert vmid == 101
        return _FakeQemuAccessor(payload=self._payload, error=self._error)


class _FakeSessionApi:
    def __init__(self, payload=None, error=None):
        self._payload = payload
        self._error = error

    def nodes(self, node):
        assert node == "pve01"
        return _FakeNodesAccessor(payload=self._payload, error=self._error)


class _FakeProxmoxSession:
    def __init__(self, payload=None, error=None):
        self.session = _FakeSessionApi(payload=payload, error=error)


def test_get_qemu_guest_agent_network_interfaces_returns_normalized_payload():
    session = _FakeProxmoxSession(
        payload={
            "result": [
                {
                    "name": "ens18",
                    "hardware-address": "AA:BB:CC:DD:EE:FF",
                    "ip-addresses": [
                        {
                            "ip-address": "10.10.10.50",
                            "prefix": 24,
                            "ip-address-type": "ipv4",
                        },
                        {
                            "ip-address": "fe80::1",
                            "prefix": 64,
                            "ip-address-type": "ipv6",
                        },
                    ],
                }
            ]
        }
    )
    result = get_qemu_guest_agent_network_interfaces(session, node="pve01", vmid=101)
    assert result == [
        {
            "name": "ens18",
            "mac_address": "AA:BB:CC:DD:EE:FF",
            "ip_addresses": [
                {"ip_address": "10.10.10.50", "prefix": 24, "ip_address_type": "ipv4"},
                {"ip_address": "fe80::1", "prefix": 64, "ip_address_type": "ipv6"},
            ],
        }
    ]


def test_get_qemu_guest_agent_network_interfaces_returns_empty_when_agent_unavailable():
    session = _FakeProxmoxSession(error=RuntimeError("QEMU guest agent is not running"))
    result = get_qemu_guest_agent_network_interfaces(session, node="pve01", vmid=101)
    assert result == []


# --- Tests for all_guest_agent_ips ---


def _make_guest_iface(ips: list[dict]) -> dict:
    """Build a normalized guest agent interface dict."""
    return {"name": "ens18", "mac_address": "de:2f:ee:0e:9a:4b", "ip_addresses": ips}


def test_all_guest_agent_ips_returns_all_non_loopback():
    iface = _make_guest_iface([
        {"ip_address": "168.0.96.30", "prefix": 27, "ip_address_type": "ipv4"},
        {"ip_address": "2804:2cac::168:0:96:30", "prefix": 64, "ip_address_type": "ipv6"},
        {"ip_address": "127.0.0.1", "prefix": 8, "ip_address_type": "ipv4"},  # loopback — excluded
        {"ip_address": "::1", "prefix": 128, "ip_address_type": "ipv6"},       # loopback — excluded
    ])
    result = all_guest_agent_ips(iface, ignore_ipv6_link_local=False)
    assert "168.0.96.30/27" in result
    assert "2804:2cac::168:0:96:30/64" in result
    assert "127.0.0.1/8" not in result
    assert "::1/128" not in result
    assert len(result) == 2


def test_all_guest_agent_ips_filters_link_local_by_default():
    iface = _make_guest_iface([
        {"ip_address": "168.0.96.30", "prefix": 27, "ip_address_type": "ipv4"},
        {"ip_address": "2804:2cac::168:0:96:30", "prefix": 64, "ip_address_type": "ipv6"},
        {"ip_address": "fe80::dc2f:eeff:fe0e:9a4b", "prefix": 64, "ip_address_type": "ipv6"},
    ])
    result_filtered = all_guest_agent_ips(iface, ignore_ipv6_link_local=True)
    result_all = all_guest_agent_ips(iface, ignore_ipv6_link_local=False)

    assert "168.0.96.30/27" in result_filtered
    assert "2804:2cac::168:0:96:30/64" in result_filtered
    assert "fe80::dc2f:eeff:fe0e:9a4b/64" not in result_filtered
    assert len(result_filtered) == 2

    assert "fe80::dc2f:eeff:fe0e:9a4b/64" in result_all
    assert len(result_all) == 3


def test_all_guest_agent_ips_returns_empty_for_none():
    assert all_guest_agent_ips(None) == []


def test_all_guest_agent_ips_returns_empty_for_no_ips():
    iface = _make_guest_iface([])
    assert all_guest_agent_ips(iface) == []


def test_all_guest_agent_ips_returns_empty_for_loopback_only():
    iface = _make_guest_iface([
        {"ip_address": "127.0.0.1", "prefix": 8, "ip_address_type": "ipv4"},
        {"ip_address": "::1", "prefix": 128, "ip_address_type": "ipv6"},
    ])
    assert all_guest_agent_ips(iface) == []


# --- Tests for cleanup_stale_ips_for_interface ---


def test_cleanup_stale_ips_preserves_current_ips():
    """IPs in current_ips set must not be deleted."""

    async def _run():
        existing = [
            {"id": 1, "address": "168.0.96.30/27"},
            {"id": 2, "address": "2804:2cac::168:0:96:30/64"},
        ]
        with patch(
            "proxbox_api.netbox_rest.rest_list_async",
            new=AsyncMock(return_value=existing),
        ):
            with patch(
                "proxbox_api.netbox_rest.rest_bulk_delete_async",
                new=AsyncMock(return_value=0),
            ) as mock_delete:
                from proxbox_api.services.sync.network import cleanup_stale_ips_for_interface

                deleted = await cleanup_stale_ips_for_interface(
                    nb=object(),
                    interface_id=152,
                    current_ips={"168.0.96.30/27", "2804:2cac::168:0:96:30/64"},
                )
                # Nothing stale — delete should not be called
                mock_delete.assert_not_called()
                assert deleted == 0

    asyncio.run(_run())


def test_cleanup_stale_ips_deletes_only_stale():
    """IPs not in current_ips must be deleted."""

    async def _run():
        existing = [
            {"id": 1, "address": "168.0.96.30/27"},   # current — keep
            {"id": 99, "address": "10.0.0.1/24"},       # stale — delete
            {"id": 100, "address": "192.168.1.1/32"},   # stale — delete
        ]
        with patch(
            "proxbox_api.netbox_rest.rest_list_async",
            new=AsyncMock(return_value=existing),
        ):
            with patch(
                "proxbox_api.netbox_rest.rest_bulk_delete_async",
                new=AsyncMock(return_value=2),
            ) as mock_delete:
                from proxbox_api.services.sync.network import cleanup_stale_ips_for_interface

                await cleanup_stale_ips_for_interface(
                    nb=object(),
                    interface_id=152,
                    current_ips={"168.0.96.30/27"},
                )
                mock_delete.assert_called_once()
                deleted_ids = mock_delete.call_args[0][2]
                assert 99 in deleted_ids
                assert 100 in deleted_ids
                assert 1 not in deleted_ids

    asyncio.run(_run())


def test_cleanup_stale_ips_returns_zero_when_no_existing():
    """If NetBox returns no IPs for the interface, cleanup is a no-op."""

    async def _run():
        with patch(
            "proxbox_api.netbox_rest.rest_list_async",
            new=AsyncMock(return_value=[]),
        ):
            from proxbox_api.services.sync.network import cleanup_stale_ips_for_interface

            count = await cleanup_stale_ips_for_interface(
                nb=object(),
                interface_id=152,
                current_ips={"168.0.96.30/27"},
            )
            assert count == 0

    asyncio.run(_run())
