"""Tests for guest-agent MAC indexing helpers."""

from __future__ import annotations

from proxbox_api.services.sync.individual.helpers import resolve_guest_interface
from proxbox_api.services.sync.vm_helpers import (
    build_guest_mac_index,
    merged_guest_iface_for_mac,
)


def test_merged_guest_iface_for_mac_aggregates_shared_config_mac_ips():
    guest_interfaces = [
        {
            "name": "lo",
            "mac_address": "",
            "ip_addresses": [{"ip_address": "127.0.0.1", "prefix": 8, "ip_address_type": "ipv4"}],
        },
        {
            "name": "eth0",
            "mac_address": "bc:24:11:20:99:1e",
            "ip_addresses": [
                {"ip_address": "10.85.0.52", "prefix": 22, "ip_address_type": "ipv4"},
                {
                    "ip_address": "fe80::bc24:11ff:fe20:991e",
                    "prefix": 64,
                    "ip_address_type": "ipv6",
                },
            ],
        },
        {
            "name": "ens19",
            "mac_address": "bc:24:11:9b:ab:78",
            "ip_addresses": [{"ip_address": "10.81.0.13", "prefix": 22, "ip_address_type": "ipv4"}],
        },
        {
            "name": "ens18",
            "mac_address": "bc:24:11:20:99:1e",
            "ip_addresses": [
                {"ip_address": "10.83.4.100", "prefix": 23, "ip_address_type": "ipv4"}
            ],
        },
    ]

    index = build_guest_mac_index(guest_interfaces)
    assert [iface["name"] for iface in index["bc:24:11:20:99:1e"]] == ["eth0", "ens18"]
    assert [iface["name"] for iface in index["bc:24:11:9b:ab:78"]] == ["ens19"]
    assert "" not in index

    merged_shared = merged_guest_iface_for_mac(guest_interfaces, "BC:24:11:20:99:1E")
    assert merged_shared is not None
    assert merged_shared["name"] == "eth0"
    assert [(addr["ip_address"], addr["prefix"]) for addr in merged_shared["ip_addresses"]] == [
        ("10.85.0.52", 22),
        ("fe80::bc24:11ff:fe20:991e", 64),
        ("10.83.4.100", 23),
    ]

    merged_single = merged_guest_iface_for_mac(guest_interfaces, "bc:24:11:9b:ab:78")
    assert merged_single is guest_interfaces[2]
    assert merged_single["ip_addresses"] == [
        {"ip_address": "10.81.0.13", "prefix": 22, "ip_address_type": "ipv4"}
    ]


def test_merged_guest_iface_for_mac_returns_single_iface_unchanged():
    guest_iface = {
        "name": "ens19",
        "mac_address": "bc:24:11:9b:ab:78",
        "hostname": "vm-one",
        "ip_addresses": [
            {"ip_address": "10.81.0.13", "prefix": 22, "ip_address_type": "ipv4"},
            {"ip_address": "2804:2cac::10:81:0:13", "prefix": 64, "ip_address_type": "ipv6"},
        ],
    }

    merged = merged_guest_iface_for_mac([guest_iface], "BC:24:11:9B:AB:78")

    assert merged is guest_iface
    assert merged["ip_addresses"] is guest_iface["ip_addresses"]


def test_merged_guest_iface_for_mac_no_match_preserves_name_fallback():
    guest_interfaces = [
        {
            "name": "ens18",
            "mac_address": "bc:24:11:20:99:1e",
            "ip_addresses": [{"ip_address": "10.85.0.52", "prefix": 22, "ip_address_type": "ipv4"}],
        }
    ]

    assert merged_guest_iface_for_mac(guest_interfaces, "bc:24:11:aa:bb:cc") is None

    guest_iface, resolved_name, resolved_mac = resolve_guest_interface(
        guest_interfaces,
        "ens18",
        "bc:24:11:aa:bb:cc",
    )
    assert guest_iface is guest_interfaces[0]
    assert resolved_name == "ens18"
    assert resolved_mac == "bc:24:11:aa:bb:cc"
