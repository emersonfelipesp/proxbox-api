"""Tests for dual core/guest VM interface synchronization."""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

from proxbox_api.exception import ProxboxException
from proxbox_api.services.sync.guest_vm_interface import reconcile_guest_vm_interfaces
from proxbox_api.services.sync.vm_network import sync_vm_interfaces


def _install_sync_vm_interfaces_patches(
    monkeypatch,
    calls: list[dict[str, object]],
    *,
    plugin_unavailable: bool = False,
) -> None:
    async def _fake_rest_reconcile(_nb, path, *, lookup, payload, **kwargs):
        calls.append(
            {
                "kind": "reconcile",
                "path": path,
                "lookup": lookup,
                "payload": payload,
            }
        )
        if path == "/api/virtualization/interfaces/":
            interface_id_by_name = {"net0": 66, "net1": 67, "ens18": 66}
            return {
                "id": interface_id_by_name.get(str(payload.get("name")), 66),
                "name": payload.get("name"),
                "virtual_machine": payload.get("virtual_machine"),
            }
        if path == "/api/ipam/ip-addresses/":
            return {
                "id": 77,
                "address": payload.get("address"),
                "assigned_object_id": payload.get("assigned_object_id"),
            }
        return {"id": 999, **payload}

    async def _fake_plugin_first(_nb, path, *, query=None, **kwargs):
        calls.append({"kind": "plugin_get", "path": path, "query": query or {}})
        _ = kwargs
        if plugin_unavailable:
            raise ProxboxException(
                message="NetBox REST request failed",
                detail="404 Not Found",
            )
        return None

    async def _fake_plugin_create(_nb, path, payload, **kwargs):
        calls.append({"kind": "reconcile", "path": path, "payload": payload})
        _ = kwargs
        if plugin_unavailable:
            raise ProxboxException(
                message="NetBox REST request failed",
                detail="404 Not Found",
            )
        if path == "/api/plugins/proxbox/guest-vm-interfaces/":
            return {
                "id": 901,
                "virtual_machine": payload.get("virtual_machine"),
                "vm_interface": payload.get("vm_interface"),
                "name": payload.get("name"),
                "mac_address": payload.get("mac_address"),
            }
        if path == "/api/plugins/proxbox/guest-vm-interface-addresses/":
            return {
                "id": 902,
                "guest_interface": payload.get("guest_interface"),
                "ip_address": payload.get("ip_address"),
            }
        return {"id": 999, **payload}

    async def _fake_plugin_patch(_nb, path, record_id, payload, **kwargs):
        calls.append(
            {
                "kind": "plugin_patch",
                "path": path,
                "record_id": record_id,
                "payload": payload,
            }
        )
        _ = kwargs
        return {"id": record_id, **payload}

    async def _fake_rest_list(*args, **kwargs):
        return []

    async def _fake_reconcile_mac(*args, **kwargs):
        calls.append({"kind": "mac", **kwargs})
        return None

    async def _fake_ensure_bridge_interfaces(*args, **kwargs):
        calls.append({"kind": "bridge", "args": args, "kwargs": kwargs})
        return 700

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.rest_reconcile_async",
        _fake_rest_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.ip_ownership.rest_reconcile_async",
        _fake_rest_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_first_async",
        _fake_plugin_first,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_create_async",
        _fake_plugin_create,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_patch_async",
        _fake_plugin_patch,
    )
    monkeypatch.setattr("proxbox_api.services.sync.ip_ownership.rest_list_async", _fake_rest_list)
    monkeypatch.setattr("proxbox_api.services.sync.network.rest_list_async", _fake_rest_list)
    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.reconcile_mac_for_vm_interface",
        _fake_reconcile_mac,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.bridge_interfaces.ensure_bridge_interfaces",
        _fake_ensure_bridge_interfaces,
    )


