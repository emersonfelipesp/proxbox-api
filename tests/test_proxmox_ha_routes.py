"""Unit tests for the read-only Proxmox HA routes (issue #243)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from proxbox_api.routes.proxmox import ha as ha_module
from proxbox_api.routes.proxmox.ha import (
    HaGroupSchema,
    HaResourceSchema,
    HaStatusItemSchema,
    HaSummarySchema,
    ha_group_detail,
    ha_groups,
    ha_resource_by_vm,
    ha_resources,
    ha_status,
    ha_summary,
)


def _make_pxs(name: str = "lab"):
    """Build a minimal ProxmoxSession-like list for the dep injection."""
    return [SimpleNamespace(name=name)]


def _patch_helpers(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status_rows=None,
    resources_list=None,
    resource_detail_map=None,
    groups_list=None,
    group_detail_map=None,
    status_error: Exception | None = None,
    resources_error: Exception | None = None,
    groups_error: Exception | None = None,
) -> None:
    """Replace the helper functions used by the HA router with deterministic stubs."""

    async def fake_status(_session):
        if status_error is not None:
            raise status_error
        return list(status_rows or [])

    async def fake_resources(_session, sid: str | None = None):
        if resources_error is not None:
            raise resources_error
        if sid is None:
            return list(resources_list or [])
        if resource_detail_map and sid in resource_detail_map:
            return SimpleNamespace(model_dump=lambda **_kwargs: dict(resource_detail_map[sid]))
        raise RuntimeError(f"resource not found: {sid}")

    async def fake_groups(_session, group: str | None = None):
        if groups_error is not None:
            raise groups_error
        if group is None:
            return list(groups_list or [])
        if group_detail_map and group in group_detail_map:
            return dict(group_detail_map[group])
        raise RuntimeError(f"group not found: {group}")

    monkeypatch.setattr(ha_module, "get_ha_status_current", fake_status)
    monkeypatch.setattr(ha_module, "get_ha_resources", fake_resources)
    monkeypatch.setattr(ha_module, "get_ha_groups", fake_groups)


def test_ha_status_aggregates_per_cluster_rows(monkeypatch: pytest.MonkeyPatch):
    _patch_helpers(
        monkeypatch,
        status_rows=[
            {
                "type": "service",
                "sid": "vm:100",
                "node": "pve01",
                "state": "started",
                "crm_state": "started",
                "request_state": "started",
                "status": "started",
                "max_relocate": 1,
                "max_restart": 1,
            },
            {"type": "quorum", "quorate": 1},
        ],
    )

    rows = asyncio.run(ha_status(_make_pxs()))

    assert isinstance(rows, list)
    assert all(isinstance(r, HaStatusItemSchema) for r in rows)
    sid_row = next(r for r in rows if r.sid == "vm:100")
    assert sid_row.cluster_name == "lab"
    assert sid_row.node == "pve01"
    assert sid_row.crm_state == "started"
    assert sid_row.max_restart == 1
    quorum_row = next(r for r in rows if r.type == "quorum")
    assert quorum_row.quorate is True


def test_ha_status_records_error_row_when_helper_raises(monkeypatch: pytest.MonkeyPatch):
    _patch_helpers(monkeypatch, status_error=RuntimeError("boom"))

    rows = asyncio.run(ha_status(_make_pxs("east")))

    assert len(rows) == 1
    assert rows[0].cluster_name == "east"
    assert rows[0].status is not None and "error" in rows[0].status


def test_ha_resources_merges_runtime_state(monkeypatch: pytest.MonkeyPatch):
    _patch_helpers(
        monkeypatch,
        status_rows=[
            {
                "type": "service",
                "sid": "vm:100",
                "node": "pve02",
                "crm_state": "started",
                "request_state": "started",
                "status": "started",
            },
        ],
        resources_list=[{"sid": "vm:100"}],
        resource_detail_map={
            "vm:100": {
                "sid": "vm:100",
                "type": "vm",
                "state": "started",
                "group": "ha-group-a",
                "max_relocate": 2,
                "max_restart": 1,
                "failback": True,
                "digest": "abc",
            },
        },
    )

    rows = asyncio.run(ha_resources(_make_pxs()))

    assert len(rows) == 1
    assert isinstance(rows[0], HaResourceSchema)
    assert rows[0].sid == "vm:100"
    assert rows[0].group == "ha-group-a"
    # Live runtime state merged from /status/current
    assert rows[0].node == "pve02"
    assert rows[0].crm_state == "started"
    assert rows[0].request_state == "started"


def test_ha_resource_by_vm_returns_null_for_unmanaged_vm(monkeypatch: pytest.MonkeyPatch):
    """The route must return null (not 404) so the NetBox tab can render an empty state."""

    _patch_helpers(
        monkeypatch,
        status_rows=[],
        resources_list=[],
        resource_detail_map={},
    )

    result = asyncio.run(ha_resource_by_vm(_make_pxs(), 999))
    assert result is None


def test_ha_resource_by_vm_falls_back_from_vm_to_ct(monkeypatch: pytest.MonkeyPatch):
    """When `vm:NNN` is missing, the route should retry with `ct:NNN`."""

    _patch_helpers(
        monkeypatch,
        status_rows=[
            {
                "type": "service",
                "sid": "ct:200",
                "node": "pve03",
                "crm_state": "started",
                "status": "started",
            },
        ],
        resource_detail_map={
            "ct:200": {
                "sid": "ct:200",
                "type": "ct",
                "state": "started",
                "group": "ha-ct",
                "digest": "xyz",
            },
        },
    )

    result = asyncio.run(ha_resource_by_vm(_make_pxs(), 200))

    assert result is not None
    assert result.sid == "ct:200"
    assert result.type == "ct"
    assert result.node == "pve03"


def test_ha_groups_lists_with_detail_merge(monkeypatch: pytest.MonkeyPatch):
    _patch_helpers(
        monkeypatch,
        groups_list=[{"group": "ha-group-a"}],
        group_detail_map={
            "ha-group-a": {
                "group": "ha-group-a",
                "nodes": "pve01:1,pve02:2",
                "restricted": 1,
                "nofailback": 0,
                "type": "group",
            },
        },
    )

    rows = asyncio.run(ha_groups(_make_pxs()))

    assert len(rows) == 1
    assert isinstance(rows[0], HaGroupSchema)
    assert rows[0].group == "ha-group-a"
    assert rows[0].nodes == "pve01:1,pve02:2"
    assert rows[0].restricted is True
    assert rows[0].nofailback is False


def test_ha_group_detail_returns_null_when_missing(monkeypatch: pytest.MonkeyPatch):
    _patch_helpers(monkeypatch, group_detail_map={})

    result = asyncio.run(ha_group_detail(_make_pxs(), "missing"))

    assert result is None


def test_ha_summary_runs_subqueries_in_parallel(monkeypatch: pytest.MonkeyPatch):
    _patch_helpers(
        monkeypatch,
        status_rows=[
            {"type": "service", "sid": "vm:100", "node": "pve01", "crm_state": "started"},
        ],
        resources_list=[{"sid": "vm:100"}],
        resource_detail_map={
            "vm:100": {"sid": "vm:100", "type": "vm", "state": "started", "digest": "d"}
        },
        groups_list=[{"group": "g1"}],
        group_detail_map={"g1": {"group": "g1", "nodes": "pve01"}},
    )

    summary = asyncio.run(ha_summary(_make_pxs()))

    assert isinstance(summary, HaSummarySchema)
    assert len(summary.status) == 1
    assert len(summary.resources) == 1
    assert summary.resources[0].sid == "vm:100"
    assert len(summary.groups) == 1
    assert summary.groups[0].group == "g1"


def test_router_is_registered_under_proxmox_cluster_prefix():
    """Sanity-check that the app factory wires the router with the right prefix."""

    from proxbox_api.app.factory import create_app

    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/proxmox/cluster/ha/status" in paths
    assert "/proxmox/cluster/ha/resources" in paths
    assert "/proxmox/cluster/ha/resources/by-vm/{vmid}" in paths
    assert "/proxmox/cluster/ha/groups" in paths
    assert "/proxmox/cluster/ha/groups/{group}" in paths
    assert "/proxmox/cluster/ha/summary" in paths
