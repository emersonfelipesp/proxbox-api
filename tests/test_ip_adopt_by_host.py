"""Regression: adopt a pre-existing unassigned IP by host, regardless of mask.

NetBox's `address` filter is exact-CIDR, so `1.2.3.4/24` does not match an
existing `1.2.3.4/32`. With ENFORCE_GLOBAL_UNIQUE the duplicate create then
fails and the address silently never attaches. The adoption lookup must query
by host so an operator-seeded `/32` is adopted onto the interface (and its mask
normalized), while still never reassigning a foreign-owned address.
"""

from types import SimpleNamespace

from proxbox_api.services.sync import ip_ownership


def _mocks(monkeypatch, existing):
    captured = {"list_query": None, "reconcile": []}

    async def fake_list(nb, path, *, query=None):
        captured["list_query"] = query
        return existing

    async def fake_reconcile(nb, path, *, lookup, payload, **kw):
        captured["reconcile"].append({"lookup": lookup, "payload": payload})
        return SimpleNamespace(id=lookup.get("id", 99))

    monkeypatch.setattr(ip_ownership, "rest_list_async", fake_list)
    monkeypatch.setattr(ip_ownership, "rest_reconcile_async", fake_reconcile)
    return captured


async def test_adopts_existing_unassigned_slash32_as_slash24(monkeypatch):
    from datetime import datetime, timezone

    existing = [
        {
            "id": 1,
            "address": "141.94.139.106/32",
            "assigned_object_type": None,
            "assigned_object_id": None,
        }
    ]
    cap = _mocks(monkeypatch, existing)

    result = await ip_ownership._reconcile_interface_ip(
        object(),
        ip_addr="141.94.139.106/24",
        interface_id=5,
        tag_refs=[],
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        dns_name=None,
        interface_name="vmbr0",
        assigned_object_type="dcim.interface",
        interface_lookup_field="interface_id",
    )

    # Lookup is by host (mask stripped) so the /32 is found.
    assert cap["list_query"]["address"] == "141.94.139.106"
    # Adopted by id (not a create-by-address), assigned to this interface, mask -> /24.
    assert len(cap["reconcile"]) == 1
    assert cap["reconcile"][0]["lookup"] == {"id": 1}
    payload = cap["reconcile"][0]["payload"]
    assert payload["address"] == "141.94.139.106/24"
    assert payload["assigned_object_id"] == 5
    assert result == 1


async def test_never_adopts_foreign_owned_address(monkeypatch):
    from datetime import datetime, timezone

    # Same host, but already owned by a different interface -> must NOT be reused.
    existing = [
        {
            "id": 7,
            "address": "141.94.139.106/32",
            "assigned_object_type": "dcim.interface",
            "assigned_object_id": 999,
        }
    ]
    cap = _mocks(monkeypatch, existing)

    await ip_ownership._reconcile_interface_ip(
        object(),
        ip_addr="141.94.139.106/24",
        interface_id=5,
        tag_refs=[],
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        dns_name=None,
        interface_name="vmbr0",
        assigned_object_type="dcim.interface",
        interface_lookup_field="interface_id",
    )

    # Falls through to the interface-scoped create path (never reconciles id=7).
    assert all(c["lookup"] != {"id": 7} for c in cap["reconcile"])
