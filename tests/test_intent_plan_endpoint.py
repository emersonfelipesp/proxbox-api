"""Sub-PR D (#381): contract test for ``POST /intent/plan``.

Pins:
  * Route is mounted at ``/intent/plan`` via the FastAPI test client.
  * Empty diff list returns ``permitted=True`` with the no-op summary.
  * CREATE/UPDATE diffs default-permit (the per-op probes ship in F/G/K).
  * DELETE diffs come back as ``warning`` with the
    ``delete_routed_to_deletion_request`` reason, since Sub-PR H sends
    them through the four-eyes DeletionRequest workflow instead of
    Proxmox destroy.

The /intent/plan endpoint is read-only by design, so the
``ProxmoxEndpoint.allow_writes`` gate is intentionally NOT applied here
(Sub-PRs F/G/H/I/K's apply/destroy routes call ``_gate()`` directly).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from proxbox_api.main import app


def test_plan_empty_diffs_is_permitted_no_op(auth_headers):
    with TestClient(app) as client:
        response = client.post(
            "/intent/plan",
            json={"endpoint_id": 1, "branch_id": 7, "actor": "alice", "diffs": []},
            headers=auth_headers,
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["permitted"] is True
    assert body["verdicts"] == []
    assert "no-op" in body["summary"].lower()


def test_plan_create_and_update_default_permit(auth_headers):
    with TestClient(app) as client:
        response = client.post(
            "/intent/plan",
            json={
                "endpoint_id": 1,
                "diffs": [
                    {"op": "create", "kind": "virtualmachine", "name": "new-vm"},
                    {
                        "op": "update",
                        "kind": "virtualmachine",
                        "netbox_id": 42,
                        "name": "existing-vm",
                    },
                ],
            },
            headers=auth_headers,
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["permitted"] is True
    assert len(body["verdicts"]) == 2
    assert all(v["verdict"] == "permitted" for v in body["verdicts"])
    assert all(v["reason"] == "default_permit" for v in body["verdicts"])


def test_plan_delete_routes_to_deletion_request_warning(auth_headers):
    with TestClient(app) as client:
        response = client.post(
            "/intent/plan",
            json={
                "endpoint_id": 1,
                "diffs": [
                    {
                        "op": "delete",
                        "kind": "virtualmachine",
                        "netbox_id": 99,
                        "name": "doomed-vm",
                    },
                ],
            },
            headers=auth_headers,
        )
    assert response.status_code == 200, response.text
    body = response.json()
    # Warnings do not block the merge; the verdict is not "permitted" but
    # the response-level ``permitted`` flag remains True.
    assert body["permitted"] is True
    assert len(body["verdicts"]) == 1
    verdict = body["verdicts"][0]
    assert verdict["verdict"] == "warning"
    assert verdict["reason"] == "delete_routed_to_deletion_request"
    assert "DeletionRequest" in verdict["message"]


def test_plan_summary_counts_diffs_by_op(auth_headers):
    with TestClient(app) as client:
        response = client.post(
            "/intent/plan",
            json={
                "endpoint_id": 1,
                "diffs": [
                    {"op": "create", "kind": "virtualmachine"},
                    {"op": "create", "kind": "lxc"},
                    {"op": "update", "kind": "virtualmachine", "netbox_id": 1},
                    {"op": "delete", "kind": "virtualmachine", "netbox_id": 2},
                ],
            },
            headers=auth_headers,
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "2 create" in body["summary"]
    assert "1 update" in body["summary"]
    assert "1 delete" in body["summary"]


def test_plan_requires_authentication():
    """No API key → middleware blocks the request before the router."""
    with TestClient(app) as client:
        response = client.post("/intent/plan", json={"endpoint_id": 1, "diffs": []})
    # The auth middleware returns 401 or 403 depending on configuration;
    # any non-2xx status is fine as long as the request never reaches the
    # handler.
    assert response.status_code >= 400


def test_plan_route_is_registered():
    """OpenAPI must list /intent/plan so client codegen sees it."""
    paths = app.openapi().get("paths", {})
    assert "/intent/plan" in paths, (
        f"/intent/plan must be registered; got {sorted(paths.keys())[:20]}"
    )
    assert "post" in {m.lower() for m in paths["/intent/plan"]}
