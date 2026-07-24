"""Tests for the node-interface route wiring behind the ``sync_node_interfaces`` flag.

Verifies that ``create_all_device_interfaces``:
- routes to ``sync_node_network`` with the RAW ``/nodes/{node}/network`` payload
  and the NetBox-resolved device id when the flag is on;
- keeps the historical per-interface path (``sync_node_interface_and_ip``) when
  the flag is off.
"""

from types import SimpleNamespace

import proxbox_api.netbox_rest as netbox_rest
import proxbox_api.services.sync.network as network
from proxbox_api.routes import dcim

RAW_NETWORK = [
    {"iface": "vmbr0", "type": "bridge", "active": 1, "bridge_ports": "eno1"},
    {"iface": "eno1", "type": "eth", "active": 1},
]


def _fake_proxmox_session(captured):
    class _Req:
        def __init__(self, path):
            self._path = path

        def get(self, **kwargs):
            captured["path"] = self._path
            return RAW_NETWORK

    return SimpleNamespace(name="cluster-a", session=lambda path: _Req(path))


def _cluster_status():
    node = SimpleNamespace(name="pve01", id="node/pve01")
    return [SimpleNamespace(name="cluster-a", node_list=[node])]


async def test_flag_on_routes_to_sync_node_network_with_raw_payload(monkeypatch):
    captured: dict = {}
    calls: dict = {"network": [], "per_iface": 0}

    async def fake_first(nb, path, *, query, **kw):
        assert path == "/api/dcim/devices/"
        assert query["name"] == "pve01"
        return {"id": 42, "name": "pve01"}

    async def fake_sync_node_network(nb, device, network_entries, tag_refs, **kw):
        calls["network"].append({"device": device, "entries": network_entries})
        return [{"id": 10, "name": "vmbr0"}, {"id": 11, "name": "eno1"}]

    async def fake_per_iface(*args, **kwargs):
        calls["per_iface"] += 1
        return {}

    monkeypatch.setattr(netbox_rest, "rest_first_async", fake_first)
    monkeypatch.setattr(network, "sync_node_network", fake_sync_node_network)
    monkeypatch.setattr(network, "sync_node_interface_and_ip", fake_per_iface)
    monkeypatch.setattr(dcim, "nested_tag_payload", lambda tag: [])

    results = await dcim.create_all_device_interfaces(
        netbox_session=object(),
        tag=object(),
        clusters_status=_cluster_status(),
        pxs=[_fake_proxmox_session(captured)],
        behavior_flags=SimpleNamespace(sync_node_interfaces=True),
    )

    assert captured["path"] == "/nodes/pve01/network"
    assert calls["per_iface"] == 0
    assert len(calls["network"]) == 1
    # Raw payload (hyphenated/topology keys preserved) and resolved device id.
    assert calls["network"][0]["device"] == {"id": 42, "name": "pve01"}
    assert calls["network"][0]["entries"] == RAW_NETWORK
    assert len(results) == 2


async def test_flag_off_keeps_per_interface_path(monkeypatch):
    calls: dict = {"network": 0, "per_iface": 0}

    async def fake_sync_node_network(*args, **kwargs):
        calls["network"] += 1
        return []

    async def fake_per_iface(*args, **kwargs):
        calls["per_iface"] += 1
        return {"id": 1, "name": "vmbr0"}

    monkeypatch.setattr(network, "sync_node_network", fake_sync_node_network)
    monkeypatch.setattr(network, "sync_node_interface_and_ip", fake_per_iface)
    monkeypatch.setattr(dcim, "nested_tag_payload", lambda tag: [])

    # node_obj.network raises (no attribute) -> empty per-interface list, but the
    # per-interface path is still the one selected (sync_node_network untouched).
    node = SimpleNamespace(
        name="pve01",
        id="node/pve01",
        network=[
            SimpleNamespace(iface="vmbr0", type="bridge", cidr=None, address=None, vlan_id=None)
        ],
    )
    clusters = [SimpleNamespace(name="cluster-a", node_list=[node])]

    await dcim.create_all_device_interfaces(
        netbox_session=object(),
        tag=object(),
        clusters_status=clusters,
        pxs=[object()],
        behavior_flags=SimpleNamespace(sync_node_interfaces=False),
    )

    assert calls["network"] == 0
    assert calls["per_iface"] == 1
