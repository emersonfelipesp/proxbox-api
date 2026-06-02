"""Ceph v2 control-plane (`/ceph/v2/*`) API tests.

Covers the issue #95 acceptance matrix: plan build/inspect, validation,
apply with destructive-confirmation gating, capability-blocked operations,
operation tracking, idempotent re-apply, reconcile, metrics, and the SSE
progress event shape. A fake provider adapter is injected so the suite is
deterministic and does not require a live Proxmox/Ceph cluster.
"""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api.ceph.v2_schemas import ProviderCapabilities
from proxbox_api.main import app
from proxbox_api.session.proxmox_providers import proxmox_sessions_dep


class _FakeCephAdapter:
    """In-memory adapter advertising write capability for deterministic tests."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="proxmox",
            supported=True,
            read_state=True,
            diff=True,
            plan=True,
            apply=True,
            reconcile=True,
            metrics=True,
            destructive_operations=True,
            operation_kinds={
                "pool:ensure": True,
                "pool:delete": True,
                "rgw_bucket:delete": False,
            },
        )

    async def read_state(self, scope: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        return {"summary": {}}

    async def diff(self, desired: Any, live: Any) -> list[Any]:  # noqa: ARG002
        return []

    async def plan(self, operations: list[Any]) -> list[Any]:
        return operations

    async def apply(self, operation: Any, *, confirm_destructive: bool) -> dict[str, Any]:  # noqa: ARG002
        return {
            "operation_id": operation.id,
            "result": "applied",
            "task_ref": f"task:{operation.target_ref}",
        }

    async def reconcile(self, scope: dict[str, Any]) -> dict[str, Any]:
        return {"result": "reconciled", "scope": scope, "summary": {}}

    async def metrics(self, scope: dict[str, Any]) -> dict[str, Any]:
        return {"scope": scope, "pgs": 128}


@pytest.fixture
def ceph_v2_client(auth_test_client, monkeypatch):
    """Authenticated client with the fake adapter and empty Proxmox sessions."""

    monkeypatch.setattr(
        "proxbox_api.ceph.v2_routes.adapter_for_provider",
        lambda *_a, **_k: _FakeCephAdapter(),
    )
    app.dependency_overrides[proxmox_sessions_dep] = lambda: []
    yield auth_test_client
    app.dependency_overrides.pop(proxmox_sessions_dep, None)


def _ensure_plan_body() -> dict[str, Any]:
    return {
        "provider": "proxmox",
        "operations": [{"kind": "pool", "target_ref": "rbd", "action": "ensure"}],
        "netbox_branch_schema_id": "branch-123",
    }


def _delete_plan_body() -> dict[str, Any]:
    return {
        "provider": "proxmox",
        "operations": [{"kind": "pool", "target_ref": "rbd", "action": "delete"}],
    }


def test_capabilities_lists_providers(auth_test_client):
    app.dependency_overrides[proxmox_sessions_dep] = lambda: []
    try:
        resp = auth_test_client.get("/ceph/v2/capabilities")
    finally:
        app.dependency_overrides.pop(proxmox_sessions_dep, None)
    assert resp.status_code == 200
    providers = {p["provider"] for p in resp.json()["providers"]}
    assert "proxmox" in providers


def test_validate_reports_errors_for_bad_payload(ceph_v2_client):
    resp = ceph_v2_client.post("/ceph/v2/validate", json={"objects": [{"kind": ""}]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert any(r["severity"] == "error" for r in body["results"])


def test_plan_build_get_and_missing(ceph_v2_client):
    created = ceph_v2_client.post("/ceph/v2/plans", json=_ensure_plan_body())
    assert created.status_code == 200, created.text
    plan = created.json()
    assert plan["id"]
    assert plan["operations"], "plan must contain the requested operation"

    fetched = ceph_v2_client.get(f"/ceph/v2/plans/{plan['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == plan["id"]

    missing = ceph_v2_client.get("/ceph/v2/plans/does-not-exist")
    assert missing.status_code == 404


def test_apply_happy_path_and_operation_lookup(ceph_v2_client):
    plan = ceph_v2_client.post("/ceph/v2/plans", json=_ensure_plan_body()).json()
    applied = ceph_v2_client.post(
        f"/ceph/v2/plans/{plan['id']}/apply", json={"confirm_destructive": False}
    )
    assert applied.status_code == 200, applied.text
    run = applied.json()
    assert run["status"] == "completed"
    assert run["source_branch_schema_id"] == "branch-123"

    looked_up = ceph_v2_client.get(f"/ceph/v2/operations/{run['id']}")
    assert looked_up.status_code == 200
    assert looked_up.json()["status"] == "completed"


def test_apply_destructive_requires_confirmation(ceph_v2_client):
    plan = ceph_v2_client.post("/ceph/v2/plans", json=_delete_plan_body()).json()

    rejected = ceph_v2_client.post(
        f"/ceph/v2/plans/{plan['id']}/apply", json={"confirm_destructive": False}
    )
    assert rejected.status_code == 409, rejected.text

    confirmed = ceph_v2_client.post(
        f"/ceph/v2/plans/{plan['id']}/apply", json={"confirm_destructive": True}
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "completed"


def test_apply_blocks_unsupported_capability(ceph_v2_client):
    body = {
        "provider": "proxmox",
        "operations": [{"kind": "rgw_bucket", "target_ref": "b1", "action": "delete"}],
    }
    plan = ceph_v2_client.post("/ceph/v2/plans", json=body).json()
    blocked_ops = [op for op in plan["operations"] if not op["supported"]]
    assert blocked_ops and blocked_ops[0]["blocked_reason"]

    applied = ceph_v2_client.post(
        f"/ceph/v2/plans/{plan['id']}/apply", json={"confirm_destructive": True}
    )
    assert applied.status_code == 409, applied.text


def test_apply_is_idempotent(ceph_v2_client):
    plan = ceph_v2_client.post("/ceph/v2/plans", json=_ensure_plan_body()).json()
    first = ceph_v2_client.post(f"/ceph/v2/plans/{plan['id']}/apply", json={}).json()
    second = ceph_v2_client.post(f"/ceph/v2/plans/{plan['id']}/apply", json={}).json()
    assert first["id"] == second["id"]
    assert second["status"] == "completed"


def test_operation_events_sse_shape(ceph_v2_client):
    plan = ceph_v2_client.post("/ceph/v2/plans", json=_ensure_plan_body()).json()
    run = ceph_v2_client.post(f"/ceph/v2/plans/{plan['id']}/apply", json={}).json()

    resp = ceph_v2_client.get(f"/ceph/v2/operations/{run['id']}/events")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "data:" in resp.text
    assert run["id"] in resp.text


def test_operation_missing_returns_404(ceph_v2_client):
    resp = ceph_v2_client.get("/ceph/v2/operations/no-such-run")
    assert resp.status_code == 404


def test_reconcile_returns_operation_run(ceph_v2_client):
    resp = ceph_v2_client.post("/ceph/v2/reconcile", json={"provider": "proxmox", "scope": {}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["provider"] == "proxmox"


def test_metrics_returns_payload(ceph_v2_client):
    resp = ceph_v2_client.get("/ceph/v2/metrics", params={"provider": "proxmox"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "proxmox"
    assert "pgs" in body["metrics"]
