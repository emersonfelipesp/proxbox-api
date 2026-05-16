"""Unit tests for the read-only Proxmox HA routes (issue #243, #111)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from proxmox_sdk.sdk.exceptions import ResourceException

from proxbox_api.routes.proxmox import ha as ha_module
from proxbox_api.routes.proxmox.ha import (
    HaGroupSchema,
    HaResourceSchema,
    HaRuleSchema,
    HaStatusItemSchema,
    HaSummarySchema,
    ha_group_detail,
    ha_groups,
    ha_resource_by_vm,
    ha_resources,
    ha_rules,
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
    rules_list=None,
    rule_detail_map=None,
    status_error: Exception | None = None,
    resources_error: Exception | None = None,
    groups_error: Exception | None = None,
    rules_error: Exception | None = None,
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

    async def fake_rules(_session, rule: str | None = None):
        if rules_error is not None:
            raise rules_error
        if rule is None:
            return list(rules_list or [])
        if rule_detail_map and rule in rule_detail_map:
            return dict(rule_detail_map[rule])
        raise RuntimeError(f"rule not found: {rule}")

    monkeypatch.setattr(ha_module, "get_ha_status_current", fake_status)
    monkeypatch.setattr(ha_module, "get_ha_resources", fake_resources)
    monkeypatch.setattr(ha_module, "get_ha_groups", fake_groups)
    monkeypatch.setattr(ha_module, "get_ha_rules", fake_rules)


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


def test_get_ha_groups_degrades_gracefully_on_pve9_500():
    """get_ha_groups helper must return [] instead of raising when PVE 9.x sends HTTP 500."""
    from proxbox_api.services import proxmox_helpers

    pve9_exc = ResourceException(
        status_code=500,
        status_message="cannot index groups: ha groups have been migrated to rules",
    )

    class _Resource:
        async def get(self):
            raise pve9_exc

    class _SdkClient:
        def __call__(self, _path):
            return _Resource()

    class _FakeSession:
        session = _SdkClient()

    # _dual_mode handles sync→async dispatch; call directly without asyncio.run
    result = proxmox_helpers.get_ha_groups(_FakeSession())
    assert result == []


def test_ha_rules_lists_with_detail_merge(monkeypatch: pytest.MonkeyPatch):
    """ha_rules must aggregate rule rows and enrich them with per-rule detail."""

    _patch_helpers(
        monkeypatch,
        rules_list=[{"rule": "rule-affinity-1"}],
        rule_detail_map={
            "rule-affinity-1": {
                "rule": "rule-affinity-1",
                "type": "node-affinity",
                "affinity": "positive",
                "nodes": "pve01:1,pve02:2",
                "resources": "vm:100,vm:101",
                "strict": 1,
                "disable": 0,
            },
        },
    )

    rows = asyncio.run(ha_rules(_make_pxs()))

    assert len(rows) == 1
    assert isinstance(rows[0], HaRuleSchema)
    assert rows[0].rule == "rule-affinity-1"
    assert rows[0].type == "node-affinity"
    assert rows[0].affinity == "positive"
    assert rows[0].nodes == "pve01:1,pve02:2"
    assert rows[0].resources == "vm:100,vm:101"
    assert rows[0].strict is True
    assert rows[0].disable is False


def test_ha_summary_includes_rules(monkeypatch: pytest.MonkeyPatch):
    """ha_summary must include a rules list alongside groups and resources."""

    _patch_helpers(
        monkeypatch,
        status_rows=[],
        resources_list=[],
        resource_detail_map={},
        groups_list=[],
        group_detail_map={},
        rules_list=[{"rule": "r1"}],
        rule_detail_map={"r1": {"rule": "r1", "type": "resource-affinity", "resources": "vm:200"}},
    )

    summary = asyncio.run(ha_summary(_make_pxs()))

    assert isinstance(summary, HaSummarySchema)
    assert hasattr(summary, "rules")
    assert len(summary.rules) == 1
    assert summary.rules[0].rule == "r1"
    assert summary.rules[0].type == "resource-affinity"


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
    assert "/proxmox/cluster/ha/rules" in paths
    assert "/proxmox/cluster/ha/summary" in paths
