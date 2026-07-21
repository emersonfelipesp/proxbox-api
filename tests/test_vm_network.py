"""Tests for VM network helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from proxbox_api.exception import ProxboxException
from proxbox_api.services.sync.individual import interface_sync
from proxbox_api.services.sync.individual.interface_sync import sync_interface_individual
from proxbox_api.services.sync.network import normalize_vm_interface_name
from proxbox_api.services.sync.vm_network import ensure_ip_assigned_to_vm, set_primary_ip


def test_normalize_vm_interface_name_strips_control_characters():
    assert normalize_vm_interface_name("en\x00s\n18\t") == "ens18"


def test_normalize_vm_interface_name_uses_fallback_when_only_control_characters():
    assert normalize_vm_interface_name("\x00\n\t", fallback="net9") == "net9"


def test_set_primary_ip_retries_with_disk_aggregate_on_validation_error(monkeypatch):
    patch_payloads: list[dict[str, object]] = []

    async def _fake_ensure_ip_assigned_to_vm(*args, **kwargs):
        return True, "already_assigned"

    async def _fake_rest_first(nb, path, query=None):
        if path == "/api/ipam/ip-addresses/":
            return {"id": 77, "address": "10.0.0.20/24"}
        return None

    async def _fake_rest_patch(nb, path, record_id, payload):
        patch_payloads.append(payload)
        if payload == {"primary_ip4": 77}:
            raise ProxboxException(
                message="NetBox REST request failed",
                detail=(
                    '{"disk":["The specified disk size (747) must match the aggregate size '
                    'of assigned virtual disks (2114283)."]}'
                ),
            )
        return {"id": 55, "primary_ip4": 77}

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.ensure_ip_assigned_to_vm",
        _fake_ensure_ip_assigned_to_vm,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.rest_first_async",
        _fake_rest_first,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.rest_patch_async",
        _fake_rest_patch,
    )

    updated = asyncio.run(
        set_primary_ip(
            nb=object(),
            virtual_machine={"id": 55, "name": "vm-55", "primary_ip4": None, "primary_ip6": None},
            primary_ip_id=77,
        )
    )

    assert updated is True
    assert patch_payloads == [
        {"primary_ip4": 77},
        {"disk": 2114283, "primary_ip4": 77},
    ]


def test_set_primary_ip_uses_primary_ip6_for_ipv6_addresses(monkeypatch):
    patch_payloads: list[dict[str, object]] = []

    async def _fake_ensure_ip_assigned_to_vm(*args, **kwargs):
        return True, "already_assigned"

    async def _fake_rest_first(nb, path, query=None):
        if path == "/api/ipam/ip-addresses/":
            return {"id": 90, "address": "2804:2cac:1030::10/64"}
        return None

    async def _fake_rest_patch(nb, path, record_id, payload):
        patch_payloads.append(payload)
        return {"id": 55, "primary_ip6": 90}

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.ensure_ip_assigned_to_vm",
        _fake_ensure_ip_assigned_to_vm,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.rest_first_async",
        _fake_rest_first,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.rest_patch_async",
        _fake_rest_patch,
    )

    updated = asyncio.run(
        set_primary_ip(
            nb=object(),
            virtual_machine={"id": 55, "name": "vm-55", "primary_ip4": None, "primary_ip6": None},
            primary_ip_id=90,
        )
    )

    assert updated is True
    assert patch_payloads == [{"primary_ip6": 90}]


def test_set_primary_ip_sets_ipv6_when_ipv4_already_set(monkeypatch):
    """IPv6 primary must be set even when primary_ip4 is already populated.

    Before the fix, the guard returned False when either primary field was set,
    so dual-stack VMs could never get primary_ip6 assigned.
    """
    patch_payloads: list[dict[str, object]] = []

    async def _fake_ensure_ip_assigned_to_vm(*args, **kwargs):
        return True, "already_assigned"

    async def _fake_rest_first(nb, path, query=None):
        if path == "/api/ipam/ip-addresses/":
            return {"id": 90, "address": "2804:2cac:1030::10/64"}
        return None

    async def _fake_rest_patch(nb, path, record_id, payload):
        patch_payloads.append(payload)
        return {"id": 55, "primary_ip4": 77, "primary_ip6": 90}

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.ensure_ip_assigned_to_vm",
        _fake_ensure_ip_assigned_to_vm,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.rest_first_async",
        _fake_rest_first,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.rest_patch_async",
        _fake_rest_patch,
    )

    updated = asyncio.run(
        set_primary_ip(
            nb=object(),
            virtual_machine={
                "id": 55,
                "name": "vm-55",
                "primary_ip4": {"id": 77, "address": "10.0.0.1/24"},  # already set
                "primary_ip6": None,
            },
            primary_ip_id=90,
        )
    )

    assert updated is True
    assert patch_payloads == [{"primary_ip6": 90}]


def test_set_primary_ip_skips_ipv6_when_already_set(monkeypatch):
    """Must not overwrite an already-designated primary_ip6."""
    patch_payloads: list[dict[str, object]] = []

    async def _fake_ensure_ip_assigned_to_vm(*args, **kwargs):
        return True, "already_assigned"

    async def _fake_rest_first(nb, path, query=None):
        if path == "/api/ipam/ip-addresses/":
            return {"id": 91, "address": "2804:2cac:1030::20/64"}
        return None

    async def _fake_rest_patch(nb, path, record_id, payload):  # pragma: no cover
        patch_payloads.append(payload)
        return {}

    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.ensure_ip_assigned_to_vm",
        _fake_ensure_ip_assigned_to_vm,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.rest_first_async",
        _fake_rest_first,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.vm_network.rest_patch_async",
        _fake_rest_patch,
    )

    updated = asyncio.run(
        set_primary_ip(
            nb=object(),
            virtual_machine={
                "id": 55,
                "name": "vm-55",
                "primary_ip4": None,
                "primary_ip6": {"id": 90, "address": "2804:2cac:1030::10/64"},  # already set
            },
            primary_ip_id=91,
        )
    )

    assert updated is False
    assert patch_payloads == []


def test_ensure_ip_assigned_does_not_steal_foreign_ip(monkeypatch):
    """An IP already assigned to another object must never be reassigned.

    Regression for "Virtual Server interface wrongly matched to another
    server's IP": the IP belongs to interface id=888 (a different server) and
    must be left untouched, returning a refusal reason instead of stealing it.
    """
    patch_calls: list[tuple] = []

    async def _fake_rest_first(nb, path, query=None):
        if path == "/api/ipam/ip-addresses/":
            return {
                "id": 77,
                "address": "10.0.0.50/24",
                "assigned_object_type": "virtualization.vminterface",
                "assigned_object_id": 888,  # belongs to another server's interface
            }
        return None

    async def _fake_rest_list(nb, path, query=None):
        if path == "/api/virtualization/interfaces/":
            return [{"id": 111}]  # this VM's interface
        return []

    async def _fake_rest_patch(nb, path, record_id, payload):  # pragma: no cover
        patch_calls.append((path, record_id, payload))
        return {}

    monkeypatch.setattr("proxbox_api.services.sync.vm_network.rest_first_async", _fake_rest_first)
    monkeypatch.setattr("proxbox_api.services.sync.vm_network.rest_list_async", _fake_rest_list)
    monkeypatch.setattr("proxbox_api.services.sync.vm_network.rest_patch_async", _fake_rest_patch)

    assigned, reason = asyncio.run(ensure_ip_assigned_to_vm(nb=object(), ip_id=77, vm_id=55))

    assert assigned is False
    assert reason == "assigned_to_other_object"
    assert patch_calls == []  # the foreign IP was never reassigned


def test_ensure_ip_assigned_adopts_unassigned_ip(monkeypatch):
    """An unassigned IP is safely adopted onto the VM's first interface."""
    patch_calls: list[tuple] = []

    async def _fake_rest_first(nb, path, query=None):
        if path == "/api/ipam/ip-addresses/":
            return {
                "id": 77,
                "address": "10.0.0.50/24",
                "assigned_object_type": None,
                "assigned_object_id": None,
            }
        return None

    async def _fake_rest_list(nb, path, query=None):
        if path == "/api/virtualization/interfaces/":
            return [{"id": 111}]
        return []

    async def _fake_rest_patch(nb, path, record_id, payload):
        patch_calls.append((path, record_id, payload))
        return {
            "id": record_id,
            "assigned_object_type": "virtualization.vminterface",
            "assigned_object_id": 111,
        }

    monkeypatch.setattr("proxbox_api.services.sync.vm_network.rest_first_async", _fake_rest_first)
    monkeypatch.setattr("proxbox_api.services.sync.vm_network.rest_list_async", _fake_rest_list)
    monkeypatch.setattr("proxbox_api.services.sync.vm_network.rest_patch_async", _fake_rest_patch)

    assigned, reason = asyncio.run(ensure_ip_assigned_to_vm(nb=object(), ip_id=77, vm_id=55))

    assert assigned is True
    assert reason == "assigned"
    assert patch_calls == [
        (
            "/api/ipam/ip-addresses/",
            77,
            {
                "assigned_object_type": "virtualization.vminterface",
                "assigned_object_id": 111,
            },
        )
    ]


