"""Tests for QEMU guest-agent helper functions."""

from __future__ import annotations

from proxbox_api.services.proxmox_helpers import get_qemu_guest_agent_network_interfaces


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
