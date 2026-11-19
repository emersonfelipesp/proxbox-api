"""Regression tests: VLAN reconciles are create-only.

Proxmox only knows a VLAN by its VID. When an interface carries a VID that
already exists in NetBox, the sync must *link* to that VLAN, never rewrite it:
before the fix, every existing VLAN matched by VID was renamed to
``VLAN <vid>``, forced to ``active`` and had its tags replaced by the Proxbox
tag, because the VLAN reconciles passed no ``patchable_fields``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from proxbox_api import netbox_rest
from proxbox_api.proxmox_to_netbox.models import NETBOX_VLAN_PATCHABLE_FIELDS
from proxbox_api.services.sync import network

ROOT = Path(__file__).parent.parent
_VLAN_PATH = "/api/ipam/vlans/"
_PROXBOX_TAG: list[dict[str, object]] = [
    {"id": 5, "name": "Proxbox", "slug": "proxbox", "color": "ff5722"},
]


class _FakeRecord:
    """Existing NetBox record: tracks PATCH traffic through ``save()``."""

    def __init__(self, payload: dict[str, Any], record_id: int) -> None:
        object.__setattr__(self, "_payload", dict(payload))
        object.__setattr__(self, "id", record_id)
        object.__setattr__(self, "save_calls", 0)
        object.__setattr__(self, "patched_fields", {})

    def serialize(self) -> dict[str, Any]:
        return {**self._payload, "id": self.id}

    def get(self, key: str, default: Any = None) -> Any:
        return self._payload.get(key, default)

    async def save(self) -> None:
        object.__setattr__(self, "save_calls", self.save_calls + 1)

    def __setattr__(self, key: str, value: Any) -> None:
        if key in {"_payload", "id", "save_calls", "patched_fields"}:
            object.__setattr__(self, key, value)
            return
        self.patched_fields[key] = value


@pytest.fixture
def fake_netbox(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Fake the REST seams used by ``rest_reconcile_async``."""
    state: dict[str, Any] = {"existing": None, "posts": []}

    async def _fake_first(_nb: object, _path: str, *, query: dict[str, Any]) -> Any:
        del query
        return state["existing"]

    async def _fake_create(_nb: object, _path: str, payload: dict[str, Any], **_kw: Any) -> Any:
        state["posts"].append(dict(payload))
        return _FakeRecord(payload, record_id=99)

    monkeypatch.setattr(netbox_rest, "rest_first_async", _fake_first)
    monkeypatch.setattr(netbox_rest, "rest_create_async", _fake_create)
    return state


def _node_vlan_entry(vid: int) -> dict[str, Any]:
    return {"type": "vlan", "vlan-id": str(vid), "vlan-raw-device": "bond0"}


def test_vlan_patchable_fields_is_empty() -> None:
    assert NETBOX_VLAN_PATCHABLE_FIELDS == frozenset()


async def test_existing_vlan_is_linked_without_being_modified(
    fake_netbox: dict[str, Any],
) -> None:
    existing = _FakeRecord(
        {"vid": 198, "name": "DEV_COMMON", "status": "reserved", "tags": []},
        record_id=15,
    )
    fake_netbox["existing"] = existing
    failures: list[dict] = []

    vlan_id = await network._sync_node_interface_vlan(
        object(),
        {"id": 1, "name": "node1"},
        _node_vlan_entry(198),
        "bond0.198",
        _PROXBOX_TAG,
        failures,
    )

    assert vlan_id == 15
    assert existing.save_calls == 0
    assert existing.patched_fields == {}
    assert fake_netbox["posts"] == []
    assert failures == []


async def test_missing_vlan_is_still_created(fake_netbox: dict[str, Any]) -> None:
    failures: list[dict] = []

    vlan_id = await network._sync_node_interface_vlan(
        object(),
        {"id": 1, "name": "node1"},
        _node_vlan_entry(1842),
        "bond0.1842",
        _PROXBOX_TAG,
        failures,
    )

    assert vlan_id == 99
    assert len(fake_netbox["posts"]) == 1
    created = fake_netbox["posts"][0]
    assert created["vid"] == 1842
    assert created["name"] == "VLAN 1842"
    assert failures == []


def _vlan_reconcile_calls() -> list[tuple[str, int, ast.Call]]:
    calls: list[tuple[str, int, ast.Call]] = []
    for path in sorted((ROOT / "proxbox_api").rglob("*.py")):
        if "generated" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name not in {"rest_reconcile_async", "rest_bulk_reconcile_async"}:
                continue
            if len(node.args) < 2:
                continue
            target = node.args[1]
            if isinstance(target, ast.Constant) and target.value == _VLAN_PATH:
                calls.append((str(path.relative_to(ROOT)), node.lineno, node))
    return calls


def test_every_vlan_reconcile_is_create_only() -> None:
    """Guard future call sites: any VLAN reconcile must pass the create-only set."""
    calls = _vlan_reconcile_calls()
    assert calls, "expected VLAN reconcile call sites"
    offenders = []
    for rel_path, lineno, call in calls:
        keywords = {kw.arg: kw.value for kw in call.keywords}
        value = keywords.get("patchable_fields")
        if not (isinstance(value, ast.Name) and value.id == "NETBOX_VLAN_PATCHABLE_FIELDS"):
            offenders.append(f"{rel_path}:{lineno}")
    assert offenders == []
