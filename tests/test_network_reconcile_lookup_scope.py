"""Regression tests: bulk reconcile must scope existence checks by id.

NetBox relation filters differ in what they match:
- ``virtual_machine`` (VMInterface) matches by VM *name*.
- ``site`` / ``tenant`` (VLAN) match by *slug*.

proxbox carries the NetBox *id* in those payload fields, so the lookup query
must be remapped to the id-based filter (``*_id``) via ``lookup_query_field_map``.
Without it NetBox silently ignores the unscoped filter and the reconcile fails:
for VM interfaces it re-creates existing rows (HTTP 400 "already exists"); for
VLANs it can match the wrong VLAN across sites/tenants.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from proxbox_api.services.sync import network


def _capture_reconcile_kwargs(monkeypatch) -> dict:
    captured: dict = {}

    async def _fake_bulk_reconcile(_nb, _path, *, payloads, **kwargs):
        captured["path"] = _path
        captured["payloads"] = payloads
        captured.update(kwargs)
        return SimpleNamespace(records=[], created=0, updated=0, unchanged=0, failed=0)

    monkeypatch.setattr(network, "rest_bulk_reconcile_async", _fake_bulk_reconcile)
    return captured


async def test_vm_interface_reconcile_scopes_lookup_by_virtual_machine_id(monkeypatch):
    captured = _capture_reconcile_kwargs(monkeypatch)

    await network.bulk_reconcile_vm_interfaces(
        MagicMock(), [{"name": "ens18", "virtual_machine": 8}]
    )

    assert captured["path"] == "/api/virtualization/interfaces/"
    assert captured["lookup_fields"] == ["name", "virtual_machine"]
    assert captured["lookup_query_field_map"] == {"virtual_machine": "virtual_machine_id"}


async def test_vlan_reconcile_scopes_lookup_by_site_and_tenant_id(monkeypatch):
    captured = _capture_reconcile_kwargs(monkeypatch)

    await network.bulk_reconcile_vlans(MagicMock(), [{"vid": 200, "site": 3, "tenant": 5}])

    assert captured["path"] == "/api/ipam/vlans/"
    assert captured["lookup_fields"] == ["vid", "site", "tenant"]
    assert captured["lookup_query_field_map"] == {"site": "site_id", "tenant": "tenant_id"}