def _run_sync(
    *,
    monkeypatch,
    calls: list[dict[str, object]],
    guest_interfaces: list[dict[str, object]],
    network_configs: list[dict[str, dict[str, str]]] | None = None,
    plugin_unavailable: bool = False,
    strategy: str = "guest_os_model",
) -> tuple[list[dict[str, object]], int | None]:
    _install_sync_vm_interfaces_patches(
        monkeypatch,
        calls,
        plugin_unavailable=plugin_unavailable,
    )
    return asyncio.run(
        sync_vm_interfaces(
            nb=SimpleNamespace(),
            virtual_machine={"id": 55, "name": "vm01"},
            vm_config={},
            guest_agent_interfaces=guest_interfaces,
            network_configs=network_configs
            or [
                {
                    "net0": {
                        "name": "net0",
                        "virtio": "AA:BB:CC:DD:EE:FF",
                        "ip": "10.0.0.20/24",
                    }
                }
            ],
            tag_refs=[{"name": "Proxbox", "slug": "proxbox"}],
            use_guest_agent_interface_name=True,
            vm_interface_sync_strategy=strategy,
        )
    )


def test_no_agent_vm_syncs_core_interface_and_ip_without_guest_plugin(monkeypatch):
    calls: list[dict[str, object]] = []

    interfaces, first_ip_id = _run_sync(
        monkeypatch=monkeypatch,
        calls=calls,
        guest_interfaces=[],
    )

    assert interfaces == [{"id": 66, "name": "net0", "virtual_machine": 55}]
    assert first_ip_id == 77
    assert [
        c["payload"]["name"]
        for c in calls
        if c["kind"] == "reconcile" and c["path"] == "/api/virtualization/interfaces/"
    ] == ["net0"]
    assert [c for c in calls if str(c.get("path", "")).startswith("/api/plugins/proxbox/")] == []


def test_guest_os_model_keeps_core_net_name_and_links_guest_to_core_ip(monkeypatch):
    calls: list[dict[str, object]] = []

    _run_sync(
        monkeypatch=monkeypatch,
        calls=calls,
        guest_interfaces=[
            {
                "name": "ens18",
                "mac_address": "aa:bb:cc:dd:ee:ff",
                "ip_addresses": [
                    {"ip_address": "10.0.0.50", "prefix": 24, "ip_address_type": "ipv4"}
                ],
            }
        ],
    )

    core_interface_payloads = [
        c["payload"]
        for c in calls
        if c["kind"] == "reconcile" and c["path"] == "/api/virtualization/interfaces/"
    ]
    core_ip_payloads = [
        c["payload"]
        for c in calls
        if c["kind"] == "reconcile" and c["path"] == "/api/ipam/ip-addresses/"
    ]
    guest_interface_payloads = [
        c["payload"]
        for c in calls
        if c["kind"] == "reconcile" and c["path"] == "/api/plugins/proxbox/guest-vm-interfaces/"
    ]
    guest_address_payloads = [
        c["payload"]
        for c in calls
        if c["kind"] == "reconcile"
        and c["path"] == "/api/plugins/proxbox/guest-vm-interface-addresses/"
    ]

    assert core_interface_payloads[0]["name"] == "net0"
    assert core_ip_payloads == [
        {
            "address": "10.0.0.50/24",
            "assigned_object_type": "virtualization.vminterface",
            "assigned_object_id": 66,
            "status": "active",
            "dns_name": "",
            "tags": [{"name": "Proxbox", "slug": "proxbox"}],
            "custom_fields": core_ip_payloads[0]["custom_fields"],
        }
    ]
    assert len(core_ip_payloads) == 1
    assert guest_interface_payloads == [
        {
            "virtual_machine": 55,
            "vm_interface": 66,
            "name": "ens18",
            "mac_address": "aa:bb:cc:dd:ee:ff",
            "enabled": True,
            "mtu": None,
            "tags": [{"name": "Proxbox", "slug": "proxbox"}],
            "custom_fields": {},
        }
    ]
    assert guest_address_payloads == [{"guest_interface": 901, "ip_address": 77}]


