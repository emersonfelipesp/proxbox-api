"""Node-network interface reconciles must stay scoped to their own node device.

Every Proxmox node has interfaces with the same names (``vmbr0``, ``vmbr1``,
``eno1``...). The node-network sync looks an interface up by ``device_id`` +
``name``; without ``strict_lookup`` the reconcile also tries the looser
``{"name": ...}`` candidate, finds the same-named interface of another node and
PATCHes its ``device``, which NetBox rejects with "Components cannot be moved to
a different device." The second node's network stage then fails as a whole.

These tests drive the real ``rest_reconcile_async`` against a fake NetBox that
applies the ``device_id`` / ``name`` filters like NetBox does.
"""

import json
import re
from types import SimpleNamespace

import pytest
from netbox_sdk.client import ApiResponse

from proxbox_api.netbox_rest import clear_rest_get_cache
from proxbox_api.services.sync import network

FIRST_NODE = 1
SECOND_NODE = 2


class _InterfaceNetBox:
    """Fake /api/dcim/interfaces/ with NetBox's filter and component-move rules."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = [dict(row) for row in rows]
        self.posts: list[dict] = []
        self.patches: list[tuple[int, dict]] = []

    @staticmethod
    def _serialize(row: dict) -> dict:
        return {**row, "type": {"value": row["type"]}, "status": {"value": "active"}}

    async def request(self, method, path, *, query=None, payload=None, expect_json=True):
        del expect_json
        query = query or {}
        if method == "GET" and path.rstrip("/").endswith("/api/dcim/interfaces"):
            rows = self.rows
            if "device_id" in query:
                rows = [r for r in rows if r["device"] == int(query["device_id"])]
            if "name" in query:
                rows = [r for r in rows if r["name"] == query["name"]]
            body = {
                "count": len(rows),
                "next": None,
                "previous": None,
                "results": [self._serialize(r) for r in rows],
            }
            return ApiResponse(status=200, text=json.dumps(body))
        if method == "POST":
            row = {"id": 100 + len(self.rows), "enabled": True, "tags": [], **payload}
            self.rows.append(row)
            self.posts.append(dict(payload))
            return ApiResponse(status=201, text=json.dumps(self._serialize(row)))
        match = re.search(r"/api/dcim/interfaces/(\d+)/?$", path)
        if method == "PATCH" and match:
            row = next(r for r in self.rows if r["id"] == int(match.group(1)))
            self.patches.append((row["id"], dict(payload)))
            if "device" in payload and payload["device"] != row["device"]:
                body = {"device": ["Components cannot be moved to a different device."]}
                return ApiResponse(status=400, text=json.dumps(body))
            row.update(payload)
            return ApiResponse(status=200, text=json.dumps(self._serialize(row)))
        raise AssertionError(f"unexpected request: {method} {path} {query}")


@pytest.fixture(autouse=True)
def _reset_rest_cache():
    clear_rest_get_cache()
    yield
    clear_rest_get_cache()


def _first_node_rows() -> list[dict]:
    return [
        {
            "id": 2,
            "device": FIRST_NODE,
            "name": "vmbr0",
            "type": "bridge",
            "enabled": True,
            "tags": [],
        },
        {
            "id": 3,
            "device": FIRST_NODE,
            "name": "eno1",
            "type": "other",
            "enabled": True,
            "tags": [],
        },
    ]


async def test_scalar_reconcile_creates_on_second_node_instead_of_moving_first_nodes_interface():
    netbox = _InterfaceNetBox(_first_node_rows())
    session = SimpleNamespace(client=netbox)

    record = await network._reconcile_node_interface_scalar(
        session,
        device={"id": SECOND_NODE, "name": "pve-b"},
        entry={"iface": "vmbr0", "type": "bridge", "active": 1},
        tag_refs=[],
    )

    assert [p["device"] for p in netbox.posts] == [SECOND_NODE]
    assert not any("device" in patch for _, patch in netbox.patches)
    assert record.get("device") == SECOND_NODE
    first = next(r for r in netbox.rows if r["id"] == 2)
    assert first["device"] == FIRST_NODE


async def test_topology_reconcile_patches_only_its_own_node_interface():
    rows = _first_node_rows() + [
        {
            "id": 20,
            "device": SECOND_NODE,
            "name": "vmbr0",
            "type": "bridge",
            "enabled": True,
            "tags": [],
        },
        {
            "id": 21,
            "device": SECOND_NODE,
            "name": "eno1",
            "type": "other",
            "enabled": True,
            "tags": [],
        },
    ]
    netbox = _InterfaceNetBox(rows)
    session = SimpleNamespace(client=netbox)
    entries = [
        {"iface": "eno1", "type": "eth", "active": 1},
        {"iface": "vmbr0", "type": "bridge", "active": 1, "bridge_ports": "eno1"},
    ]

    await network._sync_node_network_topology(
        session, SECOND_NODE, entries, {"eno1": 21, "vmbr0": 20}, {}
    )

    assert netbox.posts == []
    assert {row_id for row_id, _ in netbox.patches} <= {20, 21}
    assert next(r for r in netbox.rows if r["id"] == 21).get("bridge") == 20
