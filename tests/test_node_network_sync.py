"""Tests for node-network -> dcim.Interface sync (sync_node_network).

Drives the orchestrator on Merkeb-shaped /nodes/{node}/network data with the
NetBox REST calls mocked, and asserts the two-phase behaviour: scalar fields +
type mapping + enabled in phase 1, and topology cross-references (bridge / lag /
vlan parent + tagged VLAN) in phase 2.
"""

from types import SimpleNamespace

from proxbox_api.services.sync import network

# Subset of a real `pvesh get /nodes/<node>/network` payload.
NETWORK = [
    {"iface": "eno1", "type": "eth", "active": 1},
    {"iface": "eno2", "type": "eth", "active": 1},
    {"iface": "eno3", "type": "eth"},  # inactive
    {
        "iface": "vmbr0",
        "type": "bridge",
        "active": 1,
        "bridge_ports": "eno1",
        "cidr": "141.94.139.106/24",
        "gateway": "141.94.139.254",
        "cidr6": "2001:41d0:403:4a6a::/64",
        "options": ["hwaddress a0:42:3f:4c:61:aa"],
    },
    {"iface": "vmbr1", "type": "bridge", "active": 1, "bridge_ports": "eno2"},
    {
        "iface": "vmbr1.200",
        "type": "vlan",
        "active": 1,
        "cidr": "10.16.200.3/24",
        "vlan-id": "200",
        "vlan-raw-device": "vmbr1",
    },
    {"iface": "lo", "type": "loopback"},  # must be skipped
]


def _install_mocks(monkeypatch):
    iface_ids: dict[str, int] = {}
    next_id = [10]
    iface_calls: list[dict] = []
    ip_calls: list[dict] = []

    async def fake_reconcile(nb, path, *, lookup, payload, schema, current_normalizer, **kw):
        if path == "/api/dcim/interfaces/":
            name = lookup["name"]
            iface_ids.setdefault(name, next_id[0])
            if iface_ids[name] == next_id[0]:
                next_id[0] += 1
            iface_calls.append(
                {"name": name, "payload": payload, "patchable": kw.get("patchable_fields")}
            )
            return SimpleNamespace(id=iface_ids[name])
        if path == "/api/ipam/vlans/":
            return SimpleNamespace(id=900 + int(lookup["vid"]))
        raise AssertionError(f"unexpected path {path}")

    async def fake_ip(nb, *, ip_addr, interface_id, **kw):
        ip_calls.append({"ip": ip_addr, "interface_id": interface_id})
        return 1

    mac_calls: list[dict] = []

    async def fake_mac(nb, *, mac, assigned_object_type, assigned_object_id, **kw):
        mac_calls.append(
            {"mac": mac, "type": assigned_object_type, "interface_id": assigned_object_id}
        )
        return 1, "created"

    monkeypatch.setattr(network, "rest_reconcile_async", fake_reconcile)
    monkeypatch.setattr(network, "_reconcile_interface_ip", fake_ip)
    # sync_node_network imports these from mac_address at call time.
    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.reconcile_mac_for_interface", fake_mac
    )
    return iface_ids, iface_calls, ip_calls, mac_calls


async def test_sync_node_network_maps_types_enabled_and_topology(monkeypatch):
    iface_ids, iface_calls, ip_calls, mac_calls = _install_mocks(monkeypatch)

    await network.sync_node_network(
        nb=object(), device={"id": 1}, network_entries=NETWORK, tag_refs=[]
    )

    # `lo` is skipped entirely.
    assert "lo" not in iface_ids

    # Phase-1 create payloads (first call per interface).
    phase1 = {}
    for c in iface_calls:
        if c["patchable"] is None and c["name"] not in phase1:
            phase1[c["name"]] = c["payload"]

    # Type mapping: eth -> other, bridge -> bridge, vlan -> virtual.
    assert phase1["eno1"]["type"] == "other"
    assert phase1["vmbr0"]["type"] == "bridge"
    assert phase1["vmbr1.200"]["type"] == "virtual"

    # enabled reflects Proxmox `active`.
    assert phase1["eno1"]["enabled"] is True
    assert phase1["eno3"]["enabled"] is False

    # Phase-2 topology patches (patchable_fields set).
    patches = {c["name"]: c["payload"] for c in iface_calls if c["patchable"] is not None}

    # Bridge membership: eno1 -> vmbr0, eno2 -> vmbr1.
    assert patches["eno1"]["bridge"] == iface_ids["vmbr0"]
    assert patches["eno2"]["bridge"] == iface_ids["vmbr1"]

    # VLAN sub-interface: parent = raw device, tagged with its VLAN object.
    assert patches["vmbr1.200"]["parent"] == iface_ids["vmbr1"]
    assert patches["vmbr1.200"]["mode"] == "tagged"
    assert patches["vmbr1.200"]["tagged_vlans"] == [900 + 200]

    # IPs: both families on vmbr0, plus the VLAN sub-interface address.
    by_iface = {}
    for c in ip_calls:
        by_iface.setdefault(c["interface_id"], []).append(c["ip"])
    assert sorted(by_iface[iface_ids["vmbr0"]]) == ["141.94.139.106/24", "2001:41d0:403:4a6a::/64"]
    assert by_iface[iface_ids["vmbr1.200"]] == ["10.16.200.3/24"]

    # MAC: only the bridge with an `hwaddress` option gets one, normalized to
    # NetBox canonical form and assigned to the dcim.interface.
    assert len(mac_calls) == 1
    assert mac_calls[0] == {
        "mac": "A0:42:3F:4C:61:AA",
        "type": "dcim.interface",
        "interface_id": iface_ids["vmbr0"],
    }


async def test_sync_node_network_skips_when_no_entries(monkeypatch):
    _install_mocks(monkeypatch)
    result = await network.sync_node_network(
        nb=object(), device={"id": 1}, network_entries=[], tag_refs=[]
    )
    assert result == []
