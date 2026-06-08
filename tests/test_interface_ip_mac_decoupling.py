"""Tests for the decoupled IP/MAC gating flags on VM interface sync.

Covers four scenarios:
  (a) assign_vm_interface_ips=False — skips IP reconciliation but keeps interface and MAC.
  (b) sync_vm_interface_macs=False — skips MAC reconciliation but keeps interface and IP.
  (c) Both default True — original behavior unchanged.
  (d) create_virtual_machines_stream exposes both query params with default True.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import datetime, timezone
from types import SimpleNamespace

from proxbox_api.routes.virtualization.virtual_machines import sync_vm
from proxbox_api.routes.virtualization.virtual_machines.sync_vm import (
    _create_vm_interface_parallel,
    create_virtual_machines_stream,
)
from proxbox_api.schemas.sync import SyncOverwriteFlags

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=timezone.utc)


def _minimal_vm() -> dict:
    return {"id": 55, "name": "vm-test"}


def _interface_config_with_ip() -> dict:
    return {
        "name": "eth0",
        "virtio": "AA:BB:CC:DD:EE:FF",
        "ip": "10.0.0.5/24",
    }


def _tag_refs() -> list:
    return [{"id": 7}]


def _install_interface_parallel_stubs(monkeypatch):
    """Patch the external calls made inside _create_vm_interface_parallel."""
    mac_calls: list[dict] = []
    ip_calls: list[dict] = []

    async def _fake_reconcile(nb, path, lookup, payload, **kwargs):
        if path == "/api/virtualization/interfaces/":
            return {"id": 66, "name": payload.get("name")}
        if path == "/api/ipam/ip-addresses/":
            return {"id": 77, "address": payload.get("address")}
        return {"id": 99}

    async def _fake_reconcile_mac(nb, *, vminterface_id, mac, tag_refs=None):
        mac_calls.append({"vminterface_id": vminterface_id, "mac": mac})
        return (vminterface_id, "updated")

    async def _fake_resolve_ips(
        nb,
        interface_config,
        guest_iface,
        tag_refs,
        *,
        interface_id,
        interface_name,
        now,
        create_ip,
        **kwargs,
    ):
        if not create_ip:
            return []
        ip_calls.append({"interface_id": interface_id, "interface_name": interface_name})
        return [(77, "10.0.0.5/24")]

    async def _fake_ensure_bridge(*args, **kwargs):
        return None

    # rest_reconcile_async is used at module level in sync_vm.py
    monkeypatch.setattr(
        "proxbox_api.routes.virtualization.virtual_machines.sync_vm.rest_reconcile_async",
        _fake_reconcile,
    )
    # bridge_interfaces is imported locally; patch at source
    monkeypatch.setattr(
        "proxbox_api.services.sync.bridge_interfaces.ensure_bridge_interfaces",
        _fake_ensure_bridge,
    )
    # mac_address is imported locally; patch at source
    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.reconcile_mac_for_vm_interface",
        _fake_reconcile_mac,
    )
    # network is imported locally; patch at source
    monkeypatch.setattr(
        "proxbox_api.services.sync.network._resolve_vm_interface_ips",
        _fake_resolve_ips,
    )

    return mac_calls, ip_calls


# ---------------------------------------------------------------------------
# (a) assign_vm_interface_ips=False
# ---------------------------------------------------------------------------


def test_create_vm_interface_parallel_skips_ip_when_create_ip_false(monkeypatch):
    """When create_ip=False, no IP reconciliation happens; interface and MAC still created."""
    mac_calls, ip_calls = _install_interface_parallel_stubs(monkeypatch)

    result = asyncio.run(
        _create_vm_interface_parallel(
            nb=SimpleNamespace(),
            virtual_machine=_minimal_vm(),
            interface_name="eth0",
            interface_config=_interface_config_with_ip(),
            guest_iface=None,
            tag_refs=_tag_refs(),
            use_guest_agent_interface_name=False,
            ignore_ipv6_link_local_addresses=True,
            now=_NOW,
            create_ip=False,
            sync_mac=True,
        )
    )

    # Interface must be present
    assert result["interface"] is not None
    assert result["interface"].get("id") == 66

    # IP must be absent
    assert result.get("ip") is None
    assert result.get("first_ip_id") is None
    assert not ip_calls

    # MAC must still have been reconciled
    assert len(mac_calls) == 1
    assert mac_calls[0]["mac"] == "AA:BB:CC:DD:EE:FF"


# ---------------------------------------------------------------------------
# (b) sync_vm_interface_macs=False
# ---------------------------------------------------------------------------


def test_create_vm_interface_parallel_skips_mac_when_sync_mac_false(monkeypatch):
    """When sync_mac=False, MAC reconciliation is skipped; interface and IP still created."""
    mac_calls, ip_calls = _install_interface_parallel_stubs(monkeypatch)

    result = asyncio.run(
        _create_vm_interface_parallel(
            nb=SimpleNamespace(),
            virtual_machine=_minimal_vm(),
            interface_name="eth0",
            interface_config=_interface_config_with_ip(),
            guest_iface=None,
            tag_refs=_tag_refs(),
            use_guest_agent_interface_name=False,
            ignore_ipv6_link_local_addresses=True,
            now=_NOW,
            create_ip=True,
            sync_mac=False,
        )
    )

    # Interface must be present
    assert result["interface"] is not None
    assert result["interface"].get("id") == 66

    # IP must be present
    assert result.get("first_ip_id") == 77

    # MAC must NOT have been reconciled
    assert not mac_calls


# ---------------------------------------------------------------------------
# (c) Both default True — unchanged behavior
# ---------------------------------------------------------------------------


def test_create_vm_interface_parallel_defaults_create_both(monkeypatch):
    """Default flags (create_ip=True, sync_mac=True) create interface, IP, and MAC."""
    mac_calls, ip_calls = _install_interface_parallel_stubs(monkeypatch)

    result = asyncio.run(
        _create_vm_interface_parallel(
            nb=SimpleNamespace(),
            virtual_machine=_minimal_vm(),
            interface_name="eth0",
            interface_config=_interface_config_with_ip(),
            guest_iface=None,
            tag_refs=_tag_refs(),
            use_guest_agent_interface_name=False,
            ignore_ipv6_link_local_addresses=True,
            now=_NOW,
            # Explicitly passing defaults
            create_ip=True,
            sync_mac=True,
        )
    )

    assert result["interface"] is not None
    assert result.get("first_ip_id") == 77
    assert len(mac_calls) == 1


# ---------------------------------------------------------------------------
# (d) create_virtual_machines_stream exposes both query params with default True
# ---------------------------------------------------------------------------


def test_create_virtual_machines_stream_has_assign_vm_interface_ips_param():
    """The stream endpoint must declare assign_vm_interface_ips with default True."""
    sig = inspect.signature(create_virtual_machines_stream)
    assert "assign_vm_interface_ips" in sig.parameters, (
        "assign_vm_interface_ips param missing from create_virtual_machines_stream"
    )
    param = sig.parameters["assign_vm_interface_ips"]
    # FastAPI Query() wraps the default — unwrap it
    default = param.default
    if hasattr(default, "default"):
        default = default.default
    assert default is True, f"Expected default True, got {default!r}"


def test_create_virtual_machines_stream_has_sync_vm_interface_macs_param():
    """The stream endpoint must declare sync_vm_interface_macs with default True."""
    sig = inspect.signature(create_virtual_machines_stream)
    assert "sync_vm_interface_macs" in sig.parameters, (
        "sync_vm_interface_macs param missing from create_virtual_machines_stream"
    )
    param = sig.parameters["sync_vm_interface_macs"]
    default = param.default
    if hasattr(default, "default"):
        default = default.default
    assert default is True, f"Expected default True, got {default!r}"


async def test_create_virtual_machines_stream_forwards_ip_mac_flags(monkeypatch):
    """Flags passed to stream endpoint are forwarded to create_virtual_machines."""
    captured: dict = {}

    async def _fake_create_virtual_machines(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(sync_vm, "create_virtual_machines", _fake_create_virtual_machines)

    response = await create_virtual_machines_stream(
        netbox_session=SimpleNamespace(),
        pxs=[],
        cluster_status=[],
        cluster_resources=[],
        custom_fields=[],
        tag=SimpleNamespace(id=7),
        sync_vm_network=True,
        overwrite_flags=SyncOverwriteFlags(),
        assign_vm_interface_ips=False,
        sync_vm_interface_macs=False,
    )

    # Consume the streaming response so the inner task runs
    async for _ in response.body_iterator:
        pass

    assert captured.get("assign_vm_interface_ips") is False
    assert captured.get("sync_vm_interface_macs") is False
