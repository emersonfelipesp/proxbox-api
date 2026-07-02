from __future__ import annotations

import pytest
from proxmox_sdk.sdk.exceptions import ResourceException

from proxbox_api.services.sync.sdn import (
    SdnInventory,
    SdnNodeStatusSchema,
    SdnSubnetSchema,
    SdnSyncCounters,
    SdnVNetSchema,
    SdnZoneSchema,
    _netbox_status,
    _sdn_bgp_projection_enabled,
    _split_csv,
    _sync_l2vpn_terminations,
    _sync_netbox_bgp_projection,
    _sync_netbox_l2vpn_objects,
    _to_vnet,
    _to_zone,
    _valid_community_values,
    _valid_prefix,
    collect_sdn_inventory_for_session,
    sync_sdn_to_netbox,
)


class _FakePx:
    def __init__(self, payloads: dict[str, object]):
        self.name = "pve-a"
        self.cluster_name = "cluster-a"
        self.cluster_status = [{"type": "node", "name": "node-a"}]
        self._payloads = payloads

    def session(self, path: str):
        payload = self._payloads.get(path, [])

        class _Resource:
            async def get(self) -> object:
                if isinstance(payload, Exception):
                    raise payload
                return payload

        return _Resource()


def test_sdn_zone_normalizer_reads_hyphenated_aliases():
    zone = _to_zone(
        "cluster-a",
        {
            "zone": "evpn-prod",
            "type": "evpn",
            "rt-import": "65000:100,65000:200",
            "vrf-vxlan": "9000",
            "pending": {"tag": 100},
            "state": "new",
        },
        endpoint_id=17,
    )

    assert zone.endpoint_id == 17
    assert zone.zone == "evpn-prod"
    assert zone.type == "evpn"
    assert zone.rt_import == "65000:100,65000:200"
    assert zone.vrf_vxlan == 9000
    assert _netbox_status(zone.state, zone.pending) == "planned"


def test_sdn_netbox_status_mapping():
    assert _netbox_status("deleted") == "decommissioning"
    assert _netbox_status("available") == "active"
    assert _netbox_status("") == "active"
    assert _netbox_status(None) == "active"
    assert _netbox_status("new") == "planned"


def test_sdn_vnet_normalizer_reads_tag_and_vlanaware():
    vnet = _to_vnet(
        "cluster-a",
        {
            "vnet": "tenant100",
            "zone": "evpn-prod",
            "type": "vnet",
            "tag": "100100",
            "vlanaware": "1",
        },
    )

    assert vnet.vnet == "tenant100"
    assert vnet.zone == "evpn-prod"
    assert vnet.tag == 100100
    assert vnet.vlanaware is True


def test_sdn_prefix_and_rt_helpers():
    assert _valid_prefix("10.0.0.1/24") == "10.0.0.0/24"
    assert _valid_prefix("not-a-prefix") is None
    assert _split_csv("65000:1, 65000:2") == ["65000:1", "65000:2"]


@pytest.mark.asyncio
async def test_collect_sdn_inventory_includes_cluster_and_node_runtime_rows():
    px = _FakePx(
        {
            "cluster/sdn/controllers": [{"controller": "bgp1", "type": "evpn", "asn": 65000}],
            "cluster/sdn/zones": [{"zone": "evpn-prod", "type": "evpn"}],
            "cluster/sdn/vnets": [{"vnet": "tenant100", "zone": "evpn-prod", "tag": 100100}],
            "cluster/sdn/vnets/tenant100/subnets": [{"subnet": "10.100.0.0/24"}],
            "nodes/node-a/sdn/zones": [{"zone": "evpn-prod", "status": "available"}],
            "nodes/node-a/sdn/zones/evpn-prod/content": [
                {"vnet": "tenant100", "status": "available"}
            ],
            "nodes/node-a/sdn/zones/evpn-prod/bridges": [{"name": "tenant100"}],
            "nodes/node-a/sdn/zones/evpn-prod/ip-vrf": [{"ip": "10.100.0.0/24"}],
            "nodes/node-a/sdn/vnets/tenant100/mac-vrf": [{"mac": "00:11:22:33:44:55"}],
        }
    )

    inventory = await collect_sdn_inventory_for_session(px, endpoint_id=17)

    assert inventory.endpoint_id == 17
    assert len(inventory.controllers) == 1
    assert len(inventory.zones) == 1
    assert len(inventory.vnets) == 1
    assert len(inventory.subnets) == 1
    assert {row.kind for row in inventory.node_status} >= {
        "node-zone",
        "node-zone-content",
        "node-zone-bridge",
        "node-ip-vrf",
        "node-mac-vrf",
    }


