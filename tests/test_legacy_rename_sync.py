"""Regression coverage for deprecated legacy VM interface renaming."""

from __future__ import annotations

import asyncio

from proxbox_api.services.sync.guest_vm_interface import reconcile_guest_vm_interfaces
from proxbox_api.services.sync.network import _resolve_vm_interface_identity
from proxbox_api.services.sync.vm_filter import (
    get_interface_name_from_config_and_agent,
    parse_network_config,
)
from proxbox_api.services.sync.vm_network import sync_vm_interfaces


def _install_legacy_rename_patches(monkeypatch, calls: list[dict[str, object]]) -> None:
    async def _fake_rest_reconcile(_nb, path, *, lookup, payload, **kwargs):
        _ = kwargs
        calls.append(
            {
                "kind": "reconcile",
                "path": path,
                "lookup": lookup,
                "payload": payload,
            }
        )
        if path == "/api/virtualization/interfaces/":
            return {
                "id": 66,
                "name": payload.get("name"),
                "virtual_machine": payload.get("virtual_machine"),
            }
        if path == "/api/ipam/ip-addresses/":
            return {
                "id": 77,
                "address": payload.get("address"),
                "assigned_object_type": payload.get("assigned_object_type"),
                "assigned_object_id": payload.get("assigned_object_id"),
            }
        return {"id": 999, **payload}

    async def _fake_rest_list(*args, **kwargs):
        calls.append(
            {
                "kind": "list",
                "path": args[1] if len(args) > 1 else "",
                "query": kwargs.get("query") or {},
            }
        )
        return []

    async def _forbidden_guest_plugin_first(_nb, path, *, query=None, **kwargs):
        _ = kwargs
        calls.append({"kind": "plugin_get", "path": path, "query": query or {}})
        raise AssertionError("legacy_rename must not read guest VM interface plugin rows")

    async def _forbidden_guest_plugin_create(_nb, path, payload, **kwargs):
        _ = kwargs
        calls.append({"kind": "plugin_create", "path": path, "payload": payload})
        raise AssertionError("legacy_rename must not create guest VM interface plugin rows")

    async def _forbidden_guest_plugin_patch(_nb, path, record_id, payload, **kwargs):
        _ = kwargs
        calls.append(
            {
                "kind": "plugin_patch",
                "path": path,
                "record_id": record_id,
                "payload": payload,
            }
        )
        raise AssertionError("legacy_rename must not patch guest VM interface plugin rows")

    async def _fake_reconcile_mac(*args, **kwargs):
        calls.append({"kind": "mac", "kwargs": kwargs})
        return None

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.rest_reconcile_async",
        _fake_rest_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.ip_ownership.rest_reconcile_async",
        _fake_rest_reconcile,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.ip_ownership.rest_list_async",
        _fake_rest_list,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_first_async",
        _forbidden_guest_plugin_first,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_create_async",
        _forbidden_guest_plugin_create,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.rest_patch_async",
        _forbidden_guest_plugin_patch,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.reconcile_mac_for_vm_interface",
        _fake_reconcile_mac,
    )


def test_legacy_rename_keeps_core_rename_ip_resolution_and_skips_guest_plugin(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "proxbox_api.services.custom_fields.get_plugin_bool",
        lambda *, settings_key, default=False: (
            True if settings_key == "custom_fields_enabled" else default
        ),
    )
    calls: list[dict[str, object]] = []
    warnings: list[str] = []
    _install_legacy_rename_patches(monkeypatch, calls)

    def _capture_warning(message, *args, **kwargs):
        _ = kwargs
        warnings.append(str(message) % args if args else str(message))

    monkeypatch.setattr(
        "proxbox_api.services.sync.guest_vm_interface.logger.warning",
        _capture_warning,
    )

    guest_interfaces = [
        {
            "name": "ens18",
            "mac_address": "aa:bb:cc:dd:ee:ff",
            "ip_addresses": [{"ip_address": "10.0.0.50", "prefix": 24, "ip_address_type": "ipv4"}],
        }
    ]
    network_configs = parse_network_config({"net0": "virtio=AA:BB:CC:DD:EE:FF"})
    config_dict = network_configs[0]["net0"]

    assert (
        get_interface_name_from_config_and_agent(
            "net0",
            config_dict,
            guest_interfaces,
            use_guest_agent_name=True,
            vm_interface_sync_strategy="legacy_rename",
        )
        == "ens18"
    )
    resolved_name, _mac_address = _resolve_vm_interface_identity(
        "net0",
        config_dict,
        guest_interfaces[0],
        use_guest_agent_interface_name=True,
        vm_interface_sync_strategy="legacy_rename",
    )
    assert resolved_name == "ens18"

    interfaces, first_ip_id = asyncio.run(
        sync_vm_interfaces(
            nb=object(),
            virtual_machine={"id": 55, "name": "vm01"},
            vm_config={},
            guest_agent_interfaces=guest_interfaces,
            network_configs=network_configs,
            tag_refs=[{"name": "Proxbox", "slug": "proxbox"}],
            use_guest_agent_interface_name=True,
            vm_interface_sync_strategy="legacy_rename",
        )
    )

    assert interfaces == [{"id": 66, "name": "ens18", "virtual_machine": 55}]
    assert first_ip_id == 77

    interface_payloads = [
        call["payload"]
        for call in calls
        if call["kind"] == "reconcile" and call["path"] == "/api/virtualization/interfaces/"
    ]
    ip_payloads = [
        call["payload"]
        for call in calls
        if call["kind"] == "reconcile" and call["path"] == "/api/ipam/ip-addresses/"
    ]
    plugin_calls = [
        call for call in calls if str(call.get("path", "")).startswith("/api/plugins/proxbox/")
    ]

    assert interface_payloads == [
        {
            "virtual_machine": 55,
            "name": "ens18",
            "enabled": True,
            "bridge": None,
            "untagged_vlan": None,
            "mode": None,
            "tags": [{"name": "Proxbox", "slug": "proxbox"}],
            "custom_fields": interface_payloads[0]["custom_fields"],
        }
    ]
    assert ip_payloads == [
        {
            "address": "10.0.0.50/24",
            "assigned_object_type": "virtualization.vminterface",
            "assigned_object_id": 66,
            "status": "active",
            "dns_name": "",
            "tags": [{"name": "Proxbox", "slug": "proxbox"}],
            "custom_fields": ip_payloads[0]["custom_fields"],
        }
    ]
    assert plugin_calls == []
    assert any("vm_interface_sync_strategy=legacy_rename is deprecated" in w for w in warnings)

    legacy_guest_records = asyncio.run(
        reconcile_guest_vm_interfaces(
            nb=object(),
            vm_id=55,
            guest_interfaces=guest_interfaces,
            core_interface_id_by_mac={"AA:BB:CC:DD:EE:FF": 66},
            ip_ids_by_interface_id={66: {"10.0.0.50/24": 77}},
            tag_refs=[{"name": "Proxbox", "slug": "proxbox"}],
            strategy="legacy_rename",
        )
    )

    assert legacy_guest_records == []
    assert [
        call for call in calls if call["kind"] in {"plugin_get", "plugin_create", "plugin_patch"}
    ] == []
