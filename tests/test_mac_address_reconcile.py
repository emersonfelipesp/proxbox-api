"""Unit tests for the dcim.MACAddress reconciliation helper.

Pins the write-path contract introduced for
https://github.com/emersonfelipesp/netbox-proxbox/issues/359 — the legacy
inline ``mac_address`` field on ``VMInterface`` is read-only at NetBox 4.5/4.6,
so MACs must be written through ``dcim.MACAddress`` and linked via
``VMInterface.primary_mac_address``.
"""

from __future__ import annotations

import asyncio

from proxbox_api.services.sync.mac_address import (
    DCIM_INTERFACE_CONTENT_TYPE,
    VMINTERFACE_CONTENT_TYPE,
    normalize_mac,
    reconcile_mac_for_interface,
    reconcile_mac_for_vm_interface,
)


def test_normalize_mac_handles_hyphens_and_lowercase():
    assert normalize_mac("aa-bb-cc-dd-ee-ff") == "AA:BB:CC:DD:EE:FF"
    assert normalize_mac("AA:BB:CC:DD:EE:FF") == "AA:BB:CC:DD:EE:FF"
    assert normalize_mac("  aa:bb:cc:dd:ee:ff  ") == "AA:BB:CC:DD:EE:FF"


def test_normalize_mac_returns_none_for_empty():
    assert normalize_mac(None) is None
    assert normalize_mac("") is None


def test_content_type_constants_match_netbox_lowercase_dotted():
    # The dcim.MACAddress GFK only accepts lowercase-dotted content-type values.
    assert VMINTERFACE_CONTENT_TYPE == "virtualization.vminterface"
    assert DCIM_INTERFACE_CONTENT_TYPE == "dcim.interface"


def test_reconcile_skipped_when_mac_is_none():
    result = asyncio.run(
        reconcile_mac_for_vm_interface(
            nb=object(),
            vminterface_id=42,
            mac=None,
        )
    )
    assert result == (None, "skipped")


def test_reconcile_skipped_when_interface_id_falsy(monkeypatch):
    async def _unexpected(*_args, **_kwargs):
        raise AssertionError("rest_reconcile_async should not be called")

    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.rest_reconcile_async",
        _unexpected,
    )

    result = asyncio.run(
        reconcile_mac_for_vm_interface(
            nb=object(),
            vminterface_id=0,
            mac="AA:BB:CC:DD:EE:FF",
        )
    )
    assert result == (None, "skipped")


def test_reconcile_unchanged_when_primary_mac_already_set(monkeypatch):
    reconcile_calls: list[dict] = []
    patch_calls: list[tuple] = []

    async def _fake_reconcile(_nb, path, *, lookup, payload, schema, current_normalizer, **kwargs):
        reconcile_calls.append(
            {
                "path": path,
                "lookup": lookup,
                "payload": payload,
                "strict_lookup": kwargs.get("strict_lookup"),
            }
        )
        return {"id": 777, **payload}

    async def _fake_first(_nb, _path, *, query=None):
        return {"id": query["id"], "primary_mac_address": {"id": 777}}

    async def _fake_patch(_nb, *_args, **_kwargs):
        patch_calls.append((_args, _kwargs))
        raise AssertionError("PATCH should be skipped when FK already matches")

    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.rest_reconcile_async", _fake_reconcile
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.rest_first_async", _fake_first
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.rest_patch_async", _fake_patch
    )

    mac_id, status = asyncio.run(
        reconcile_mac_for_vm_interface(
            nb=object(),
            vminterface_id=42,
            mac="aa:bb:cc:dd:ee:ff",
        )
    )

    assert mac_id == 777
    assert status == "unchanged"
    assert patch_calls == []
    # MAC-row upsert went to dcim.MACAddress with the right GFK + canonical MAC.
    call = reconcile_calls[0]
    assert call["path"] == "/api/dcim/mac-addresses/"
    assert call["payload"]["mac_address"] == "AA:BB:CC:DD:EE:FF"
    assert call["payload"]["assigned_object_type"] == "virtualization.vminterface"
    assert call["payload"]["assigned_object_id"] == 42
    assert call["strict_lookup"] is True
    assert call["lookup"] == {
        "mac_address": "AA:BB:CC:DD:EE:FF",
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": 42,
    }