@pytest.mark.parametrize(
    "unsupported",
    [
        pytest.param(
            ResourceException(
                status_code=501,
                status_message="Not Implemented",
                content="Method not implemented",
            ),
            id="resource-501",
        ),
        pytest.param(
            ResourceException(
                status_code=404,
                status_message="Not Found",
                content="No such API path",
            ),
            id="resource-404",
        ),
        pytest.param(RuntimeError("no such api path"), id="message-no-such-api-path"),
        pytest.param(RuntimeError("endpoint not implemented"), id="message-not-implemented"),
    ],
)
@pytest.mark.asyncio
async def test_collect_sdn_inventory_treats_unsupported_sdn_as_skipped(unsupported):
    px = _FakePx({"cluster/sdn/controllers": unsupported})

    inventory = await collect_sdn_inventory_for_session(px, include_node_runtime=False)

    assert inventory.controllers == []
    assert inventory.skipped_warnings
    assert inventory.errors == []


@pytest.mark.asyncio
async def test_sdn_l2vpn_mapping_writes_route_targets_l2vpn_and_prefix(monkeypatch):
    calls = []

    async def _fake_reconcile(_nb, path, *, lookup, payload, fields, **_kwargs):
        del fields
        calls.append({"path": path, "lookup": lookup, "payload": payload})
        ids = {
            "/api/ipam/route-targets/": len(calls),
            "/api/vpn/l2vpns/": 42,
            "/api/ipam/prefixes/": 84,
        }
        return ids[path], True

    import proxbox_api.services.sync.sdn as sdn_module

    monkeypatch.setattr(sdn_module, "_reconcile", _fake_reconcile)

    inventory = SdnInventory(
        endpoint_id=17,
        endpoint_name="pve-a",
        cluster_name="cluster-a",
        zones=[
            SdnZoneSchema(
                endpoint_id=17,
                cluster_name="cluster-a",
                zone="evpn-prod",
                type="evpn",
                rt_import="65000:100,65000:200",
            )
        ],
        vnets=[
            SdnVNetSchema(
                endpoint_id=17,
                cluster_name="cluster-a",
                zone="evpn-prod",
                vnet="tenant100",
                tag=100100,
            )
        ],
        subnets=[
            SdnSubnetSchema(
                endpoint_id=17,
                cluster_name="cluster-a",
                zone="evpn-prod",
                vnet="tenant100",
                subnet="10.100.0.1/24",
            )
        ],
    )
    counters = SdnSyncCounters()

    vnet_l2vpn_ids, subnet_prefix_ids = await _sync_netbox_l2vpn_objects(
        object(),
        inventory,
        counters,
    )

    assert vnet_l2vpn_ids == {("evpn-prod", "tenant100"): 42}
    assert subnet_prefix_ids == {("tenant100", "10.100.0.1/24"): 84}
    assert counters.route_targets == 2
    assert counters.l2vpns == 1
    assert counters.subnets == 1
    l2vpn_call = next(call for call in calls if call["path"] == "/api/vpn/l2vpns/")
    assert l2vpn_call["payload"]["name"] == "Proxbox pve-a / cluster-a / evpn-prod / tenant100"
    assert l2vpn_call["payload"]["slug"] == "proxbox-17-cluster-a-evpn-prod-tenant100"
    assert l2vpn_call["payload"]["type"] == "vxlan-evpn"
    assert l2vpn_call["payload"]["identifier"] == 100100
    assert l2vpn_call["payload"]["import_targets"] == [1, 2]
    assert l2vpn_call["payload"]["export_targets"] == []
    prefix_call = next(call for call in calls if call["path"] == "/api/ipam/prefixes/")
    assert prefix_call["payload"]["prefix"] == "10.100.0.0/24"


