"""Tests for VM interface sync on interface-dense guests.

Models the reported failure: a VRRP router VM whose guest agent returns ~110
interfaces (named NICs, vrrp.* virtual interfaces, and "name:N" alias entries
that share the parent NIC's MAC while carrying the IPv4 service addresses).
"""

from __future__ import annotations

import asyncio

import pytest

from proxbox_api.services import proxmox_helpers
from proxbox_api.services.proxmox_helpers import (
    _GUEST_AGENT_TIMEOUT_HINT,
    _normalize_guest_agent_interfaces,
    fetch_qemu_guest_agent_network_interfaces,
)


def _addr(ip: str, prefix: int, family: str = "ipv4") -> dict:
    return {"ip-address": ip, "prefix": prefix, "ip-address-type": family}


def _dense_payload() -> dict:
    """Reduced model of the reported guest-agent payload."""
    return {
        "result": [
            {
                "name": "lo",
                "hardware-address": "00:00:00:00:00:00",
                "ip-addresses": [_addr("127.0.0.1", 8)],
            },
            {
                "name": "ens19",
                "hardware-address": "bc:24:11:0d:43:b8",
                "ip-addresses": [
                    _addr("10.46.0.31", 24),
                    _addr("fe80::be24:11ff:fe0d:43b8", 64, "ipv6"),
                ],
            },
            {
                "name": "ens20",
                "hardware-address": "bc:24:11:e8:44:8e",
                "ip-addresses": [_addr("fe80::be24:11ff:fee8:448e", 64, "ipv6")],
            },
            {
                "name": "enp2s20",
                "hardware-address": "bc:24:11:33:eb:33",
                "ip-addresses": [_addr("fe80::be24:11ff:fe33:eb33", 64, "ipv6")],
            },
            # VRRP virtual interfaces: unique VRRP MACs, no ip-addresses key.
            {"name": "vrrp.1", "hardware-address": "00:00:5e:00:01:01"},
            {"name": "vrrp.2", "hardware-address": "00:00:5e:00:01:02"},
            {"name": "vrrp.103", "hardware-address": "00:00:5e:00:01:67"},
            # Alias entries: share the parent NIC MAC, carry service IPv4s.
            {
                "name": "ens20:1",
                "hardware-address": "bc:24:11:e8:44:8e",
                "ip-addresses": [_addr("10.31.3.251", 22)],
            },
            {
                "name": "enp2s20:1",
                "hardware-address": "bc:24:11:33:eb:33",
                "ip-addresses": [_addr("10.33.5.251", 23)],
            },
            {
                "name": "enp2s20:3",
                "hardware-address": "bc:24:11:33:eb:33",
                "ip-addresses": [_addr("10.33.5.253", 23)],
            },
            {
                "name": "enp2s20:2",
                "hardware-address": "bc:24:11:33:eb:33",
                "ip-addresses": [_addr("10.33.5.252", 23)],
            },
        ]
    }


def _by_name(interfaces: list[dict]) -> dict[str, dict]:
    return {str(iface["name"]): iface for iface in interfaces}


class TestAliasAggregation:
    def test_alias_entries_merge_into_parent_and_are_dropped(self):
        normalized = _normalize_guest_agent_interfaces(_dense_payload())
        names = set(_by_name(normalized))

        assert "ens20:1" not in names
        assert "enp2s20:1" not in names
        assert "enp2s20:2" not in names
        assert "enp2s20:3" not in names

        ens20 = _by_name(normalized)["ens20"]
        ens20_ips = [a["ip_address"] for a in ens20["ip_addresses"]]
        assert ens20_ips == ["fe80::be24:11ff:fee8:448e", "10.31.3.251"]

        enp2s20 = _by_name(normalized)["enp2s20"]
        enp2s20_ips = [a["ip_address"] for a in enp2s20["ip_addresses"]]
        assert enp2s20_ips == [
            "fe80::be24:11ff:fe33:eb33",
            "10.33.5.251",
            "10.33.5.253",
            "10.33.5.252",
        ]

    def test_vrrp_and_regular_entries_are_preserved(self):
        normalized = _normalize_guest_agent_interfaces(_dense_payload())
        names = set(_by_name(normalized))
        assert {"lo", "ens19", "ens20", "enp2s20", "vrrp.1", "vrrp.2", "vrrp.103"} <= names

        ens19 = _by_name(normalized)["ens19"]
        assert [a["ip_address"] for a in ens19["ip_addresses"]] == [
            "10.46.0.31",
            "fe80::be24:11ff:fe0d:43b8",
        ]
        vrrp = _by_name(normalized)["vrrp.1"]
        assert vrrp["ip_addresses"] == []

    def test_duplicate_addresses_across_aliases_are_deduped(self):
        payload = {
            "result": [
                {
                    "name": "ens18",
                    "hardware-address": "aa:aa:aa:aa:aa:01",
                    "ip-addresses": [_addr("10.0.0.1", 24)],
                },
                {
                    "name": "ens18:1",
                    "hardware-address": "aa:aa:aa:aa:aa:01",
                    "ip-addresses": [_addr("10.0.0.1", 24), _addr("10.0.0.2", 24)],
                },
            ]
        }
        normalized = _normalize_guest_agent_interfaces(payload)
        assert len(normalized) == 1
        assert [a["ip_address"] for a in normalized[0]["ip_addresses"]] == [
            "10.0.0.1",
            "10.0.0.2",
        ]

    def test_small_payload_behavior_unchanged(self):
        payload = {
            "result": [
                {
                    "name": "ens18",
                    "hardware-address": "AA:BB:CC:DD:EE:FF",
                    "ip-addresses": [_addr("10.10.10.50", 24)],
                }
            ]
        }
        normalized = _normalize_guest_agent_interfaces(payload)
        assert normalized == [
            {
                "name": "ens18",
                "mac_address": "AA:BB:CC:DD:EE:FF",
                "ip_addresses": [
                    {"ip_address": "10.10.10.50", "prefix": 24, "ip_address_type": "ipv4"}
                ],
            }
        ]

    def test_alias_only_group_without_parent_keeps_first_alias(self):
        payload = {
            "result": [
                {
                    "name": "eth0:1",
                    "hardware-address": "aa:aa:aa:aa:aa:02",
                    "ip-addresses": [_addr("10.1.0.1", 24)],
                },
                {
                    "name": "eth0:2",
                    "hardware-address": "aa:aa:aa:aa:aa:02",
                    "ip-addresses": [_addr("10.1.0.2", 24)],
                },
            ]
        }
        normalized = _normalize_guest_agent_interfaces(payload)
        assert len(normalized) == 1
        assert normalized[0]["name"] == "eth0:1"
        assert [a["ip_address"] for a in normalized[0]["ip_addresses"]] == [
            "10.1.0.1",
            "10.1.0.2",
        ]