def test_ensure_ip_assigned_already_on_this_vm(monkeypatch):
    """An IP already assigned to this VM's interface is left unchanged."""
    patch_calls: list[tuple] = []

    async def _fake_rest_first(nb, path, query=None):
        if path == "/api/ipam/ip-addresses/":
            return {
                "id": 77,
                "address": "10.0.0.50/24",
                "assigned_object_type": "virtualization.vminterface",
                "assigned_object_id": 111,
            }
        return None

    async def _fake_rest_list(nb, path, query=None):
        if path == "/api/virtualization/interfaces/":
            return [{"id": 111}]
        return []

    async def _fake_rest_patch(nb, path, record_id, payload):  # pragma: no cover
        patch_calls.append((path, record_id, payload))
        return {}

    monkeypatch.setattr("proxbox_api.services.sync.vm_network.rest_first_async", _fake_rest_first)
    monkeypatch.setattr("proxbox_api.services.sync.vm_network.rest_list_async", _fake_rest_list)
    monkeypatch.setattr("proxbox_api.services.sync.vm_network.rest_patch_async", _fake_rest_patch)

    assigned, reason = asyncio.run(ensure_ip_assigned_to_vm(nb=object(), ip_id=77, vm_id=55))

    assert assigned is True
    assert reason == "already_assigned"
    assert patch_calls == []