@pytest.mark.asyncio
async def test_sdn_l2vpn_mapping_supports_vxlan_and_skips_unmanaged_zones(monkeypatch):
    calls = []

    async def _fake_reconcile(_nb, path, *, lookup, payload, fields, **_kwargs):
        del lookup, fields
        calls.append({"path": path, "payload": payload})
        return 42, True

    import proxbox_api.services.sync.sdn as sdn_module

    monkeypatch.setattr(sdn_module, "_reconcile", _fake_reconcile)

    inventory = SdnInventory(
        endpoint_id=17,
        endpoint_name="pve-a",
        cluster_name="cluster-a",
        zones=[
            SdnZoneSchema(
                endpoint_id=17,
                cluster_name="cluster-a",
                zone="vxlan-prod",
                type="vxlan",
            ),
            SdnZoneSchema(
                endpoint_id=17,
                cluster_name="cluster-a",
                zone="simple-prod",
                type="simple",
            ),
        ],
        vnets=[
            SdnVNetSchema(
                endpoint_id=17,
                cluster_name="cluster-a",
                zone="vxlan-prod",
                vnet="tenant200",
                tag=200200,
            ),
            SdnVNetSchema(
                endpoint_id=17,
                cluster_name="cluster-a",
                zone="simple-prod",
                vnet="tenant300",
                tag=300300,
            ),
        ],
    )
    counters = SdnSyncCounters()

    vnet_l2vpn_ids, subnet_prefix_ids = await _sync_netbox_l2vpn_objects(
        object(),
        inventory,
        counters,
    )

    assert vnet_l2vpn_ids == {("vxlan-prod", "tenant200"): 42}
    assert subnet_prefix_ids == {}
    assert counters.l2vpns == 1
    assert counters.skipped == 1
    l2vpn_calls = [call for call in calls if call["path"] == "/api/vpn/l2vpns/"]
    assert len(l2vpn_calls) == 1
    assert l2vpn_calls[0]["payload"]["type"] == "vxlan"
    assert "tenant300" not in l2vpn_calls[0]["payload"]["name"]


@pytest.mark.asyncio
async def test_sdn_l2vpn_reconcile_error_records_object_error(monkeypatch):
    async def _fake_reconcile(_nb, path, *, lookup, payload, fields, **_kwargs):
        del lookup, payload, fields
        if path == "/api/vpn/l2vpns/":
            raise RuntimeError("l2vpn write failed")
        return 1, True

    import proxbox_api.services.sync.sdn as sdn_module

    monkeypatch.setattr(sdn_module, "_reconcile", _fake_reconcile)

    inventory = SdnInventory(
        endpoint_id=17,
        endpoint_name="pve-a",
        cluster_name="cluster-a",
        zones=[
            SdnZoneSchema(
                endpoint_id=17,
                cluster_name="cluster-a",
                zone="evpn-prod",
                type="evpn",
            )
        ],
        vnets=[
            SdnVNetSchema(
                endpoint_id=17,
                cluster_name="cluster-a",
                zone="evpn-prod",
                vnet="tenant100",
            )
        ],
    )
    counters = SdnSyncCounters()

    vnet_l2vpn_ids, subnet_prefix_ids = await _sync_netbox_l2vpn_objects(
        object(),
        inventory,
        counters,
    )

    assert vnet_l2vpn_ids == {}
    assert subnet_prefix_ids == {}
    assert counters.per_endpoint_errors == {"cluster-a": 1}
    assert counters.object_errors == [
        {
            "kind": "l2vpn",
            "name": "evpn-prod/tenant100",
            "error": "l2vpn write failed",
        }
    ]
    assert counters.as_dict()["object_errors"] == counters.object_errors