class _SlowThenFastAgent:
    """agent("network-get-interfaces").get() stub: sleeps, then answers."""

    def __init__(self, sleep_sequence: list[float], payload: dict):
        self.sleep_sequence = list(sleep_sequence)
        self.payload = payload
        self.calls = 0

    def __call__(self, command):
        assert command == "network-get-interfaces"
        return self

    async def _respond(self) -> dict:
        delay = self.sleep_sequence[min(self.calls, len(self.sleep_sequence) - 1)]
        self.calls += 1
        await asyncio.sleep(delay)
        return self.payload

    def get(self, **kwargs):
        return self._respond()


class _FakeQemu:
    def __init__(self, agent):
        self.agent = agent


class _FakeNodes:
    def __init__(self, agent):
        self._agent = agent

    def qemu(self, vmid):
        return _FakeQemu(self._agent)


class _FakeApi:
    def __init__(self, agent):
        self._agent = agent

    def nodes(self, node):
        return _FakeNodes(self._agent)


class _FakeSession:
    def __init__(self, agent):
        self.session = _FakeApi(agent)


class TestGuestAgentTimeout:
    def test_timeout_then_retry_succeeds(self, monkeypatch):
        monkeypatch.setattr(proxmox_helpers, "_resolve_guest_agent_timeout", lambda: 0.2)
        agent = _SlowThenFastAgent(
            sleep_sequence=[5.0, 0.0],
            payload={"result": [{"name": "ens18", "hardware-address": "aa:bb:cc:dd:ee:01"}]},
        )
        result = fetch_qemu_guest_agent_network_interfaces(
            _FakeSession(agent), node="pve01", vmid=101
        )
        assert agent.calls == 2
        assert result.diagnostic is None
        assert [i["name"] for i in result.interfaces] == ["ens18"]

    def test_double_timeout_surfaces_timeout_diagnostic(self, monkeypatch):
        monkeypatch.setattr(proxmox_helpers, "_resolve_guest_agent_timeout", lambda: 0.1)
        agent = _SlowThenFastAgent(sleep_sequence=[5.0, 5.0], payload={"result": []})
        result = fetch_qemu_guest_agent_network_interfaces(
            _FakeSession(agent), node="pve01", vmid=101
        )
        assert agent.calls == 2
        assert result.interfaces == []
        assert result.diagnostic == _GUEST_AGENT_TIMEOUT_HINT

    def test_default_timeout_resolves_to_fifteen_seconds(self, monkeypatch):
        monkeypatch.delenv("PROXBOX_GUEST_AGENT_TIMEOUT", raising=False)
        monkeypatch.setattr("proxbox_api.runtime_settings._load_settings", lambda: None)
        assert proxmox_helpers._resolve_guest_agent_timeout() == 15

    def test_env_var_overrides_timeout(self, monkeypatch):
        monkeypatch.setenv("PROXBOX_GUEST_AGENT_TIMEOUT", "45")
        assert proxmox_helpers._resolve_guest_agent_timeout() == 45


class TestBulkFailureSurfacing:
    @pytest.mark.asyncio
    async def test_bulk_reconcile_vm_interfaces_reraises(self, monkeypatch):
        from proxbox_api.services.sync import network as network_module

        async def _boom(*args, **kwargs):
            raise RuntimeError("netbox bulk write failed")

        monkeypatch.setattr(network_module, "rest_bulk_reconcile_async", _boom)
        with pytest.raises(RuntimeError, match="netbox bulk write failed"):
            await network_module.bulk_reconcile_vm_interfaces(
                object(), [{"name": "net0", "virtual_machine": 1}]
            )