def test_reconcile_patches_primary_mac_address_when_fk_differs(monkeypatch):
    async def _fake_reconcile(_nb, _path, *, lookup, payload, schema, current_normalizer, **kwargs):
        return {"id": 555, **payload}

    async def _fake_first(_nb, path, *, query=None):
        assert path == "/api/virtualization/interfaces/"
        return {"id": query["id"], "primary_mac_address": {"id": 111}}

    patch_calls: list[tuple] = []

    async def _fake_patch(_nb, path, record_id, body):
        patch_calls.append((path, record_id, body))
        return {"id": record_id, **body}

    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.rest_reconcile_async", _fake_reconcile
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.rest_first_async", _fake_first
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.rest_patch_async", _fake_patch
    )

    mac_id, status = asyncio.run(
        reconcile_mac_for_vm_interface(
            nb=object(),
            vminterface_id=42,
            mac="AA:BB:CC:DD:EE:FF",
        )
    )

    assert mac_id == 555
    assert status == "updated"
    # FK direction pin: PATCH target is VMInterface, body key is primary_mac_address,
    # value is the integer MAC row id (NOT a nested dict).
    assert patch_calls == [
        (
            "/api/virtualization/interfaces/",
            42,
            {"primary_mac_address": 555},
        )
    ]


def test_reconcile_patches_when_no_primary_mac_address_set(monkeypatch):
    async def _fake_reconcile(_nb, _path, *, lookup, payload, schema, current_normalizer, **kwargs):
        return {"id": 999, **payload}

    async def _fake_first(_nb, _path, *, query=None):
        return {"id": query["id"], "primary_mac_address": None}

    patched: list[tuple] = []

    async def _fake_patch(_nb, path, record_id, body):
        patched.append((path, record_id, body))
        return {"id": record_id, **body}

    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.rest_reconcile_async", _fake_reconcile
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.rest_first_async", _fake_first
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.rest_patch_async", _fake_patch
    )

    mac_id, status = asyncio.run(
        reconcile_mac_for_vm_interface(
            nb=object(),
            vminterface_id=7,
            mac="aa:bb:cc:dd:ee:ff",
        )
    )

    assert (mac_id, status) == (999, "updated")
    assert patched == [
        ("/api/virtualization/interfaces/", 7, {"primary_mac_address": 999})
    ]


def test_reconcile_for_dcim_interface_uses_dcim_path(monkeypatch):
    reconcile_calls: list[dict] = []

    async def _fake_reconcile(_nb, path, *, lookup, payload, schema, current_normalizer, **kwargs):
        reconcile_calls.append({"path": path, "payload": payload, "lookup": lookup})
        return {"id": 33, **payload}

    async def _fake_first(_nb, path, *, query=None):
        # FK already matches → no PATCH needed.
        return {"id": query["id"], "primary_mac_address": {"id": 33}}

    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.rest_reconcile_async", _fake_reconcile
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.rest_first_async", _fake_first
    )

    mac_id, status = asyncio.run(
        reconcile_mac_for_interface(
            nb=object(),
            mac="AA:BB:CC:DD:EE:FF",
            assigned_object_type=DCIM_INTERFACE_CONTENT_TYPE,
            assigned_object_id=12,
            interface_list_path="/api/dcim/interfaces/",
        )
    )

    assert (mac_id, status) == (33, "unchanged")
    assert reconcile_calls[0]["path"] == "/api/dcim/mac-addresses/"
    assert reconcile_calls[0]["payload"]["assigned_object_type"] == "dcim.interface"
    assert reconcile_calls[0]["payload"]["assigned_object_id"] == 12


def test_current_normalizer_canonicalizes_netbox_mac_casing(monkeypatch):
    """Second-run-silent acceptance: NetBox may serialize MACs in lowercase or
    with hyphens. The drift comparison must canonicalize both sides so a
    stable sync emits zero ObjectChange rows.
    """
    captured: dict = {}

    async def _fake_reconcile(_nb, _path, *, lookup, payload, schema, current_normalizer, **kwargs):
        # Simulate NetBox returning the existing row with a non-canonical MAC.
        existing = {
            "id": 42,
            "mac_address": "aa-bb-cc-dd-ee-ff",
            "assigned_object_type": {"app_label": "virtualization", "model": "vminterface"},
            "assigned_object_id": {"id": 7},
            "tags": [],
            "custom_fields": {},
        }
        captured["current"] = current_normalizer(existing)
        captured["payload"] = payload
        return existing

    async def _fake_first(_nb, _path, *, query=None):
        return {"id": query["id"], "primary_mac_address": {"id": 42}}

    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.rest_reconcile_async", _fake_reconcile
    )
    monkeypatch.setattr(
        "proxbox_api.services.sync.mac_address.rest_first_async", _fake_first
    )

    mac_id, status = asyncio.run(
        reconcile_mac_for_vm_interface(
            nb=object(),
            vminterface_id=7,
            mac="AA:BB:CC:DD:EE:FF",
        )
    )

    assert (mac_id, status) == (42, "unchanged")
    # Drift comparison must see equal MACs after normalization.
    assert captured["current"]["mac_address"] == captured["payload"]["mac_address"]
    assert captured["current"]["mac_address"] == "AA:BB:CC:DD:EE:FF"
    # GFK content-type also canonicalized from the nested dict form.
    assert captured["current"]["assigned_object_type"] == "virtualization.vminterface"
    assert captured["current"]["assigned_object_id"] == 7
