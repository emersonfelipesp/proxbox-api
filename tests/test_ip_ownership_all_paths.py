"""Regression tests for IP ownership safety across sync paths."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from proxbox_api.services.sync.individual.ip_sync import sync_ip_individual
from proxbox_api.services.sync.network import (
    bulk_reconcile_vm_interface_ips,
    sync_node_interface_and_ip,
)

_NOW = datetime(2026, 6, 6, tzinfo=timezone.utc)
_ADDR = "10.0.0.50/24"


class _Record:
    def __init__(self, **data: Any) -> None:
        self._data = dict(data)
        self.id = data.get("id")

    def serialize(self) -> dict[str, Any]:
        return dict(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)


def _foreign_vm_ip() -> dict[str, Any]:
    return {
        "id": 900,
        "address": _ADDR,
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": 888,
        "status": "active",
        "dns_name": "",
        "tags": [],
    }


def _own_vm_ip() -> dict[str, Any]:
    return {
        "id": 600,
        "address": _ADDR,
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": 111,
        "status": "active",
        "dns_name": "",
        "tags": [],
    }


def _unassigned_ip(record_id: int = 500) -> dict[str, Any]:
    return {
        "id": record_id,
        "address": _ADDR,
        "assigned_object_type": None,
        "assigned_object_id": None,
        "status": "active",
        "dns_name": "",
        "tags": [],
    }


def _foreign_dcim_ip() -> dict[str, Any]:
    return {
        "id": 901,
        "address": _ADDR,
        "assigned_object_type": "dcim.interface",
        "assigned_object_id": 777,
        "status": "active",
        "dns_name": "",
        "tags": [],
    }


def _own_dcim_ip() -> dict[str, Any]:
    return {
        "id": 601,
        "address": _ADDR,
        "assigned_object_type": "dcim.interface",
        "assigned_object_id": 222,
        "status": "active",
        "dns_name": "",
        "tags": [],
    }


@pytest.mark.asyncio
async def test_bulk_vm_interface_ips_foreign_address_creates_scoped_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A foreign VM-interface IP must not suppress creation for this interface."""
    created: list[dict[str, Any]] = []
    patched: list[dict[str, Any]] = []
    captured_base_query: list[dict[str, object] | None] = []

    async def _fake_list_paginated(_nb: object, _path: str, *, base_query=None, **_kwargs):
        captured_base_query.append(base_query)
        return [_Record(**_foreign_vm_ip())]

    async def _fake_bulk_create(_nb: object, _path: str, entries: list[dict[str, Any]]):
        created.extend(entries)
        return [_Record(id=700 + index, **entry) for index, entry in enumerate(entries)]

    async def _fake_bulk_patch(_nb: object, _path: str, entries: list[dict[str, Any]]):
        patched.extend(entries)
        return [_Record(**entry) for entry in entries]

    monkeypatch.setattr(
        "proxbox_api.netbox_rest.rest_list_paginated_async",
        _fake_list_paginated,
    )
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_bulk_create_async", _fake_bulk_create)
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_bulk_patch_async", _fake_bulk_patch)

    records = await bulk_reconcile_vm_interface_ips(
        nb=object(),
        ip_payloads=[
            {
                "address": _ADDR,
                "assigned_object_type": "virtualization.vminterface",
                "assigned_object_id": 111,
                "status": "active",
                "dns_name": "",
                "tags": [],
            }
        ],
    )

    assert captured_base_query == [{"assigned_object_type": "virtualization.vminterface"}]
    assert patched == []
    assert len(created) == 1
    assert created[0]["address"] == _ADDR
    assert created[0]["assigned_object_id"] == 111
    assert records[0].id == 700


@pytest.mark.asyncio
async def test_bulk_vm_interface_ips_reuses_own_record(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, Any]] = []
    patched: list[dict[str, Any]] = []

    async def _fake_list_paginated(_nb: object, _path: str, *, base_query=None, **_kwargs):
        assert base_query == {"assigned_object_type": "virtualization.vminterface"}
        return [_Record(**_own_vm_ip())]

    async def _fake_bulk_create(_nb: object, _path: str, entries: list[dict[str, Any]]):
        created.extend(entries)
        return []

    async def _fake_bulk_patch(_nb: object, _path: str, entries: list[dict[str, Any]]):
        patched.extend(entries)
        return []

    monkeypatch.setattr(
        "proxbox_api.netbox_rest.rest_list_paginated_async",
        _fake_list_paginated,
    )
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_bulk_create_async", _fake_bulk_create)
    monkeypatch.setattr("proxbox_api.netbox_rest.rest_bulk_patch_async", _fake_bulk_patch)

    records = await bulk_reconcile_vm_interface_ips(
        nb=object(),
        ip_payloads=[
            {
                "address": _ADDR,
                "assigned_object_type": "virtualization.vminterface",
                "assigned_object_id": 111,
                "status": "active",
                "dns_name": "",
                "tags": [],
            }
        ],
    )

    assert created == []
    assert patched == []
    assert records[0].id == 600


