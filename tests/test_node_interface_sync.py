"""Regression tests for Proxmox node interface synchronization."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from proxbox_api.enum.status_mapping import NetBoxInterfaceType
from proxbox_api.routes.dcim import create_all_device_interfaces, create_proxmox_device_interfaces
from proxbox_api.routes.proxmox.nodes import ProxmoxNodeInterfaceSchema
from proxbox_api.schemas.proxmox import ClusterNodeStatusSchema, ClusterStatusSchema


class _FakeNetworkAccessor:
    def __init__(self, payload: list[dict[str, object]]) -> None:
        self._payload = payload

    def get(self, **_kwargs):
        return self._payload


class _FakeProxmoxSession:
    def __init__(self, name: str, networks_by_node: dict[str, list[dict[str, object]]]) -> None:
        self.name = name
        self._networks_by_node = networks_by_node
        self.calls: list[str] = []

    def session(self, path: str) -> _FakeNetworkAccessor:
        self.calls.append(path)
        node = path.strip("/").split("/")[1]
        return _FakeNetworkAccessor(self._networks_by_node[node])


def _tag() -> SimpleNamespace:
    return SimpleNamespace(name="Proxbox", slug="proxbox", color="ff5722")


def test_batch_node_interface_sync_fetches_per_node_network_and_uses_device_name(
    monkeypatch,
):
    interface_payloads: list[dict[str, object]] = []
    ip_payloads: list[dict[str, object]] = []

    async def _fake_list(_nb, path, *, query=None):
        if path == "/api/virtualization/clusters/":
            assert query == {"name": "lab", "limit": 2}
            return [{"id": 11, "name": "lab", "scope_type": "dcim.site", "scope_id": 41}]
        if path == "/api/dcim/devices/":
            assert query == {"name": "pve-a", "limit": 2, "site_id": 41}
            return [{"id": 42, "name": "pve-a", "site": 41, "primary_ip4": None}]
        raise AssertionError(f"unexpected list lookup: {path} {query}")

    async def _fake_reconcile(_nb, path, *, lookup, payload, **kwargs):
        assert path == "/api/dcim/interfaces/"
        interface_payloads.append(
            {
                "lookup": dict(lookup),
                "payload": dict(payload),
                "strict_lookup": kwargs.get("strict_lookup"),
                "lookup_query_field_map": dict(kwargs.get("lookup_query_field_map") or {}),
            }
        )
        return {"id": 9001, **payload}

    async def _fake_ip(_nb, **kwargs):
        ip_payloads.append(dict(kwargs))
        return 7001

    monkeypatch.setattr("proxbox_api.routes.dcim.rest_list_async", _fake_list)
    monkeypatch.setattr("proxbox_api.services.sync.network.rest_reconcile_async", _fake_reconcile)
    monkeypatch.setattr("proxbox_api.services.sync.network._reconcile_interface_ip", _fake_ip)

    cluster_status = ClusterStatusSchema(
        id="cluster/lab",
        name="lab",
        type="cluster",
        nodes=1,
        quorate=True,
        version=1,
        mode="Proxmox",
        node_list=[
            ClusterNodeStatusSchema(
                id="node/pve-a",
                name="pve-a",
                type="node",
                ip="192.0.2.10",
                local=False,
                nodeid=1,
                online=True,
            )
        ],
    )
    px = _FakeProxmoxSession(
        "lab",
        {"pve-a": [{"iface": "eno1", "type": "eth", "cidr": "192.0.2.10/24"}]},
    )

    results = asyncio.run(
        create_all_device_interfaces(
            netbox_session=object(),
            tag=_tag(),
            clusters_status=[cluster_status],
            pxs=[px],
        )
    )

    assert px.calls == ["/nodes/pve-a/network"]
    assert len(results) == 1
    assert results[0]["name"] == "eno1"
    assert interface_payloads == [
        {
            "lookup": {"device": 42, "name": "eno1"},
            "payload": {
                "device": 42,
                "name": "eno1",
                "status": "active",
                "type": NetBoxInterfaceType.other,
                "untagged_vlan": None,
                "mode": None,
                "tags": [{"name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
            },
            "strict_lookup": True,
            "lookup_query_field_map": {"device": "device_id"},
        }
    ]
    assert ip_payloads[0]["assigned_object_type"] == "dcim.interface"
    assert ip_payloads[0]["interface_lookup_field"] == "interface_id"


def test_named_node_interface_sync_uses_requested_device_not_first_dependency_result(
    monkeypatch,
):
    interface_payloads: list[dict[str, object]] = []
    patched_devices: list[tuple[object, dict[str, object]]] = []

    async def _fake_list(_nb, path, *, query=None):
        if path == "/api/virtualization/clusters/":
            assert query == {"name": "lab", "limit": 2}
            return [{"id": 11, "name": "lab", "scope_type": "dcim.site", "scope_id": 41}]
        raise AssertionError(f"named device should be found in dependency results: {path} {query}")

    async def _fake_reconcile(_nb, path, *, lookup, payload, **kwargs):
        assert path == "/api/dcim/interfaces/"
        interface_payloads.append(
            {
                "lookup": dict(lookup),
                "payload": dict(payload),
                "strict_lookup": kwargs.get("strict_lookup"),
                "lookup_query_field_map": dict(kwargs.get("lookup_query_field_map") or {}),
            }
        )
        return {"id": 9100, **payload}

    async def _fake_ip(_nb, **_kwargs):
        return 7100

    async def _fake_patch(_nb, path, record_id, payload):
        assert path == "/api/dcim/devices/"
        patched_devices.append((record_id, dict(payload)))
        return {"id": record_id, **payload}

    monkeypatch.setattr("proxbox_api.routes.dcim.rest_list_async", _fake_list)
    monkeypatch.setattr("proxbox_api.routes.dcim.rest_patch_async", _fake_patch)
    monkeypatch.setattr("proxbox_api.services.sync.network.rest_reconcile_async", _fake_reconcile)
    monkeypatch.setattr("proxbox_api.services.sync.network._reconcile_interface_ip", _fake_ip)

    results = asyncio.run(
        create_proxmox_device_interfaces(
            node="pve-b",
            nodes=[
                {"id": 101, "name": "pve-a", "site": 41, "primary_ip4": None},
                {"id": 202, "name": "pve-b", "site": 41, "primary_ip4": None},
            ],
            netbox_session=object(),
            tag=_tag(),
            node_interfaces=[
                ProxmoxNodeInterfaceSchema(
                    iface="vmbr0",
                    type="bridge",
                    cidr="198.51.100.2/24",
                )
            ],
            clusters_status=[
                ClusterStatusSchema(
                    id="cluster/lab",
                    name="lab",
                    type="cluster",
                    nodes=1,
                    quorate=True,
                    version=1,
                    mode="Proxmox",
                    node_list=[
                        ClusterNodeStatusSchema(
                            id="node/pve-b",
                            name="pve-b",
                            type="node",
                            ip="198.51.100.2",
                            local=False,
                            nodeid=1,
                            online=True,
                        )
                    ],
                )
            ],
            cluster_name="lab",
        )
    )

    assert len(results) == 1
    assert interface_payloads[0]["lookup"] == {"device": 202, "name": "vmbr0"}
    assert interface_payloads[0]["payload"]["device"] == 202
    assert interface_payloads[0]["strict_lookup"] is True
    assert interface_payloads[0]["lookup_query_field_map"] == {"device": "device_id"}
    assert patched_devices == [(202, {"primary_ip4": 7100})]


def test_named_node_interface_sync_resolves_same_name_device_by_cluster_site(
    monkeypatch,
):
    interface_payloads: list[dict[str, object]] = []
    list_calls: list[tuple[str, dict[str, object] | None]] = []

    async def _fake_list(_nb, path, *, query=None):
        list_calls.append((path, dict(query or {})))
        if path == "/api/virtualization/clusters/":
            assert query == {"name": "lab-b", "limit": 2}
            return [{"id": 22, "name": "lab-b", "scope_type": "dcim.site", "scope_id": 202}]
        if path == "/api/dcim/devices/":
            assert query == {"name": "pve-shared", "limit": 2, "site_id": 202}
            return [{"id": 2002, "name": "pve-shared", "site": 202, "primary_ip4": 8802}]
        raise AssertionError(f"unexpected list lookup: {path} {query}")

    async def _fake_reconcile(_nb, path, *, lookup, payload, **_kwargs):
        assert path == "/api/dcim/interfaces/"
        interface_payloads.append({"lookup": dict(lookup), "payload": dict(payload)})
        return {"id": 9902, **payload}

    monkeypatch.setattr("proxbox_api.routes.dcim.rest_list_async", _fake_list)
    monkeypatch.setattr("proxbox_api.services.sync.network.rest_reconcile_async", _fake_reconcile)

    results = asyncio.run(
        create_proxmox_device_interfaces(
            node="pve-shared",
            nodes=[
                {"id": 1001, "name": "pve-shared", "site": 101, "primary_ip4": 8801},
                {"id": 2002, "name": "pve-shared", "site": 202, "primary_ip4": 8802},
            ],
            netbox_session=object(),
            tag=_tag(),
            node_interfaces=[
                ProxmoxNodeInterfaceSchema(
                    iface="eno1",
                    type="eth",
                )
            ],
            clusters_status=[
                ClusterStatusSchema(
                    id="cluster/lab-a",
                    name="lab-a",
                    type="cluster",
                    nodes=1,
                    quorate=True,
                    version=1,
                    mode="Proxmox",
                    node_list=[
                        ClusterNodeStatusSchema(
                            id="node/pve-shared",
                            name="pve-shared",
                            type="node",
                            ip="192.0.2.10",
                            local=False,
                            nodeid=1,
                            online=True,
                        )
                    ],
                ),
                ClusterStatusSchema(
                    id="cluster/lab-b",
                    name="lab-b",
                    type="cluster",
                    nodes=1,
                    quorate=True,
                    version=1,
                    mode="Proxmox",
                    node_list=[
                        ClusterNodeStatusSchema(
                            id="node/pve-shared",
                            name="pve-shared",
                            type="node",
                            ip="198.51.100.10",
                            local=False,
                            nodeid=1,
                            online=True,
                        )
                    ],
                ),
            ],
            cluster_name="lab-b",
        )
    )

    assert results == [{"id": 9902, "name": "eno1"}]
    assert interface_payloads == [
        {
            "lookup": {"device": 2002, "name": "eno1"},
            "payload": {
                "device": 2002,
                "name": "eno1",
                "status": "active",
                "type": NetBoxInterfaceType.other,
                "untagged_vlan": None,
                "mode": None,
                "tags": [{"name": "Proxbox", "slug": "proxbox", "color": "ff5722"}],
            },
        }
    ]
    assert list_calls == [("/api/virtualization/clusters/", {"name": "lab-b", "limit": 2})]


def test_node_interface_reconcile_uses_strict_device_scoped_lookup(monkeypatch):
    reconcile_calls: list[dict[str, object]] = []

    async def _fake_reconcile(_nb, path, *, lookup, payload, **kwargs):
        reconcile_calls.append(
            {
                "path": path,
                "lookup": dict(lookup),
                "payload": dict(payload),
                "strict_lookup": kwargs.get("strict_lookup"),
                "lookup_query_field_map": dict(kwargs.get("lookup_query_field_map") or {}),
            }
        )
        return {"id": 5502, **payload}

    monkeypatch.setattr("proxbox_api.services.sync.network.rest_reconcile_async", _fake_reconcile)

    from proxbox_api.services.sync.network import sync_node_interface_and_ip

    result = asyncio.run(
        sync_node_interface_and_ip(
            nb=object(),
            device={"id": 202, "name": "pve-b"},
            interface_name="eno1",
            interface_config={"type": "eth"},
            tag_refs=[],
        )
    )

    assert result == {"id": 5502, "name": "eno1"}
    assert reconcile_calls == [
        {
            "path": "/api/dcim/interfaces/",
            "lookup": {"device": 202, "name": "eno1"},
            "payload": {
                "device": 202,
                "name": "eno1",
                "status": "active",
                "type": NetBoxInterfaceType.other,
                "untagged_vlan": None,
                "mode": None,
                "tags": [],
            },
            "strict_lookup": True,
            "lookup_query_field_map": {"device": "device_id"},
        }
    ]
