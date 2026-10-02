"""Tests for the node-interface route wiring behind the ``sync_node_interfaces`` flag.

Verifies that ``create_all_device_interfaces``:
- routes to ``sync_node_network`` with the RAW ``/nodes/{node}/network`` payload
  and the NetBox-resolved device record when the flag is on;
- keeps the historical per-interface path (``sync_node_interface_and_ip``) when
  the flag is off.
"""

from types import SimpleNamespace

import pytest

import proxbox_api.services.sync.network as network
from proxbox_api.exception import ProxboxException
from proxbox_api.routes import dcim


class _CapturingWebSocket:
    def __init__(self):
        self.events: list[dict] = []

    async def send_json(self, payload):
        self.events.append(payload)


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

    async def fake_resolve_device(nb, node_name, *, clusters_status, cluster_name):
        assert node_name == "pve01"
        return {"id": 42, "name": "pve01"}

    async def fake_load_node_network(session, node):
        # The full-topology path re-fetches the raw payload itself, so the
        # normalized loader is skipped entirely when the flag is on.
        raise AssertionError("load_proxmox_node_network must not run on the full-topology path")

    async def fake_sync_node_network(nb, device, network_entries, tag_refs, **kw):
        calls["network"].append({"device": device, "entries": network_entries})
        return [{"id": 10, "name": "vmbr0"}, {"id": 11, "name": "eno1"}]

    async def fake_per_iface(*args, **kwargs):
        calls["per_iface"] += 1
        return {}

    monkeypatch.setattr(dcim, "_resolve_netbox_device_by_name", fake_resolve_device)
    monkeypatch.setattr(dcim, "load_proxmox_node_network", fake_load_node_network)
    monkeypatch.setattr(network, "sync_node_network", fake_sync_node_network)
    monkeypatch.setattr(dcim, "sync_node_interface_and_ip", fake_per_iface)
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
    # Raw payload (hyphenated/topology keys preserved) and resolved device record.
    assert calls["network"][0]["device"] == {"id": 42, "name": "pve01"}
    assert calls["network"][0]["entries"] == RAW_NETWORK
    assert len(results) == 2


async def test_flag_off_keeps_per_interface_path(monkeypatch):
    calls: dict = {"network": 0, "per_iface": 0}

    async def fake_resolve_device(nb, node_name, *, clusters_status, cluster_name):
        return {"id": 42, "name": node_name}

    async def fake_load_node_network(session, node):
        return [{"iface": "vmbr0", "type": "bridge"}]

    async def fake_sync_node_network(*args, **kwargs):
        calls["network"] += 1
        return []

    async def fake_per_iface(*args, **kwargs):
        calls["per_iface"] += 1
        return {"id": 1, "name": "vmbr0"}

    monkeypatch.setattr(dcim, "_resolve_netbox_device_by_name", fake_resolve_device)
    monkeypatch.setattr(dcim, "load_proxmox_node_network", fake_load_node_network)
    monkeypatch.setattr(network, "sync_node_network", fake_sync_node_network)
    monkeypatch.setattr(dcim, "sync_node_interface_and_ip", fake_per_iface)
    monkeypatch.setattr(dcim, "nested_tag_payload", lambda tag: [])

    await dcim.create_all_device_interfaces(
        netbox_session=object(),
        tag=object(),
        clusters_status=_cluster_status(),
        pxs=[SimpleNamespace(name="cluster-a")],
        behavior_flags=SimpleNamespace(sync_node_interfaces=False),
    )

    # Full-topology reconcile untouched; the per-interface path handled the node.
    assert calls["network"] == 0
    assert calls["per_iface"] == 1


