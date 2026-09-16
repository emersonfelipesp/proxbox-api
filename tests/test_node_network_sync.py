"""Tests for node-network -> dcim.Interface sync (sync_node_network).

Drives the orchestrator on Merkeb-shaped /nodes/{node}/network data with the
NetBox REST calls mocked, and asserts the two-phase behaviour: scalar fields +
type mapping + enabled in phase 1, and topology cross-references (bridge / lag /
vlan parent + tagged VLAN) in phase 2.
"""

import json
from types import SimpleNamespace

import pytest
from netbox_sdk.client import ApiResponse

from proxbox_api.netbox_rest import clear_rest_get_cache
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

OVS_NETWORK = [
    {"iface": "eno4", "type": "OVSPort", "active": 1, "ovs_bridge": "ovs-br0"},
    {"iface": "eno5", "type": "eth", "active": 1},
    {"iface": "ovs-bond0", "type": "OVSBond", "active": 1, "ovs_bonds": "eno5"},
    {
        "iface": "ovs-br0",
        "type": "OVSBridge",
        "active": 1,
        "ovs_ports": "ovs-bond0 ovs-int0",
    },
    {
        "iface": "ovs-int0",
        "type": "OVSIntPort",
        "active": 1,
        "ovs_bridge": "ovs-br0",
    },
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


def _calls_by_phase(iface_calls):
    phase1 = {}
    topology = {}
    for call in iface_calls:
        fields = call["patchable"]
        if fields is not None and "enabled" in fields:
            phase1[call["name"]] = call["payload"]
        elif fields:
            topology[call["name"]] = call["payload"]
    return phase1, topology


class _MigrationRaceClient:
    def __init__(
        self,
        *,
        duplicate_create: bool = False,
        initial_blocker: bool = False,
        patch_succeeds: bool = False,
        blocker: dict | None = None,
    ) -> None:
        self.duplicate_create = duplicate_create
        self.initial_blocker = initial_blocker
        self.patch_succeeds = patch_succeeds
        self.blocker = blocker or {"cable": {"id": 91}}
        self.get_count = 0
        self.patch_payloads = []
        self.post_count = 0

    async def request(self, method, path, *, query=None, payload=None, expect_json=True):
        del query, expect_json
        if method == "GET":
            self.get_count += 1
            if self.duplicate_create and self.get_count == 1:
                body = {"count": 0, "next": None, "previous": None, "results": []}
            else:
                row = {
                    "id": 81,
                    "device": 5,
                    "name": "ovs0",
                    "status": "active",
                    "enabled": True,
                    "type": {"value": "other"},
                    "tags": [],
                }
                if self.initial_blocker or self.get_count > 1 or self.duplicate_create:
                    row.update(self.blocker)
                body = {"count": 1, "next": None, "previous": None, "results": [row]}
            return ApiResponse(status=200, text=json.dumps(body))
        if method == "POST":
            self.post_count += 1
            body = {"non_field_errors": ["Interface with this Device and Name already exists."]}
            return ApiResponse(status=400, text=json.dumps(body))
        if method == "PATCH":
            self.patch_payloads.append(dict(payload))
            if self.patch_succeeds:
                body = {
                    "id": 81,
                    "device": 5,
                    "name": "ovs0",
                    "status": "active",
                    "enabled": True,
                    "type": payload["type"],
                    "tags": [],
                }
                return ApiResponse(status=200, text=json.dumps(body))
            body = {"type": ["Virtual interfaces cannot have a cable."]}
            return ApiResponse(status=400, text=json.dumps(body))
        raise AssertionError(f"unexpected request: {method} {path}")


@pytest.fixture(autouse=True)
def _reset_rest_cache():
    clear_rest_get_cache()
    yield
    clear_rest_get_cache()


@pytest.mark.parametrize("duplicate_create", [False, True])
async def test_real_reconcile_preserves_type_after_concurrent_blocker(
    monkeypatch, duplicate_create
):
    client = _MigrationRaceClient(duplicate_create=duplicate_create)
    session = SimpleNamespace(client=client)
    warnings = []
    monkeypatch.setattr(network.logger, "warning", lambda *args: warnings.append(args))

    record = await network._reconcile_node_interface_scalar(
        session,
        device={"id": 5, "name": "pve-a"},
        entry={"iface": "ovs0", "type": "OVSBridge", "active": 1},
        tag_refs=[],
    )

    assert record.get("type") == {"value": "other"}
    assert any("concurrent migration conflict" in call[0] for call in warnings)
    assert client.patch_payloads == [{"type": "bridge"}]
    if duplicate_create:
        assert client.post_count == 1


async def test_real_reconcile_preserves_initially_blocked_row_without_patch(monkeypatch):
    client = _MigrationRaceClient(initial_blocker=True)
    session = SimpleNamespace(client=client)
    monkeypatch.setattr(network.logger, "warning", lambda *_args: None)

    record = await network._reconcile_node_interface_scalar(
        session,
        device={"id": 5, "name": "pve-a"},
        entry={"iface": "ovs0", "type": "OVSBridge", "active": 1},
        tag_refs=[],
    )

    assert record.get("type") == {"value": "other"}
    assert client.patch_payloads == []


async def test_real_reconcile_retypes_compatible_row(monkeypatch):
    client = _MigrationRaceClient(patch_succeeds=True)
    session = SimpleNamespace(client=client)
    monkeypatch.setattr(network.logger, "warning", lambda *_args: None)

    record = await network._reconcile_node_interface_scalar(
        session,
        device={"id": 5, "name": "pve-a"},
        entry={"iface": "ovs0", "type": "OVSBridge", "active": 1},
        tag_refs=[],
    )

    assert record.get("type") == "bridge"
    assert client.patch_payloads == [{"type": "bridge"}]


@pytest.mark.parametrize(
    ("duplicate_create", "blocker"),
    [
        (False, {"cable": {"id": 91}}),
        (False, {"mark_connected": True}),
        (True, {"cable": {"id": 91}}),
    ],
)
async def test_legacy_interface_path_preserves_concurrent_blocker(
    monkeypatch, duplicate_create, blocker
):
    client = _MigrationRaceClient(duplicate_create=duplicate_create, blocker=blocker)
    session = SimpleNamespace(client=client)
    monkeypatch.setattr(network.logger, "warning", lambda *_args: None)

    result = await network.sync_node_interface_and_ip(
        session,
        device={"id": 5, "name": "pve-a"},
        interface_name="ovs0",
        interface_config={"type": "OVSBridge"},
        tag_refs=[],
    )

    assert result == {"id": 81, "name": "ovs0"}
    assert client.patch_payloads == [{"type": "bridge"}]


async def test_migration_rethrows_unrelated_patch_failure(monkeypatch):
    client = _MigrationRaceClient()
    session = SimpleNamespace(client=client)

    async def always_compatible(_nb, _path, *, query):
        del query
        return network.RestRecord(
            session,
            "/api/dcim/interfaces/",
            {
                "id": 81,
                "device": 5,
                "name": "ovs0",
                "status": "active",
                "enabled": True,
                "type": {"value": "other"},
                "tags": [],
            },
        )

    monkeypatch.setattr(network, "rest_first_async", always_compatible)

    with pytest.raises(network.ProxboxException):
        await network._reconcile_node_interface_scalar(
            session,
            device={"id": 5, "name": "pve-a"},
            entry={"iface": "ovs0", "type": "OVSBridge", "active": 1},
            tag_refs=[],
        )


class _InvisibleDuplicateClient:
    async def request(self, method, path, *, query=None, payload=None, expect_json=True):
        del path, query, payload, expect_json
        if method == "GET":
            body = {"count": 0, "next": None, "previous": None, "results": []}
            return ApiResponse(status=200, text=json.dumps(body))
        if method == "POST":
            body = {"non_field_errors": ["Interface with this Device and Name already exists."]}
            return ApiResponse(status=400, text=json.dumps(body))
        raise AssertionError(f"unexpected method: {method}")


async def test_node_network_fails_closed_when_duplicate_never_becomes_visible(monkeypatch):
    session = SimpleNamespace(client=_InvisibleDuplicateClient())

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("proxbox_api.netbox_rest.asyncio.sleep", no_sleep)

    with pytest.raises(network.ProxboxException, match="no persisted record"):
        await network.sync_node_network(
            session,
            device={"id": 5, "name": "pve-a"},
            network_entries=[{"iface": "ovs0", "type": "OVSBridge", "active": 1}],
            tag_refs=[],
        )


@pytest.mark.parametrize(
    ("proxmox_type", "current", "blocker", "expected"),
    [
        ("OVSBridge", "other", {"cable": {"id": 7}}, "other"),
        ("OVSBond", "other", {"mark_connected": True}, "other"),
        ("OVSIntPort", "other", {}, "virtual"),
    ],
)
async def test_safe_ovs_retype_preserves_only_incompatible_legacy_rows(
    monkeypatch,
    proxmox_type,
    current,
    blocker,
    expected,
):
    existing = {"id": 41, "type": {"value": current}, **blocker}
    warnings = []

    async def fake_first(_nb, path, *, query):
        assert path == "/api/dcim/interfaces/"
        assert query == {"device_id": 5, "name": "ovs0", "limit": 2}
        return existing

    monkeypatch.setattr(network, "rest_first_async", fake_first)
    monkeypatch.setattr(network, "clear_rest_get_cache_for_path", lambda *_args: None)
    monkeypatch.setattr(network.logger, "warning", lambda *args: warnings.append(args))

    resolved, record, lookup_performed = await network._safe_node_interface_type(
        object(),
        device_id=5,
        device_name="pve-a",
        interface_name="ovs0",
        proxmox_type=proxmox_type,
    )

    assert resolved == expected
    assert record is existing
    assert lookup_performed is True
    if blocker:
        assert len(warnings) == 1
        assert warnings[0][2:4] == ("pve-a", "ovs0")
        assert next(iter(blocker)) in warnings[0][-1]
    else:
        assert warnings == []


async def test_safe_ovs_retype_creates_new_rows_with_specific_type(monkeypatch):
    async def fake_first(_nb, _path, *, query):
        assert query == {"device_id": 5, "name": "ovs0", "limit": 2}
        return None

    monkeypatch.setattr(network, "rest_first_async", fake_first)
    monkeypatch.setattr(network, "clear_rest_get_cache_for_path", lambda *_args: None)

    resolved, record, lookup_performed = await network._safe_node_interface_type(
        object(),
        device_id=5,
        device_name="pve-a",
        interface_name="ovs0",
        proxmox_type="OVSBridge",
    )

    assert resolved == "bridge"
    assert record is None
    assert lookup_performed is True


@pytest.mark.parametrize(
    ("proxmox_type", "desired"),
    [("OVSBridge", "bridge"), ("OVSBond", "lag"), ("OVSIntPort", "virtual")],
)
@pytest.mark.parametrize("blocker", [{"cable": {"id": 9}}, {"mark_connected": True}])
async def test_node_network_preserves_each_incompatible_ovs_legacy_row(
    monkeypatch,
    proxmox_type,
    desired,
    blocker,
):
    existing = {"id": 71, "type": {"value": "other"}, **blocker}
    reconcile_calls = []

    async def fake_first(_nb, _path, *, query):
        assert query == {"device_id": 5, "name": "ovs0", "limit": 2}
        return existing

    async def fake_reconcile(_nb, path, *, payload, existing_record=None, **_kwargs):
        assert path == "/api/dcim/interfaces/"
        reconcile_calls.append((payload, existing_record))
        return SimpleNamespace(id=71)

    monkeypatch.setattr(network, "rest_first_async", fake_first)
    monkeypatch.setattr(network, "clear_rest_get_cache_for_path", lambda *_args: None)
    monkeypatch.setattr(network, "rest_reconcile_async", fake_reconcile)

    result = await network.sync_node_network(
        nb=object(),
        device={"id": 5, "name": "pve-a"},
        network_entries=[{"iface": "ovs0", "type": proxmox_type, "active": 1}],
        tag_refs=[],
    )

    assert result == [{"id": 71, "name": "ovs0"}]
    assert reconcile_calls[0][0]["type"] == "other"
    assert reconcile_calls[0][1] is existing
    assert desired != "other"


@pytest.mark.parametrize(
    ("proxmox_type", "expected"),
    [("OVSBridge", "bridge"), ("OVSBond", "lag"), ("OVSIntPort", "virtual")],
)
async def test_node_network_retypes_compatible_ovs_legacy_rows(
    monkeypatch,
    proxmox_type,
    expected,
):
    existing = {"id": 72, "type": {"value": "other"}, "mark_connected": False}
    payloads = []

    async def fake_first(_nb, _path, *, query):
        assert query == {"device_id": 5, "name": "ovs0", "limit": 2}
        return existing

    async def fake_reconcile(_nb, path, *, payload, existing_record=None, **_kwargs):
        assert path == "/api/dcim/interfaces/"
        if "enabled" in payload:
            assert existing_record is existing
        payloads.append(payload)
        return SimpleNamespace(id=72)

    monkeypatch.setattr(network, "rest_first_async", fake_first)
    monkeypatch.setattr(network, "clear_rest_get_cache_for_path", lambda *_args: None)
    monkeypatch.setattr(network, "rest_reconcile_async", fake_reconcile)

    await network.sync_node_network(
        nb=object(),
        device={"id": 5, "name": "pve-a"},
        network_entries=[{"iface": "ovs0", "type": proxmox_type, "active": 1}],
        tag_refs=[],
    )

    assert payloads[0]["type"] == expected


async def test_sync_node_network_maps_types_enabled_and_topology(monkeypatch):
    iface_ids, iface_calls, ip_calls, mac_calls = _install_mocks(monkeypatch)

    await network.sync_node_network(
        nb=object(), device={"id": 1}, network_entries=NETWORK, tag_refs=[]
    )

    # `lo` is skipped entirely.
    assert "lo" not in iface_ids

    # Phase-1 vs phase-2 calls carry disjoint patchable_fields whitelists:
    # phase 1 owns the scalar fields (incl. `enabled`); phase 2 owns the
    # topology cross-references. Detect phase 1 by its `enabled` whitelist entry.
    def _is_phase1(call):
        return call["patchable"] is not None and "enabled" in call["patchable"]

    phase1 = {}
    phase1_patchable = {}
    for c in iface_calls:
        if _is_phase1(c) and c["name"] not in phase1:
            phase1[c["name"]] = c["payload"]
            phase1_patchable[c["name"]] = c["patchable"]

    # Type mapping: eth -> other, bridge -> bridge, vlan -> virtual.
    assert phase1["eno1"]["type"] == "other"
    assert phase1["vmbr0"]["type"] == "bridge"
    assert phase1["vmbr1.200"]["type"] == "virtual"

    # enabled reflects Proxmox `active`.
    assert phase1["eno1"]["enabled"] is True
    assert phase1["eno3"]["enabled"] is False

    # Phase 1 must NEVER be allowed to touch topology / VLAN membership, or a
    # re-sync would clear the VLANs that phase 2 assigns (the desired payload
    # carries tagged_vlans=[] via the schema default). Its whitelist is scalar-only.
    for name, patchable in phase1_patchable.items():
        assert {"bridge", "lag", "parent", "mode", "tagged_vlans"}.isdisjoint(patchable), name

    # Phase-2 topology patches (scalar `enabled` field absent from the whitelist).
    patches = {c["name"]: c["payload"] for c in iface_calls if c["patchable"] and not _is_phase1(c)}

    # Bridge membership: eno1 -> vmbr0, eno2 -> vmbr1.
    assert patches["eno1"]["bridge"] == iface_ids["vmbr0"]
    assert patches["eno2"]["bridge"] == iface_ids["vmbr1"]

    # VLAN sub-interface: parent = raw device, tagged with its VLAN object.
    assert patches["vmbr1.200"]["parent"] == iface_ids["vmbr1"]
    assert patches["vmbr1.200"]["mode"] == "tagged"
    assert patches["vmbr1.200"]["tagged_vlans"] == [900 + 200]

    # IPs: vmbr0 gets its v4; the v6 (2001:...::/64) is a network ID and is
    # skipped (NetBox won't assign it). VLAN sub-interface gets its address.
    by_iface = {}
    for c in ip_calls:
        by_iface.setdefault(c["interface_id"], []).append(c["ip"])
    assert by_iface[iface_ids["vmbr0"]] == ["141.94.139.106/24"]
    assert by_iface[iface_ids["vmbr1.200"]] == ["10.16.200.3/24"]

    # MAC: only the bridge with an `hwaddress` option gets one, normalized to
    # NetBox canonical form and assigned to the dcim.interface.
    assert len(mac_calls) == 1
    assert mac_calls[0] == {
        "mac": "A0:42:3F:4C:61:AA",
        "type": "dcim.interface",
        "interface_id": iface_ids["vmbr0"],
    }


async def test_sync_node_network_maps_ovs_types_and_topology(monkeypatch):
    iface_ids, iface_calls, _ip_calls, _mac_calls = _install_mocks(monkeypatch)

    async def fake_first(_nb, _path, *, query):
        return None

    monkeypatch.setattr(network, "rest_first_async", fake_first)
    monkeypatch.setattr(network, "clear_rest_get_cache_for_path", lambda *_args: None)

    await network.sync_node_network(
        nb=object(), device={"id": 1, "name": "pve-a"}, network_entries=OVS_NETWORK, tag_refs=[]
    )

    phase1, patches = _calls_by_phase(iface_calls)
    assert phase1["ovs-br0"]["type"] == "bridge"
    assert phase1["ovs-bond0"]["type"] == "lag"
    assert phase1["ovs-int0"]["type"] == "virtual"

    assert phase1["eno4"]["type"] == "other"
    assert patches["eno4"]["bridge"] == iface_ids["ovs-br0"]
    assert patches["eno5"]["lag"] == iface_ids["ovs-bond0"]
    assert patches["ovs-bond0"]["bridge"] == iface_ids["ovs-br0"]
    assert patches["ovs-int0"]["bridge"] == iface_ids["ovs-br0"]


async def test_sync_node_network_clears_removed_ovs_topology(monkeypatch):
    _iface_ids, iface_calls, _ip_calls, _mac_calls = _install_mocks(monkeypatch)

    async def fake_first(_nb, _path, *, query):
        return None

    monkeypatch.setattr(network, "rest_first_async", fake_first)
    monkeypatch.setattr(network, "clear_rest_get_cache_for_path", lambda *_args: None)

    await network.sync_node_network(
        nb=object(), device={"id": 1, "name": "pve-a"}, network_entries=OVS_NETWORK, tag_refs=[]
    )
    first_count = len(iface_calls)
    detached = [
        {
            key: value
            for key, value in entry.items()
            if key not in {"ovs_ports", "ovs_bonds", "ovs_bridge"}
        }
        for entry in OVS_NETWORK
    ]
    await network.sync_node_network(
        nb=object(), device={"id": 1, "name": "pve-a"}, network_entries=detached, tag_refs=[]
    )

    _phase1, topology = _calls_by_phase(iface_calls[first_count:])
    for iface in ("eno4", "eno5", "ovs-bond0", "ovs-br0", "ovs-int0"):
        assert topology[iface]["bridge"] is None
        assert topology[iface]["lag"] is None
        assert topology[iface]["parent"] is None
        assert topology[iface]["mode"] is None
        assert topology[iface]["tagged_vlans"] == []


def test_is_network_id():
    # Subnet network addresses (host bits zero) are network IDs.
    assert network._is_network_id("2001:41d0:403:4a6a::/64") is True
    assert network._is_network_id("10.0.0.0/24") is True
    # Real host addresses are not.
    assert network._is_network_id("141.94.139.106/24") is False
    assert network._is_network_id("10.16.200.3/24") is False
    # Host-style prefixes are assignable even at the "network" address.
    assert network._is_network_id("10.0.0.0/32") is False
    assert network._is_network_id("10.0.0.0/31") is False
    assert network._is_network_id("2001:db8::/128") is False
    assert network._is_network_id("2001:db8::/127") is False


async def test_sync_node_network_skips_when_no_entries(monkeypatch):
    _install_mocks(monkeypatch)
    result = await network.sync_node_network(
        nb=object(), device={"id": 1}, network_entries=[], tag_refs=[]
    )
    assert result == []
