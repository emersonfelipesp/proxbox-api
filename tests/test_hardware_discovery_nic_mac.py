"""Physical-NIC MAC reflection through SSH hardware discovery.

``/nodes/{node}/network`` only exposes ``hwaddress`` for bridges and bonds, so
``sync_node_network()`` cannot give a physical NIC a MAC. The SSH
hardware-discovery path already parses one into ``NicFacts.mac_address``;
these tests pin that ``reflect_to_netbox()`` actually writes it, reusing the
same ``reconcile_mac_for_interface`` helper the node-network path uses.

``reflect_to_netbox`` imports its REST helpers at call time, so the fakes are
installed on the source modules rather than on ``hardware_discovery``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from proxbox_api.services import hardware_discovery


@dataclass
class FakeEthtool:
    duplex: str | None = "full"
    link_detected: bool | None = True


@dataclass
class FakeNic:
    name: str
    mac_address: str | None = None
    speed_gbps: float | None = 10.0
    ethtool: FakeEthtool | None = field(default_factory=FakeEthtool)


@dataclass
class FakeSystem:
    product_name: str | None = "PowerEdge R740"


@dataclass
class FakeChassis:
    serial_number: str | None = "SN12345"
    manufacturer: str | None = "Dell Inc."


@dataclass
class FakeFacts:
    nics: tuple[FakeNic, ...] = field(default_factory=tuple)
    system: FakeSystem = field(default_factory=FakeSystem)
    chassis: FakeChassis = field(default_factory=FakeChassis)


def _install(monkeypatch, *, mac_side_effect: Exception | None = None):
    """Patch the REST + MAC seams and return the recorded call lists."""
    patches: list[tuple[str, int, dict[str, Any]]] = []
    macs: list[dict[str, Any]] = []

    async def fake_patch(nb, path, record_id, payload):
        patches.append((path, record_id, payload))
        return {"id": record_id}

    async def fake_mac(nb, **kwargs):
        macs.append(kwargs)
        if mac_side_effect is not None:
            raise mac_side_effect
        return 7, "created"

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_patch_async", fake_patch)
    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.reconcile_mac_for_interface", fake_mac
    )
    return patches, macs


async def test_reflect_sets_primary_mac_for_physical_nic(monkeypatch):
    patches, macs = _install(monkeypatch)

    await hardware_discovery.reflect_to_netbox(
        object(),
        11,
        FakeFacts(nics=(FakeNic(name="eno1", mac_address="a0:42:3f:4c:61:aa"),)),
        interface_lookup={"eno1": 42},
    )

    assert len(macs) == 1
    assert macs[0]["mac"] == "a0:42:3f:4c:61:aa"
    assert macs[0]["assigned_object_type"] == "dcim.interface"
    assert macs[0]["assigned_object_id"] == 42
    # Must target dcim, not the virtualization interface path.
    assert macs[0]["interface_list_path"] == "/api/dcim/interfaces/"

    # The pre-existing speed/duplex/link reflection is untouched.
    iface_patch = [p for p in patches if p[0] == "/api/dcim/interfaces/"]
    assert iface_patch[0][1] == 42
    assert iface_patch[0][2]["custom_fields"]["nic_speed_gbps"] == 10.0


async def test_reflect_skips_mac_when_nic_has_none(monkeypatch):
    patches, macs = _install(monkeypatch)

    await hardware_discovery.reflect_to_netbox(
        object(),
        11,
        FakeFacts(nics=(FakeNic(name="eno1", mac_address=None),)),
        interface_lookup={"eno1": 42},
    )

    assert macs == []
    # The custom-field reflection still ran.
    assert any(p[0] == "/api/dcim/interfaces/" for p in patches)


async def test_reflect_mac_failure_does_not_abort_remaining_nics(monkeypatch):
    patches, macs = _install(monkeypatch, mac_side_effect=RuntimeError("netbox 500"))

    await hardware_discovery.reflect_to_netbox(
        object(),
        11,
        FakeFacts(
            nics=(
                FakeNic(name="eno1", mac_address="AA:BB:CC:DD:EE:01"),
                FakeNic(name="eno2", mac_address="AA:BB:CC:DD:EE:02"),
            )
        ),
        interface_lookup={"eno1": 42, "eno2": 43},
    )

    # Both NICs were attempted despite the first raising.
    assert [m["assigned_object_id"] for m in macs] == [42, 43]
    assert len([p for p in patches if p[0] == "/api/dcim/interfaces/"]) == 2


async def test_reflect_skips_nic_without_matching_netbox_interface(monkeypatch):
    patches, macs = _install(monkeypatch)

    await hardware_discovery.reflect_to_netbox(
        object(),
        11,
        FakeFacts(nics=(FakeNic(name="enp5s0", mac_address="AA:BB:CC:DD:EE:03"),)),
        interface_lookup={"eno1": 42},
    )

    assert macs == []
    assert [p[0] for p in patches] == ["/api/dcim/devices/"]


@pytest.mark.parametrize("lookup", [{}, None])
async def test_reflect_without_interfaces_writes_no_mac(monkeypatch, lookup):
    patches, macs = _install(monkeypatch)

    async def fake_list(nb, path, query=None):
        return []

    monkeypatch.setattr("proxbox_api.netbox_rest.rest_list_async", fake_list)

    await hardware_discovery.reflect_to_netbox(
        object(),
        11,
        FakeFacts(nics=(FakeNic(name="eno1", mac_address="AA:BB:CC:DD:EE:04"),)),
        interface_lookup=lookup,
    )

    assert macs == []
    # Chassis reflection still happens even with no interfaces to match.
    assert [p[0] for p in patches] == ["/api/dcim/devices/"]
