"""Regression tests for the proxmox_last_updated object_types union helper.

Issue netbox-proxbox#349: prior to this helper, restarting proxbox-api
PATCH'd the custom field's ``object_types`` back to the hardcoded list,
wiping any entries an operator had added in the NetBox UI. The helper
pre-merges current ∪ desired before reconcile, so the diff is one-sided
(adds entries; never removes).
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from proxbox_api.routes.extras import (
    _coerce_object_type_entry,
    _normalize_current_object_types,
    _union_object_types_with_current,
)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _record(serialized: dict) -> object:
    return SimpleNamespace(serialize=lambda: serialized)


@pytest.fixture
def proxbox_caplog(caplog):
    """Attach caplog to the non-propagating ``proxbox`` logger.

    ``proxbox_api.logger`` sets ``propagate=False`` so caplog's root handler
    never sees its records. We bolt caplog's own handler onto that logger for
    the duration of the test, then detach it cleanly.
    """
    proxbox_logger = logging.getLogger("proxbox")
    proxbox_logger.addHandler(caplog.handler)
    proxbox_logger.setLevel(logging.DEBUG)
    try:
        yield caplog
    finally:
        proxbox_logger.removeHandler(caplog.handler)


def test_coerce_object_type_entry_string():
    assert _coerce_object_type_entry("dcim.device") == "dcim.device"


def test_coerce_object_type_entry_app_label_model_dict():
    assert _coerce_object_type_entry({"app_label": "extras", "model": "tag"}) == "extras.tag"


def test_coerce_object_type_entry_name_dict():
    assert _coerce_object_type_entry({"name": "ipam.vlan"}) == "ipam.vlan"


def test_coerce_object_type_entry_unknown_returns_none():
    assert _coerce_object_type_entry(42) is None
    assert _coerce_object_type_entry({"unrelated": "x"}) is None


def test_normalize_current_object_types_mixed_shapes():
    raw = [
        "dcim.device",
        {"app_label": "extras", "model": "tag"},
        {"name": "ipam.vlan"},
        12345,  # garbage; must be skipped, not raise
    ]
    assert _normalize_current_object_types(raw) == [
        "dcim.device",
        "extras.tag",
        "ipam.vlan",
    ]


def test_normalize_current_object_types_non_list_returns_empty():
    assert _normalize_current_object_types(None) == []
    assert _normalize_current_object_types("dcim.device") == []


def test_union_preserves_operator_additions(monkeypatch, proxbox_caplog):
    """The operator added ``extras.tag`` to the scope manually. The reconcile
    pre-merge must keep it after restart.
    """

    async def _fake_first(_session, _path, query=None):
        assert query == {"name": "proxmox_last_updated", "limit": 2}
        return _record({"object_types": ["virtualization.virtualmachine", "extras.tag"]})

    monkeypatch.setattr("proxbox_api.routes.extras.rest_first_async", _fake_first)

    field: dict[str, object] = {
        "name": "proxmox_last_updated",
        "object_types": ["virtualization.virtualmachine", "dcim.device"],
    }

    proxbox_caplog.set_level(logging.INFO)
    _run(_union_object_types_with_current(object(), field))

    # Desired entries kept first, operator-added entries appended.
    assert field["object_types"] == [
        "virtualization.virtualmachine",
        "dcim.device",
        "extras.tag",
    ]
    assert any("extras.tag" in record.message for record in proxbox_caplog.records)


def test_union_handles_dict_shape_object_types(monkeypatch):
    async def _fake_first(_session, _path, query=None):
        return _record({"object_types": [{"app_label": "extras", "model": "tag"}]})

    monkeypatch.setattr("proxbox_api.routes.extras.rest_first_async", _fake_first)

    field: dict[str, object] = {
        "name": "proxmox_last_updated",
        "object_types": ["virtualization.virtualmachine"],
    }
    _run(_union_object_types_with_current(object(), field))
    assert field["object_types"] == [
        "virtualization.virtualmachine",
        "extras.tag",
    ]


def test_union_skips_when_no_existing_record(monkeypatch):
    async def _fake_first(_session, _path, query=None):
        return None

    monkeypatch.setattr("proxbox_api.routes.extras.rest_first_async", _fake_first)

    desired = ["virtualization.virtualmachine", "dcim.device"]
    field: dict[str, object] = {"name": "proxmox_last_updated", "object_types": list(desired)}
    _run(_union_object_types_with_current(object(), field))
    assert field["object_types"] == desired  # untouched, no shrink


def test_union_logs_warning_when_prefetch_raises(monkeypatch, proxbox_caplog):
    async def _fake_first(_session, _path, query=None):
        raise RuntimeError("netbox unreachable")

    monkeypatch.setattr("proxbox_api.routes.extras.rest_first_async", _fake_first)

    desired = ["virtualization.virtualmachine"]
    field: dict[str, object] = {"name": "proxmox_last_updated", "object_types": list(desired)}

    proxbox_caplog.set_level(logging.WARNING)
    _run(_union_object_types_with_current(object(), field))

    assert field["object_types"] == desired  # never shrunk on error
    assert any(
        "netbox unreachable" in r.message or "Could not pre-fetch" in r.message
        for r in proxbox_caplog.records
    )


def test_union_skips_when_desired_is_not_list(monkeypatch):
    """Defensive: if a malformed custom_field dict has no list ``object_types``
    we must not call rest_first_async or mutate anything.
    """
    called = {"count": 0}

    async def _fake_first(_session, _path, query=None):
        called["count"] += 1
        return None

    monkeypatch.setattr("proxbox_api.routes.extras.rest_first_async", _fake_first)

    field: dict[str, object] = {"name": "proxmox_last_updated", "object_types": None}
    _run(_union_object_types_with_current(object(), field))
    assert called["count"] == 0
    assert field["object_types"] is None