@pytest.mark.asyncio
async def test_sdn_l2vpn_termination_uses_explicit_target(monkeypatch):
    calls = []

    async def _fake_rest_first(_nb, _path, *, query):
        del query
        return None

    async def _fake_reconcile(_nb, path, *, lookup, payload, fields, **_kwargs):
        del lookup, fields
        calls.append({"path": path, "payload": payload})
        return 99 if path == "/api/vpn/l2vpn-terminations/" else 199, True

    import proxbox_api.services.sync.sdn as sdn_module

    monkeypatch.setattr(sdn_module, "rest_first_async", _fake_rest_first)
    monkeypatch.setattr(sdn_module, "_reconcile", _fake_reconcile)

    inventory = SdnInventory(
        endpoint_id=17,
        endpoint_name="pve-a",
        cluster_name="cluster-a",
        node_status=[
            SdnNodeStatusSchema(
                endpoint_id=17,
                cluster_name="cluster-a",
                node="node-a",
                zone="evpn-prod",
                vnet="tenant100",
                kind="node-zone-content",
                name="net0",
                raw_config={
                    "assigned_object_type": "ipam.vlan",
                    "assigned_object_id": 7,
                },
            )
        ],
    )
    counters = SdnSyncCounters()

    await _sync_l2vpn_terminations(
        object(),
        inventory,
        counters,
        vnet_l2vpn_ids={("evpn-prod", "tenant100"): 42},
    )

    assert counters.terminations == 1
    termination_call = next(
        call for call in calls if call["path"] == "/api/vpn/l2vpn-terminations/"
    )
    assert termination_call["payload"] == {
        "l2vpn": 42,
        "assigned_object_type": "ipam.vlan",
        "assigned_object_id": 7,
    }
    binding_call = next(
        call for call in calls if call["path"] == "/api/plugins/proxbox/sdn-bindings/"
    )
    assert binding_call["payload"]["source_type"] == "l2vpn-termination"
    assert binding_call["payload"]["target_type"] == "ipam.vlan"
    assert binding_call["payload"]["target_id"] == 7


@pytest.mark.asyncio
async def test_sdn_l2vpn_termination_resolves_vlan_by_vid(monkeypatch):
    calls = []

    async def _fake_rest_first(_nb, path, *, query):
        if path == "/api/ipam/vlans/":
            assert query == {"vid": 200, "limit": 2}
            return {"id": 77, "vid": 200}
        if path == "/api/vpn/l2vpn-terminations/":
            return None
        raise AssertionError(f"unexpected rest_first path: {path}")

    async def _fake_reconcile(_nb, path, *, lookup, payload, fields, **_kwargs):
        del lookup, fields
        calls.append({"path": path, "payload": payload})
        return 99 if path == "/api/vpn/l2vpn-terminations/" else 199, True

    import proxbox_api.services.sync.sdn as sdn_module

    monkeypatch.setattr(sdn_module, "rest_first_async", _fake_rest_first)
    monkeypatch.setattr(sdn_module, "_reconcile", _fake_reconcile)

    inventory = SdnInventory(
        endpoint_id=17,
        endpoint_name="pve-a",
        cluster_name="cluster-a",
        node_status=[
            SdnNodeStatusSchema(
                endpoint_id=17,
                cluster_name="cluster-a",
                node="node-a",
                zone="vxlan-prod",
                vnet="tenant200",
                kind="node-zone-content",
                name="net0",
                raw_config={"vid": 200},
            )
        ],
    )
    counters = SdnSyncCounters()

    await _sync_l2vpn_terminations(
        object(),
        inventory,
        counters,
        vnet_l2vpn_ids={("vxlan-prod", "tenant200"): 42},
    )

    termination_call = next(
        call for call in calls if call["path"] == "/api/vpn/l2vpn-terminations/"
    )
    assert termination_call["payload"] == {
        "l2vpn": 42,
        "assigned_object_type": "ipam.vlan",
        "assigned_object_id": 77,
    }
    assert counters.terminations == 1