async def test_flag_on_topology_failure_raises_and_emits_error_event(monkeypatch):
    """A failed topology reconcile must surface, not report a silent count:0 success."""

    async def fake_resolve_device(nb, node_name, *, clusters_status, cluster_name):
        return {"id": 42, "name": node_name}

    async def fake_sync_node_network(*args, **kwargs):
        raise RuntimeError("netbox exploded")

    monkeypatch.setattr(dcim, "_resolve_netbox_device_by_name", fake_resolve_device)
    monkeypatch.setattr(network, "sync_node_network", fake_sync_node_network)
    monkeypatch.setattr(dcim, "nested_tag_payload", lambda tag: [])

    websocket = _CapturingWebSocket()

    with pytest.raises(ProxboxException):
        await dcim.create_all_device_interfaces(
            netbox_session=object(),
            tag=object(),
            clusters_status=_cluster_status(),
            pxs=[_fake_proxmox_session({})],
            websocket=websocket,
            use_websocket=True,
            behavior_flags=SimpleNamespace(sync_node_interfaces=True),
        )

    # The node-level failure is visible in the stream as a completed:False error.
    error_events = [
        e
        for e in websocket.events
        if e.get("object") == "node_interface"
        and e.get("data", {}).get("completed") is False
        and "error" in e.get("data", {})
    ]
    assert error_events, "expected a completed:False node_interface error event"
    assert error_events[-1]["data"]["error"] == "netbox exploded"


async def _run_flag_on(monkeypatch, sync_result, websocket=None):
    async def fake_resolve_device(nb, node_name, *, clusters_status, cluster_name):
        return {"id": 42, "name": node_name}

    async def fake_sync_node_network(nb, device, network_entries, tag_refs, **kw):
        return sync_result(device["name"])

    monkeypatch.setattr(dcim, "_resolve_netbox_device_by_name", fake_resolve_device)
    monkeypatch.setattr(network, "sync_node_network", fake_sync_node_network)
    monkeypatch.setattr(dcim, "nested_tag_payload", lambda tag: [])

    return await dcim.create_all_device_interfaces(
        netbox_session=object(),
        tag=object(),
        clusters_status=_cluster_status(),
        pxs=[_fake_proxmox_session({})],
        websocket=websocket,
        use_websocket=websocket is not None,
        behavior_flags=SimpleNamespace(sync_node_interfaces=True),
    )


def _degraded_result(node):
    from proxbox_api.services.sync.stage_result import WarningList

    return WarningList(
        [{"id": 10, "name": "vmbr0"}],
        warnings=[{"device": node, "interface": "vmbr0", "kind": "mac", "reason": "boom"}],
    )


async def test_degraded_node_sync_propagates_warnings_through_create_all(monkeypatch):
    results = await _run_flag_on(monkeypatch, _degraded_result)

    assert results.degraded is True
    assert results.warnings == [
        {"device": "pve01", "interface": "vmbr0", "kind": "mac", "reason": "boom"}
    ]
    assert [r["name"] for r in results] == ["vmbr0"]


async def test_clean_node_sync_returns_plain_list(monkeypatch):
    results = await _run_flag_on(monkeypatch, lambda node: [{"id": 10, "name": "vmbr0"}])

    assert type(results) is list
    assert not hasattr(results, "warnings")


async def test_degraded_node_sync_marks_completion_event(monkeypatch):
    websocket = _CapturingWebSocket()
    await _run_flag_on(monkeypatch, _degraded_result, websocket)

    done = [
        e["data"]
        for e in websocket.events
        if e.get("object") == "node_interface" and e.get("data", {}).get("count") == 1
    ]
    assert done and done[-1]["degraded"] is True
    assert done[-1]["warnings"][0]["kind"] == "mac"


def test_rest_route_wraps_only_degraded_results(monkeypatch):
    import asyncio

    async def fake_create_all(**kwargs):
        return _degraded_result("pve01") if kwargs["tag"] == "bad" else [{"id": 1}]

    monkeypatch.setattr(dcim, "create_all_device_interfaces", fake_create_all)

    def call(tag):
        return asyncio.run(
            dcim.create_all_devices_interfaces(
                netbox_session=object(),
                clusters_status=[],
                pxs=[],
                tag=tag,
                behavior_flags=None,
            )
        )

    assert call("ok") == [{"id": 1}]
    wrapped = call("bad")
    assert wrapped["degraded"] is True
    assert wrapped["count"] == 1
    assert wrapped["interfaces"] == [{"id": 10, "name": "vmbr0"}]
    assert wrapped["warnings"][0]["kind"] == "mac"
