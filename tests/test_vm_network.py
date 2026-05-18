"""Tests for VM network helpers."""

from __future__ import annotations

import asyncio

from proxbox_api.exception import ProxboxException
from proxbox_api.services.sync.vm_network import set_primary_ip


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