def test_sync_interface_individual_mirrors_bridge_sidecar(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.services.custom_fields.get_plugin_bool",
        lambda *, settings_key, default=False: (
            True if settings_key == "custom_fields_enabled" else default
        ),
    )
    sidecar_calls: list[dict[str, object]] = []

    async def _fake_get_vm_config(*_args, **_kwargs):
        return {"net0": "bridge=vmbr0"}

    def _fake_guest_interfaces(*_args, **_kwargs):
        return []

    async def _fake_ensure_vm_record(*_args, **_kwargs):
        return {"id": 55, "name": "vm01"}, None

    async def _fake_rest_first(_nb, path, *, query=None):
        if path == "/api/dcim/devices/":
            return {"id": 12, "name": query["name"]}
        return None

    async def _fake_ensure_bridge(*_args, **_kwargs):
        return 400

    async def _fake_get_first_record(*_args, **_kwargs):
        return None

    async def _fake_reconcile(_nb, path, lookup, payload, **_kwargs):
        assert path == "/api/virtualization/interfaces/"
        assert lookup == {"name": "net0", "virtual_machine_id": 55}
        assert payload["custom_fields"]["proxbox_bridge"] == 400
        return {"id": 66, **payload}

    async def _fake_sidecar(_nb, **kwargs):
        sidecar_calls.append(kwargs)

    monkeypatch.setattr(interface_sync, "get_vm_config_individual", _fake_get_vm_config)
    monkeypatch.setattr(
        interface_sync,
        "get_qemu_guest_agent_network_interfaces",
        _fake_guest_interfaces,
    )
    monkeypatch.setattr(interface_sync, "ensure_vm_record", _fake_ensure_vm_record)
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_first_async", _fake_rest_first)
    monkeypatch.setattr(
        "proxbox_api.services.sync.bridge_interfaces.ensure_bridge_interfaces",
        _fake_ensure_bridge,
    )
    monkeypatch.setattr(interface_sync, "get_first_record", _fake_get_first_record)
    monkeypatch.setattr(interface_sync, "rest_reconcile_async", _fake_reconcile)
    monkeypatch.setattr(interface_sync, "write_vm_interface_sync_state", _fake_sidecar)

    result = asyncio.run(
        sync_interface_individual(
            nb=object(),
            px=SimpleNamespace(name="lab"),
            tag=SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
            node="pve01",
            vm_type="qemu",
            vmid=101,
            interface_name="net0",
        )
    )

    assert result["action"] == "created"
    assert sidecar_calls == [
        {
            "vm_interface_id": 66,
            "proxbox_bridge_id": 400,
            "overwrite_custom_fields": True,
        }
    ]
