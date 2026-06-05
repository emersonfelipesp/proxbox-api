"""Regression tests: VM interface IP sync must not match another server's IP.

These cover the defect where a VM interface was "wrongly matched to another
server's IP". The per-interface IP reconcile previously looked an address up
globally and PATCHed its assignment, stealing an address that belonged to a
different object. The fixed behavior resolves ownership first: reuse an IP
already on this interface, adopt an unassigned IP, or create a new one scoped
to this interface -- but never reassign a foreign-owned address.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from proxbox_api.services.sync.network import _resolve_vm_interface_ips

_NOW = datetime(2026, 6, 5, tzinfo=timezone.utc)
_ADDR = "10.0.0.50/24"


def _run_resolve(monkeypatch, existing_for_address):
    """Drive _resolve_vm_interface_ips with a fake NetBox and capture reconcile calls."""
    reconcile_calls: list[dict] = []

    async def _fake_rest_list(nb, path, query=None):
        query = query or {}
        # Address ownership lookup performed by _reconcile_interface_ip.
        if query.get("address") == _ADDR and "vminterface_id" not in query and "tag" not in query:
            return list(existing_for_address)
        # cleanup_stale_ips_for_interface (vminterface_id + tag) -> nothing stale.
        return []

    async def _fake_rest_reconcile(nb, path, *, lookup, payload, **kwargs):
        reconcile_calls.append({"lookup": lookup, "payload": payload, "kwargs": kwargs})
        # Echo a record id: reuse the looked-up id when present, else a new one.
        rec_id = lookup.get("id", 123)
        return {"id": rec_id, **payload}

    monkeypatch.setattr("proxbox_api.services.sync.network.rest_list_async", _fake_rest_list)
    monkeypatch.setattr(
        "proxbox_api.services.sync.network.rest_reconcile_async", _fake_rest_reconcile
    )

    results = asyncio.run(
        _resolve_vm_interface_ips(
            nb=object(),
            interface_config={"ip": _ADDR},
            guest_iface=None,
            tag_refs=[],
            interface_id=111,
            interface_name="eth0",
            now=_NOW,
            create_ip=True,
        )
    )
    return results, reconcile_calls


def test_resolve_does_not_reassign_foreign_ip(monkeypatch):
    """An address owned by another interface (id=888) must not be reassigned."""
    foreign = {
        "id": 999,
        "address": _ADDR,
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": 888,
    }
    results, reconcile_calls = _run_resolve(monkeypatch, [foreign])

    # The foreign record must never be the reconcile target.
    assert all(call["lookup"].get("id") != 999 for call in reconcile_calls)
    # The old global address-only reconcile (the steal pattern) must not be used.
    assert {"address": _ADDR} not in [call["lookup"] for call in reconcile_calls]
    # Exactly one reconcile: an interface-scoped create for this interface.
    assert len(reconcile_calls) == 1
    create_call = reconcile_calls[0]
    assert create_call["lookup"] == {"address": _ADDR, "vminterface_id": 111}
    assert create_call["kwargs"].get("strict_lookup") is True
    assert create_call["payload"]["assigned_object_id"] == 111
    # The interface still gets an IP id back (its own record, id=123).
    assert results == [(123, _ADDR)]


def test_resolve_adopts_unassigned_ip(monkeypatch):
    """An unassigned address (e.g. pre-created in a prefix) is adopted by id."""
    unassigned = {
        "id": 500,
        "address": _ADDR,
        "assigned_object_type": None,
        "assigned_object_id": None,
    }
    results, reconcile_calls = _run_resolve(monkeypatch, [unassigned])

    assert len(reconcile_calls) == 1
    call = reconcile_calls[0]
    assert call["lookup"] == {"id": 500}
    assert call["kwargs"].get("strict_lookup") is True
    assert call["payload"]["assigned_object_id"] == 111
    assert results == [(500, _ADDR)]


def test_resolve_reuses_own_ip(monkeypatch):
    """An address already on this interface is reconciled in place (idempotent)."""
    own = {
        "id": 600,
        "address": _ADDR,
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": 111,
    }
    results, reconcile_calls = _run_resolve(monkeypatch, [own])

    assert len(reconcile_calls) == 1
    call = reconcile_calls[0]
    assert call["lookup"] == {"id": 600}
    assert call["payload"]["assigned_object_id"] == 111
    assert results == [(600, _ADDR)]


def test_resolve_creates_when_no_existing_record(monkeypatch):
    """No existing address -> create a new record scoped to this interface."""
    results, reconcile_calls = _run_resolve(monkeypatch, [])

    assert len(reconcile_calls) == 1
    call = reconcile_calls[0]
    assert call["lookup"] == {"address": _ADDR, "vminterface_id": 111}
    assert call["kwargs"].get("strict_lookup") is True
    assert results == [(123, _ADDR)]