@pytest.mark.asyncio
async def test_sdn_l2vpn_termination_conflict_records_binding(monkeypatch):
    calls = []

    async def _fake_rest_first(_nb, _path, *, query):
        del query
        return {"id": 55, "l2vpn": {"id": 88}}

    async def _fake_reconcile(_nb, path, *, lookup, payload, fields, **_kwargs):
        del lookup, fields
        calls.append({"path": path, "payload": payload})
        return 199, True

    import proxbox_api.services.sync.sdn as sdn_module

    monkeypatch.setattr(sdn_module, "rest_first_async", _fake_rest_first)
    monkeypatch.setattr(sdn_module, "_reconcile", _fake_reconcile)

    inventory = SdnInventory(
        endpoint_id=17,
        endpoint_name="pve-a",
        cluster_name="cluster-a",
        node_status=[
            SdnNodeStatusSchema(
                endpoint_id=17,
                cluster_name="cluster-a",
                node="node-a",
                zone="evpn-prod",
                vnet="tenant100",
                kind="node-zone-content",
                name="net0",
                raw_config={
                    "assigned_object_type": "ipam.vlan",
                    "assigned_object_id": 7,
                },
            )
        ],
    )
    counters = SdnSyncCounters()

    await _sync_l2vpn_terminations(
        object(),
        inventory,
        counters,
        vnet_l2vpn_ids={("evpn-prod", "tenant100"): 42},
    )

    assert counters.terminations == 0
    assert counters.skipped == 1
    assert all(call["path"] != "/api/vpn/l2vpn-terminations/" for call in calls)
    binding_call = next(
        call for call in calls if call["path"] == "/api/plugins/proxbox/sdn-bindings/"
    )
    assert binding_call["payload"]["status"] == "conflict"
    assert "already terminates L2VPN 88" in binding_call["payload"]["conflict_reason"]


def test_sdn_bgp_projection_mode_and_community_helpers():
    assert _sdn_bgp_projection_enabled("always") is True
    assert _sdn_bgp_projection_enabled("bootstrap_only") is True
    assert _sdn_bgp_projection_enabled("disabled") is False
    assert _sdn_bgp_projection_enabled("bogus") is False
    assert _valid_community_values("65000:100, invalid 4294967296:1") == ["65000:100"]


@pytest.mark.asyncio
async def test_sdn_bgp_projection_skips_when_optional_plugin_unavailable(monkeypatch):
    import proxbox_api.services.sync.sdn as sdn_module

    async def _endpoint_id(_nb, _px):
        return 17

    monkeypatch.setattr(sdn_module, "_resolve_plugin_endpoint_id", _endpoint_id)

    async def _empty_l2vpn(*_args, **_kwargs):
        return {}, {}

    async def _noop(*_args, **_kwargs):
        return None

    async def _missing_bgp(_nb):
        return False

    async def _unexpected_projection(*_args, **_kwargs):
        raise AssertionError("BGP projection must not run when netbox_bgp is unavailable")

    monkeypatch.setattr(sdn_module, "_sync_netbox_l2vpn_objects", _empty_l2vpn)
    monkeypatch.setattr(sdn_module, "_sync_l2vpn_terminations", _noop)
    monkeypatch.setattr(sdn_module, "_sync_plugin_inventory", _noop)
    monkeypatch.setattr(sdn_module, "_record_object_bindings", _noop)
    monkeypatch.setattr(sdn_module, "_netbox_bgp_available", _missing_bgp)
    monkeypatch.setattr(sdn_module, "_sync_netbox_bgp_projection", _unexpected_projection)

    result = await sync_sdn_to_netbox(
        netbox_session=object(),
        pxs=[_FakePx({})],
        sync_mode_sdn_bgp="always",
    )

    counters = result["counters"]
    assert counters["warnings"] == [
        {
            "kind": "bgp-projection",
            "name": "cluster-a",
            "warning": "Skipped because the optional netbox_bgp API is unavailable.",
        }
    ]


