"""LXC runtime discovery supplies DHCP IPs without a QEMU guest agent."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from proxbox_api.services.proxmox_helpers import (
    fetch_lxc_network_interfaces,
    get_lxc_network_interfaces,
)
from proxbox_api.services.sync.vm_helpers import all_guest_agent_ips


def _session(payload=None, error=None):
    get = AsyncMock(return_value=payload, side_effect=error)
    lxc = Mock(return_value=SimpleNamespace(interfaces=SimpleNamespace(get=get)))
    nodes = Mock(return_value=SimpleNamespace(lxc=lxc))
    return SimpleNamespace(session=SimpleNamespace(nodes=nodes)), nodes, lxc, get


@pytest.mark.parametrize("legacy", [False, True])
async def test_lxc_runtime_addresses_normalize_and_filter(legacy):
    interface = {
        "name": "eth0",
        "hwaddr": "02:00:00:00:00:01",
        "inet": "192.0.2.10/24",
        "inet6": "fe80::1/64",
    }
    if not legacy:
        interface["hardware-address"] = interface["hwaddr"]
        interface["ip-addresses"] = [
            {"ip-address": "192.0.2.10", "prefix": 24, "ip-address-type": "inet"},
            {"ip-address": "2001:db8::10", "prefix": 64, "ip-address-type": "inet6"},
            {"ip-address": "fe80::1", "prefix": 64, "ip-address-type": "inet6"},
        ]
    session, nodes, lxc, get = _session(
        [
            {"name": "lo", "inet": "127.0.0.1/8", "inet6": "::1/128"},
            interface,
        ]
    )
    result = await fetch_lxc_network_interfaces(session, "pve01", 110)
    assert result.diagnostic is None
    nodes.assert_called_once_with("pve01")
    lxc.assert_called_once_with(110)
    get.assert_awaited_once_with()
    assert result.interfaces[1]["mac_address"] == "02:00:00:00:00:01"
    expected = ["192.0.2.10/24"] if legacy else ["192.0.2.10/24", "2001:db8::10/64"]
    assert all_guest_agent_ips(result.interfaces[1], True) == expected
    assert all_guest_agent_ips(result.interfaces[0], True) == []


@pytest.mark.parametrize("payload", [[], [{"name": "eth0", "inet": "invalid"}]])
async def test_lxc_runtime_empty_or_invalid_address_does_not_invent_ip(payload):
    session, *_ = _session(payload)
    interfaces = await get_lxc_network_interfaces(session, "pve01", 110)
    assert all(not iface["ip_addresses"] for iface in interfaces)


@pytest.mark.parametrize("payload", [{"unexpected": []}, ["invalid-interface"]])
async def test_lxc_runtime_invalid_collection_reports_unavailable(payload):
    session, *_ = _session(payload)
    result = await fetch_lxc_network_interfaces(session, "pve01", 110)
    assert result.interfaces == []
    assert result.diagnostic


async def test_lxc_runtime_failure_preserves_config_fallback_and_redacts_error(monkeypatch):
    warning = Mock()
    monkeypatch.setattr("proxbox_api.services.proxmox_helpers.logger.warning", warning)
    session, *_ = _session(error=PermissionError("synthetic-secret-canary"))
    result = await fetch_lxc_network_interfaces(session, "pve01", 110)
    assert result.interfaces == []
    assert result.diagnostic
    assert "synthetic-secret-canary" not in str(warning.call_args)
    assert "synthetic-secret-canary" not in result.diagnostic


async def test_lxc_runtime_cancellation_propagates():
    session, *_ = _session(error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await fetch_lxc_network_interfaces(session, "pve01", 110)


@pytest.mark.parametrize("status", ["stopped", "paused"])
async def test_stopped_lxc_skips_runtime_request_in_stage_helpers(status, monkeypatch):
    """A container known to be stopped costs no request, warning, or diagnostic."""
    from proxbox_api.routes.virtualization.virtual_machines import sync_vm

    calls = []

    async def _record(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(sync_vm, "get_lxc_network_interfaces", _record)
    monkeypatch.setattr(sync_vm, "fetch_lxc_network_interfaces", _record)

    interfaces = await sync_vm._fetch_vm_interface_guest_data(
        object(), "pve01", "lxc", 110, {}, resource_status=status
    )
    context = SimpleNamespace(pxs=[object()], cluster_status=[SimpleNamespace(name="lab")])
    task_interfaces = await sync_vm._vm_task_guest_interfaces(
        context,
        cluster_name="lab",
        resource={"node": "pve01", "vmid": 110, "name": "ct", "status": status},
        vm_type="lxc",
        vm_config_obj=SimpleNamespace(qemu_agent_enabled=False),
        vm_name="ct",
        timing_key="ct",
    )

    assert interfaces == []
    assert task_interfaces == []
    assert calls == []


@pytest.mark.parametrize("status", ["running", None])
async def test_running_or_unknown_lxc_reads_runtime_interfaces(status, monkeypatch):
    from proxbox_api.routes.virtualization.virtual_machines import sync_vm

    calls = []

    async def _record(session, node, vmid):
        calls.append((node, vmid))
        return [{"name": "eth0"}]

    monkeypatch.setattr(sync_vm, "get_lxc_network_interfaces", _record)

    interfaces = await sync_vm._fetch_vm_interface_guest_data(
        object(), "pve01", "lxc", 110, {}, resource_status=status
    )

    assert interfaces == [{"name": "eth0"}]
    assert calls == [("pve01", 110)]


@pytest.mark.parametrize("legacy", [False, True])
async def test_lxc_address_family_uses_guest_agent_labels(legacy):
    """PVE 9 reports inet/inet6; PVE 8 CIDRs are parsed. Both become ipv4/ipv6."""
    interface = {"name": "eth0", "hwaddr": "02:00:00:00:00:01", "inet": "192.0.2.10/24"}
    if legacy:
        interface["inet6"] = "2001:db8::10/64"
    else:
        interface["hardware-address"] = interface["hwaddr"]
        interface["ip-addresses"] = [
            {"ip-address": "192.0.2.10", "prefix": 24, "ip-address-type": "inet"},
            {"ip-address": "2001:db8::10", "prefix": 64, "ip-address-type": "inet6"},
        ]
    session, *_ = _session([interface])
    interfaces = await get_lxc_network_interfaces(session, "pve01", 110)
    labels = [a["ip_address_type"] for a in interfaces[0]["ip_addresses"]]
    assert labels == ["ipv4", "ipv6"]