def test_guest_os_model_aggregates_ips_from_guest_interfaces_sharing_config_mac(
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []

    interfaces, first_ip_id = _run_sync(
        monkeypatch=monkeypatch,
        calls=calls,
        network_configs=[
            {
                "net0": {
                    "name": "net0",
                    "virtio": "BC:24:11:20:99:1E",
                    "bridge": "vmbr219",
                    "ip": "10.85.0.52/22",
                }
            },
            {
                "net1": {
                    "name": "net1",
                    "virtio": "BC:24:11:9B:AB:78",
                    "bridge": "vmbr215",
                }
            },
        ],
        guest_interfaces=[
            {
                "name": "eth0",
                "mac_address": "bc:24:11:20:99:1e",
                "ip_addresses": [
                    {"ip_address": "10.85.0.52", "prefix": 22, "ip_address_type": "ipv4"},
                    {
                        "ip_address": "fe80::bc24:11ff:fe20:991e",
                        "prefix": 64,
                        "ip_address_type": "ipv6",
                    },
                ],
            },
            {
                "name": "ens19",
                "mac_address": "bc:24:11:9b:ab:78",
                "ip_addresses": [
                    {"ip_address": "10.81.0.13", "prefix": 22, "ip_address_type": "ipv4"}
                ],
            },
            {
                "name": "ens18",
                "mac_address": "bc:24:11:20:99:1e",
                "ip_addresses": [
                    {"ip_address": "10.83.4.100", "prefix": 23, "ip_address_type": "ipv4"}
                ],
            },
        ],
    )

    assert interfaces[0] == {"id": 66, "name": "net0", "virtual_machine": 55}
    assert first_ip_id == 77

    core_ip_payloads = [
        c["payload"]
        for c in calls
        if c["kind"] == "reconcile" and c["path"] == "/api/ipam/ip-addresses/"
    ]
    net0_ip_payloads = [
        payload for payload in core_ip_payloads if payload["assigned_object_id"] == 66
    ]

    assert [payload["address"] for payload in net0_ip_payloads] == [
        "10.85.0.52/22",
        "10.83.4.100/23",
    ]
    assert {payload["assigned_object_id"] for payload in net0_ip_payloads} == {66}
    assert len(net0_ip_payloads) == 2


def test_legacy_rename_preserves_guest_name_and_logs_deprecation(monkeypatch):
    calls: list[dict[str, object]] = []
    warnings: list[str] = []

    def _capture_warning(message, *args, **kwargs):
        _ = kwargs
        warnings.append(str(message) % args if args else str(message))

    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.logger.warning",
        _capture_warning,
    )

    _run_sync(
        monkeypatch=monkeypatch,
        calls=calls,
        guest_interfaces=[
            {"name": "ens18", "mac_address": "aa:bb:cc:dd:ee:ff", "ip_addresses": []}
        ],
        strategy="legacy_rename",
    )

    core_interface_payloads = [
        c["payload"]
        for c in calls
        if c["kind"] == "reconcile" and c["path"] == "/api/virtualization/interfaces/"
    ]
    assert core_interface_payloads[0]["name"] == "ens18"
    assert [c for c in calls if str(c.get("path", "")).startswith("/api/plugins/proxbox/")] == []
    assert any("vm_interface_sync_strategy=legacy_rename is deprecated" in w for w in warnings)


