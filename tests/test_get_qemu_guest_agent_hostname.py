"""Tests for the QEMU guest-agent hostname helper."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from proxbox_api.services.proxmox_helpers import (
    get_qemu_guest_agent_hostname,
    sanitize_dns_hostname,
)


class _FakeAgentResource:
    def __init__(self, payload):
        self._payload = payload

    def get(self, **kwargs):
        return self._payload


class _FakeAgentAccessor:
    def __init__(self, hostname_payload=None, hostname_error=None):
        self._payload = hostname_payload
        self._error = hostname_error

    def __call__(self, command):
        if self._error:
            raise self._error
        assert command == "get-host-name"
        return _FakeAgentResource(self._payload)

    def get(self, **kwargs):
        if self._error:
            raise self._error
        assert kwargs.get("command") == "get-host-name"
        return self._payload


class _FakeQemuAccessor:
    def __init__(self, hostname_payload=None, hostname_error=None):
        self.agent = _FakeAgentAccessor(
            hostname_payload=hostname_payload,
            hostname_error=hostname_error,
        )


class _FakeNodesAccessor:
    def __init__(self, hostname_payload=None, hostname_error=None):
        self._payload = hostname_payload
        self._error = hostname_error

    def qemu(self, vmid):
        assert vmid == 101
        return _FakeQemuAccessor(
            hostname_payload=self._payload,
            hostname_error=self._error,
        )


class _FakeSessionApi:
    def __init__(self, hostname_payload=None, hostname_error=None):
        self._payload = hostname_payload
        self._error = hostname_error

    def nodes(self, node):
        assert node == "pve01"
        return _FakeNodesAccessor(
            hostname_payload=self._payload,
            hostname_error=self._error,
        )


class _FakeProxmoxSession:
    def __init__(self, hostname_payload=None, hostname_error=None):
        self.session = _FakeSessionApi(
            hostname_payload=hostname_payload,
            hostname_error=hostname_error,
        )


def test_sanitize_dns_hostname_strips_and_lowercases():
    assert sanitize_dns_hostname("  Web-01.Example.COM. ") == "web-01.example.com"


def test_sanitize_dns_hostname_drops_localhost_and_empty():
    assert sanitize_dns_hostname("") is None
    assert sanitize_dns_hostname(None) is None
    assert sanitize_dns_hostname("localhost") is None
    assert sanitize_dns_hostname("localhost.localdomain") is None
    assert sanitize_dns_hostname("   ") is None


def test_sanitize_dns_hostname_caps_length():
    long_value = "a" * 300
    result = sanitize_dns_hostname(long_value)
    assert result is not None
    assert len(result) == 255


def test_get_qemu_guest_agent_hostname_returns_normalized_value():
    session = _FakeProxmoxSession(hostname_payload={"result": {"host-name": "Web-01.Example.COM"}})
    assert get_qemu_guest_agent_hostname(session, node="pve01", vmid=101) == "web-01.example.com"


def test_get_qemu_guest_agent_hostname_returns_none_when_agent_unavailable():
    session = _FakeProxmoxSession(hostname_error=RuntimeError("guest agent disconnected"))
    with patch(
        "proxbox_api.services.proxmox_helpers.get_qemu_guest_agent_network_interfaces",
        new=AsyncMock(return_value=[]),
    ):
        assert get_qemu_guest_agent_hostname(session, node="pve01", vmid=101) is None


def test_get_qemu_guest_agent_hostname_falls_back_to_network_interfaces():
    session = _FakeProxmoxSession(hostname_payload={"result": {}})
    with patch(
        "proxbox_api.services.proxmox_helpers.get_qemu_guest_agent_network_interfaces",
        new=AsyncMock(return_value=[{"name": "ens18", "fqdn": "node-fallback.example.com"}]),
    ):
        assert (
            get_qemu_guest_agent_hostname(session, node="pve01", vmid=101)
            == "node-fallback.example.com"
        )


def test_get_qemu_guest_agent_hostname_drops_localhost_payload():
    session = _FakeProxmoxSession(hostname_payload={"result": {"host-name": "localhost"}})
    with patch(
        "proxbox_api.services.proxmox_helpers.get_qemu_guest_agent_network_interfaces",
        new=AsyncMock(return_value=[]),
    ):
        assert get_qemu_guest_agent_hostname(session, node="pve01", vmid=101) is None
