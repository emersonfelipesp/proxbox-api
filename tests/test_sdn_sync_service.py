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
    _split_csv,
    _sync_l2vpn_terminations,
    _sync_netbox_l2vpn_objects,
    _to_vnet,
    _to_zone,
    _valid_prefix,
    collect_sdn_inventory_for_session,
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