async def _run_individual_ip_path(
    monkeypatch: pytest.MonkeyPatch,
    existing_for_address: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    reconcile_calls: list[dict[str, Any]] = []

    async def _fake_ensure_vm_record(*_args: Any, **_kwargs: Any):
        return {"id": 55, "name": "vm01"}, None

    async def _fake_resolve_interface_id(*_args: Any, **_kwargs: Any) -> int:
        return 111

    async def _fake_rest_list(_nb: object, path: str, query=None):
        if path == "/api/ipam/ip-addresses/":
            return list(existing_for_address)
        return []

    async def _fake_reconcile(_nb: object, _path: str, *, lookup, payload, **kwargs):
        reconcile_calls.append({"lookup": lookup, "payload": payload, "kwargs": kwargs})
        record_id = lookup.get("id", 701)
        return _Record(id=record_id, **payload)

    async def _fake_first(_nb: object, _path: str, query=None):
        record_id = (query or {}).get("id")
        return _Record(
            id=record_id,
            address=_ADDR,
            assigned_object_type="virtualization.vminterface",
            assigned_object_id=111,
            status="active",
            dns_name="",
            tags=[],
        )

    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.ip_sync.ensure_vm_record",
        _fake_ensure_vm_record,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.ip_sync._resolve_interface_id",
        _fake_resolve_interface_id,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.ip_sync._resolve_dns_name",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.ip_sync.rest_list_async",
        _fake_rest_list,
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.individual.ip_sync.rest_first_async",
        _fake_first,
    )
    monkeypatch.setattr("proxbox_api.services.sync.ip_ownership.rest_list_async", _fake_rest_list)
    monkeypatch.setattr(
        "proxbox_api.services.sync.ip_ownership.rest_reconcile_async",
        _fake_reconcile,
    )

    result = await sync_ip_individual(
        nb=object(),
        px=SimpleNamespace(name="lab"),
        tag=SimpleNamespace(id=7, name="Proxbox", slug="proxbox", color="ff5722"),
        node="pve01",
        vm_type="lxc",
        vmid=101,
        ip_address=_ADDR,
        interface_name="eth0",
    )

    assert result["error"] is None
    return reconcile_calls


@pytest.mark.asyncio
async def test_individual_ip_foreign_address_creates_scoped_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = await _run_individual_ip_path(monkeypatch, [_foreign_vm_ip()])

    assert len(calls) == 1
    assert calls[0]["lookup"] == {"address": _ADDR, "vminterface_id": 111}
    assert calls[0]["kwargs"].get("strict_lookup") is True
    assert calls[0]["payload"]["assigned_object_id"] == 111


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing", "expected_lookup"),
    [
        (_own_vm_ip(), {"id": 600}),
        (_unassigned_ip(), {"id": 500}),
    ],
)
async def test_individual_ip_reuses_own_and_adopts_unassigned(
    monkeypatch: pytest.MonkeyPatch,
    existing: dict[str, Any],
    expected_lookup: dict[str, int],
) -> None:
    calls = await _run_individual_ip_path(monkeypatch, [existing])

    assert len(calls) == 1
    assert calls[0]["lookup"] == expected_lookup
    assert calls[0]["payload"]["assigned_object_id"] == 111


async def _run_node_ip_path(
    monkeypatch: pytest.MonkeyPatch,
    existing_for_address: list[dict[str, Any]],
) -> tuple[dict, list[dict[str, Any]]]:
    reconcile_calls: list[dict[str, Any]] = []

    async def _fake_network_reconcile(_nb: object, path: str, *, lookup, payload, **_kwargs):
        if path == "/api/dcim/interfaces/":
            return {"id": 222, **payload}
        raise AssertionError(f"unexpected network reconcile path: {path}")

    async def _fake_rest_list(_nb: object, path: str, query=None):
        if path == "/api/ipam/ip-addresses/":
            return list(existing_for_address)
        return []

    async def _fake_ip_reconcile(_nb: object, _path: str, *, lookup, payload, **kwargs):
        reconcile_calls.append({"lookup": lookup, "payload": payload, "kwargs": kwargs})
        record_id = lookup.get("id", 702)
        return _Record(id=record_id, **payload)

    monkeypatch.setattr(
        "proxbox_api.services.sync.network.rest_reconcile_async",
        _fake_network_reconcile,
    )
    monkeypatch.setattr("proxbox_api.services.sync.ip_ownership.rest_list_async", _fake_rest_list)
    monkeypatch.setattr(
        "proxbox_api.services.sync.ip_ownership.rest_reconcile_async",
        _fake_ip_reconcile,
    )

    result = await sync_node_interface_and_ip(
        nb=object(),
        device={"id": 44},
        interface_name="vmbr0",
        interface_config={"cidr": _ADDR, "type": "bridge"},
        tag_refs=[],
    )
    return result, reconcile_calls


@pytest.mark.asyncio
async def test_node_interface_ip_foreign_address_creates_scoped_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, calls = await _run_node_ip_path(monkeypatch, [_foreign_dcim_ip()])

    assert result["ip_id"] == 702
    assert len(calls) == 1
    assert calls[0]["lookup"] == {"address": _ADDR, "interface_id": 222}
    assert calls[0]["kwargs"].get("strict_lookup") is True
    assert calls[0]["payload"]["assigned_object_type"] == "dcim.interface"
    assert calls[0]["payload"]["assigned_object_id"] == 222


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing", "expected_lookup"),
    [
        (_own_dcim_ip(), {"id": 601}),
        (_unassigned_ip(502), {"id": 502}),
    ],
)
async def test_node_interface_ip_reuses_own_and_adopts_unassigned(
    monkeypatch: pytest.MonkeyPatch,
    existing: dict[str, Any],
    expected_lookup: dict[str, int],
) -> None:
    result, calls = await _run_node_ip_path(monkeypatch, [existing])

    assert result["ip_id"] == expected_lookup["id"]
    assert len(calls) == 1
    assert calls[0]["lookup"] == expected_lookup
    assert calls[0]["payload"]["assigned_object_type"] == "dcim.interface"
    assert calls[0]["payload"]["assigned_object_id"] == 222