def test_guest_plugin_404_skips_guest_writes_after_core_sync(monkeypatch):
    calls: list[dict[str, object]] = []

    interfaces, first_ip_id = _run_sync(
        monkeypatch=monkeypatch,
        calls=calls,
        guest_interfaces=[
            {
                "name": "ens18",
                "mac_address": "aa:bb:cc:dd:ee:ff",
                "ip_addresses": [
                    {"ip_address": "10.0.0.50", "prefix": 24, "ip_address_type": "ipv4"}
                ],
            }
        ],
        plugin_unavailable=True,
    )

    assert interfaces == [{"id": 66, "name": "net0", "virtual_machine": 55}]
    assert first_ip_id == 77
    assert [c for c in calls if c["kind"] == "reconcile" and c["path"] == "/api/ipam/ip-addresses/"]
    assert [
        c
        for c in calls
        if c["kind"] == "plugin_get" and c["path"] == "/api/plugins/proxbox/guest-vm-interfaces/"
    ]
    assert not [
        c
        for c in calls
        if c["kind"] == "reconcile" and str(c.get("path", "")).startswith("/api/plugins/proxbox/")
    ]


def test_guest_plugin_ignored_filter_foreign_record_is_not_patched(monkeypatch):
    calls: list[dict[str, object]] = []

    async def _foreign_first(_nb, path, *, query=None, **kwargs):
        calls.append({"kind": "plugin_get", "path": path, "query": query or {}})
        _ = kwargs
        if path == "/api/plugins/proxbox/guest-vm-interfaces/":
            return {
                "id": 321,
                "virtual_machine": {"id": 999},
                "vm_interface": {"id": 888},
                "name": "ens18",
                "mac_address": "aa:bb:cc:dd:ee:ff",
                "enabled": True,
                "mtu": None,
                "tags": [],
                "custom_fields": {},
            }
        return None

    async def _forbidden_create(*args, **kwargs):
        calls.append({"kind": "plugin_create", "args": args, "kwargs": kwargs})
        raise AssertionError("foreign guest row must not be bypassed with create")

    async def _forbidden_patch(*args, **kwargs):
        calls.append({"kind": "plugin_patch", "args": args, "kwargs": kwargs})
        raise AssertionError("foreign guest row must not be patched")

    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_first_async",
        _foreign_first,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_create_async",
        _forbidden_create,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_patch_async",
        _forbidden_patch,
    )

    result = asyncio.run(
        reconcile_guest_vm_interfaces(
            nb=SimpleNamespace(),
            vm_id=55,
            guest_interfaces=[
                {
                    "name": "ens18",
                    "mac_address": "aa:bb:cc:dd:ee:ff",
                    "ip_addresses": [
                        {"ip_address": "10.0.0.50", "prefix": 24, "ip_address_type": "ipv4"}
                    ],
                }
            ],
            core_interface_id_by_mac={"aa:bb:cc:dd:ee:ff": 66},
            ip_ids_by_interface_id={66: {"10.0.0.50/24": 77}},
            tag_refs=[{"name": "Proxbox", "slug": "proxbox"}],
            strategy="guest_os_model",
        )
    )

    assert result == []
    assert [call["kind"] for call in calls] == ["plugin_get"]


def _iter_registered_routes(routes, prefix: str = ""):
    for route in routes:
        include_context = getattr(route, "include_context", None)
        original_router = getattr(route, "original_router", None)
        if include_context is not None and original_router is not None:
            yield from _iter_registered_routes(
                original_router.routes,
                f"{prefix}{include_context.prefix}",
            )
            continue
        path = getattr(route, "path", None)
        if path is not None:
            yield f"{prefix}{path}", route


def test_registered_stream_routes_expose_strategy_param(test_client):
    paths = {
        "/virtualization/virtual-machines/interfaces/create/stream",
        "/virtualization/virtual-machines/interfaces/ip-address/create/stream",
    }
    registered_routes = list(_iter_registered_routes(test_client.app.routes))
    for path in paths:
        matches = [
            route
            for registered_path, route in registered_routes
            if registered_path == path and "GET" in getattr(route, "methods", set())
        ]
        assert len(matches) == 1, f"{path} should be registered exactly once"
        endpoint = matches[0].endpoint
        assert endpoint.__module__ == "proxbox_api.routes.virtualization.virtual_machines.read_vm"
        param = inspect.signature(endpoint).parameters["vm_interface_sync_strategy"]
        assert getattr(param.default, "default", None) == "guest_os_model"