@pytest.mark.asyncio
async def test_sdn_bgp_projection_writes_bgp_objects_and_trace_bindings(monkeypatch):
    calls = []
    next_id = 100

    async def _fake_reconcile(_nb, path, *, lookup, payload, fields, **_kwargs):
        nonlocal next_id
        del fields
        next_id += 1
        calls.append({"path": path, "lookup": lookup, "payload": payload, "id": next_id})
        return next_id, True

    async def _fake_rest_first(_nb, path, *, query):
        if path == "/api/ipam/prefixes/":
            return {"id": 501} if query["prefix"] == "10.10.0.0/24" else None
        if path == "/api/ipam/ip-addresses/":
            ids = {"192.0.2.1/32": 601, "192.0.2.2/32": 602}
            record_id = ids.get(query["address"])
            return {"id": record_id} if record_id is not None else None
        if path == "/api/ipam/asns/":
            ids = {65000: 701, 65001: 702}
            record_id = ids.get(query["asn"])
            return {"id": record_id} if record_id is not None else None
        raise AssertionError(f"unexpected rest_first path: {path}")

    import proxbox_api.services.sync.sdn as sdn_module

    monkeypatch.setattr(sdn_module, "_reconcile", _fake_reconcile)
    monkeypatch.setattr(sdn_module, "rest_first_async", _fake_rest_first)

    inventory = SdnInventory(
        endpoint_id=17,
        endpoint_name="pve-a",
        cluster_name="cluster-a",
        controllers=[
            sdn_module.SdnControllerSchema(
                endpoint_id=17,
                cluster_name="cluster-a",
                controller="bgp1",
                type="bgp",
                asn=65000,
                peers="192.0.2.2=65001",
                loopback="192.0.2.1",
            )
        ],
        route_maps=[
            sdn_module.SdnRouteMapSchema(
                endpoint_id=17,
                cluster_name="cluster-a",
                name="RM-IN",
                action="permit",
                match_ip="PL-IN",
                set_community="65000:100 invalid",
                order=10,
            )
        ],
        prefix_lists=[
            sdn_module.SdnPrefixListSchema(
                endpoint_id=17,
                cluster_name="cluster-a",
                name="PL-IN",
                cidr="10.10.0.0/24",
                action="permit",
                le=32,
            )
        ],
    )
    counters = SdnSyncCounters()

    await _sync_netbox_bgp_projection(object(), inventory, counters)

    paths = [call["path"] for call in calls]
    assert "/api/plugins/bgp/prefix-list/" in paths
    assert "/api/plugins/bgp/prefix-list-rule/" in paths
    assert "/api/plugins/bgp/routing-policy/" in paths
    assert "/api/plugins/bgp/routing-policy-rule/" in paths
    assert "/api/plugins/bgp/community/" in paths
    assert "/api/plugins/bgp/peer-group/" in paths
    assert "/api/plugins/bgp/session/" in paths
    assert counters.bgp_prefix_lists == 1
    assert counters.bgp_prefix_list_rules == 1
    assert counters.bgp_routing_policies == 1
    assert counters.bgp_routing_policy_rules == 1
    assert counters.bgp_communities == 1
    assert counters.bgp_peer_groups == 1
    assert counters.bgp_sessions == 1

    prefix_rule = next(
        call for call in calls if call["path"] == "/api/plugins/bgp/prefix-list-rule/"
    )
    assert prefix_rule["payload"]["prefix"] == 501
    assert prefix_rule["payload"]["prefix_custom"] is None
    policy_rule = next(
        call for call in calls if call["path"] == "/api/plugins/bgp/routing-policy-rule/"
    )
    assert policy_rule["payload"]["match_ip_address"]
    assert policy_rule["payload"]["set_actions"] == {"communities": ["65000:100"]}
    session = next(call for call in calls if call["path"] == "/api/plugins/bgp/session/")
    assert session["payload"]["local_address"] == 601
    assert session["payload"]["remote_address"] == 602
    assert session["payload"]["local_as"] == 701
    assert session["payload"]["remote_as"] == 702
    binding_targets = {
        call["payload"]["target_type"]
        for call in calls
        if call["path"] == "/api/plugins/proxbox/sdn-bindings/"
    }
    assert "netbox_bgp.bgpsession" in binding_targets
    assert "netbox_bgp.bgppeergroup" in binding_targets
