"""Regression tests for bridge interface synchronization helpers."""

import asyncio
from datetime import datetime, timezone

from proxbox_api.exception import ProxboxException
from proxbox_api.services.sync.bridge_interfaces import ensure_node_bridge_interface


class _FakeRestRecord:
    def __init__(self, data: dict):
        object.__setattr__(self, "_data", dict(data))
        object.__setattr__(self, "_dirty", {})
        object.__setattr__(self, "saved_payloads", [])

    def serialize(self) -> dict:
        return dict(self._data)

    def dict(self) -> dict:
        return dict(self._data)

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def __setattr__(self, name: str, value):
        if name in {"_data", "_dirty", "saved_payloads"}:
            object.__setattr__(self, name, value)
            return
        self._dirty[name] = value
        self._data[name] = value

    async def save(self):
        self.saved_payloads.append(dict(self._dirty))
        self._dirty.clear()
        return self


def test_ensure_node_bridge_interface_uses_strict_lookup_on_create(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    async def _fake_first(_nb, path, *, query=None):
        calls.append(("first", path, dict(query or {})))
        return None

    async def _fake_create(_nb, path, payload):
        calls.append(("create", path, dict(payload)))
        return {"id": 501, **payload}

    async def _unexpected_reconcile(*_args, **_kwargs):
        raise AssertionError("unexpected fallback reconcile")

    monkeypatch.setattr("proxbox_api.services.sync.bridge_interfaces.rest_first_async", _fake_first)
    monkeypatch.setattr("proxbox_api.services.sync.bridge_interfaces.rest_create_async", _fake_create)
    monkeypatch.setattr(
        "proxbox_api.services.sync.bridge_interfaces.rest_reconcile_async",
        _unexpected_reconcile,
    )

    result = asyncio.run(
        ensure_node_bridge_interface(
            nb=object(),
            device_id=19,
            bridge_name="vmbr1",
            tag_refs=[{"slug": "proxbox", "name": "Proxbox"}],
            now=datetime(2026, 4, 9, 20, 0, 0, tzinfo=timezone.utc),
        )
    )

    assert calls[0] == (
        "first",
        "/api/dcim/interfaces/",
        {"device_id": 19, "name": "vmbr1", "limit": 2},
    )
    assert calls[1][0] == "create"
    assert calls[1][2]["device"] == 19
    assert calls[1][2]["name"] == "vmbr1"
    assert result["id"] == 501


def test_ensure_node_bridge_interface_never_patches_device_field(monkeypatch):
    existing = _FakeRestRecord(
        {
            "id": 88,
            "device": {"id": 19, "name": "pve05"},
            "name": "vmbr1",
            "type": {"value": "other", "label": "Other"},
            "status": {"value": "planned", "label": "Planned"},
            "tags": [],
            "custom_fields": {},
        }
    )

    async def _fake_first(_nb, _path, *, query=None):
        assert query == {"device_id": 19, "name": "vmbr1", "limit": 2}
        return existing

    async def _unexpected_create(*_args, **_kwargs):
        raise AssertionError("create should not be called when strict lookup finds interface")

    monkeypatch.setattr("proxbox_api.services.sync.bridge_interfaces.rest_first_async", _fake_first)
    monkeypatch.setattr(
        "proxbox_api.services.sync.bridge_interfaces.rest_create_async",
        _unexpected_create,
    )

    result = asyncio.run(
        ensure_node_bridge_interface(
            nb=object(),
            device_id=19,
            bridge_name="vmbr1",
            tag_refs=[{"slug": "proxbox", "name": "Proxbox"}],
            now=datetime(2026, 4, 9, 20, 0, 0, tzinfo=timezone.utc),
        )
    )

    assert result["id"] == 88
    assert existing.saved_payloads
    assert all("device" not in patch for patch in existing.saved_payloads)


def test_ensure_node_bridge_interface_refetches_strict_after_create_error(monkeypatch):
    calls = {"first": 0, "create": 0}
    existing = _FakeRestRecord(
        {
            "id": 91,
            "device": {"id": 19, "name": "pve05"},
            "name": "vmbr1",
            "type": {"value": "bridge", "label": "Bridge"},
            "status": {"value": "active", "label": "Active"},
            "tags": [{"slug": "proxbox", "name": "Proxbox"}],
            "custom_fields": {},
        }
    )

    async def _fake_first(_nb, _path, *, query=None):
        calls["first"] += 1
        assert query == {"device_id": 19, "name": "vmbr1", "limit": 2}
        return None if calls["first"] == 1 else existing

    async def _fake_create(_nb, _path, _payload):
        calls["create"] += 1
        raise ProxboxException(
            message="NetBox REST request failed",
            detail={"name": ["An interface with this name already exists."]},
        )

    monkeypatch.setattr("proxbox_api.services.sync.bridge_interfaces.rest_first_async", _fake_first)
    monkeypatch.setattr("proxbox_api.services.sync.bridge_interfaces.rest_create_async", _fake_create)

    result = asyncio.run(
        ensure_node_bridge_interface(
            nb=object(),
            device_id=19,
            bridge_name="vmbr1",
            tag_refs=[{"slug": "proxbox", "name": "Proxbox"}],
            now=datetime(2026, 4, 9, 20, 0, 0, tzinfo=timezone.utc),
        )
    )

    assert calls == {"first": 2, "create": 1}
    assert result["id"] == 91
