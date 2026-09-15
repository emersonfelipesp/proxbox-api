"""Read/plan contract tests for the netbox-ceph CephOperation payload.

Proves that the payload the ``netbox-ceph`` orchestrator posts to
``/ceph/v2/plans`` carries its resolved proxbox-api endpoint ID through the real
HTTP route, is coerced into a :class:`DesiredStateBundle`, and flows through the
Proxmox adapter ``diff`` into a ``ProviderOperation``. Write authorization is
deliberately covered by the HTTP approval tests; legacy ``confirmed`` fields
are not execution authority.
"""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api.ceph.v2_providers.proxmox import ProxmoxCephProviderAdapter
from proxbox_api.ceph.v2_providers.proxmox_writer import execute_operation
from proxbox_api.ceph.v2_schemas import PlanRequest


def _netbox_operation_payload(
    operation_type: str = "create",
    target_kind: str = "pool",
    target_ref: str = "rbd",
    desired: dict[str, Any] | None = None,
    *,
    endpoint_id: int = 41,
    is_destructive: bool = False,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Mirror ``netbox_ceph.services.operation_actions.operation_payload``."""
    desired_payload = desired if desired is not None else {"size": 3, "pg_num": 128}
    return {
        "id": 1,
        "cluster_id": 7,
        "provider_id": 3,
        "provider_kind": "proxmox",
        "provider_name": "pve-cluster",
        "provider": "proxmox",
        "endpoint_id": endpoint_id,
        "operation_type": operation_type,
        "target_kind": target_kind,
        "target_ref": target_ref,
        "execution_node": "node1",
        "desired": desired_payload,
        "desired_state": {
            "objects": [
                {
                    "kind": target_kind,
                    "target_ref": target_ref,
                    "action": operation_type,
                    "provider": "proxmox",
                    "node": "node1",
                    "payload": desired_payload,
                }
            ]
        },
        "is_destructive": is_destructive,
        "confirmation_required": is_destructive,
        "confirmed": confirmed,
        "source_branch_schema_id": "branch-abc",
    }


class _FakeWrite:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def pool_create(self, node: str, name: str, **kwargs: Any) -> str:
        self.calls.append(("pool_create", (node, name), kwargs))
        return "UPID:pool_create"


# --------------------------------------------------------------------------- #
# Schema coercion
# --------------------------------------------------------------------------- #


def test_plan_request_coerces_netbox_operation_into_bundle() -> None:
    request = PlanRequest.model_validate(_netbox_operation_payload())
    assert request.provider == "proxmox"
    assert len(request.desired_state.objects) == 1
    obj = request.desired_state.objects[0]
    assert obj.kind == "pool"
    assert obj.action == "create"
    assert obj.target_ref == "rbd"
    assert obj.node == "node1"
    assert obj.payload == {"size": 3, "pg_num": 128}
    assert obj.provider == "proxmox"
    assert request.endpoint_id == 41
    # branch id threads through from source_branch_schema_id
    assert request.source_branch_schema_id == "branch-abc"


def test_plan_request_without_target_kind_is_unaffected() -> None:
    # A native bundle payload still works (additive coercion).
    request = PlanRequest.model_validate(
        {"provider": "proxmox", "desired_state": {"objects": [{"kind": "pool", "target_ref": "x"}]}}
    )
    assert request.desired_state.objects[0].kind == "pool"


# --------------------------------------------------------------------------- #
# Read/plan chain: payload -> coercion -> adapter.diff -> executor mapping
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_full_chain_pool_create() -> None:
    request = PlanRequest.model_validate(_netbox_operation_payload())
    adapter = ProxmoxCephProviderAdapter([])
    operations = await adapter.diff(request.desired_state, {"resources": []})
    assert len(operations) == 1
    op = operations[0]
    assert op.kind == "pool"
    assert op.action == "create"
    assert op.target_ref == "rbd"
    assert op.node == "node1"
    assert op.after_summary == {"size": 3, "pg_num": 128}

    write = _FakeWrite()
    result = await execute_operation(write, op, "node1", confirm_destructive=False)
    assert result["upid"] == "UPID:pool_create"
    assert write.calls[0][0] == "pool_create"
    assert write.calls[0][1] == ("node1", "rbd")
    assert write.calls[0][2] == {"size": 3, "pg_num": 128}


@pytest.mark.asyncio
async def test_netbox_shaped_identical_pool_is_canonical_noop_without_dispatch() -> None:
    request = PlanRequest.model_validate(_netbox_operation_payload(operation_type="update"))
    adapter = ProxmoxCephProviderAdapter([])
    live = {
        "resources": [
            {
                "kind": "pool",
                "target_ref": "rbd",
                "node": "node1",
                "summary": {"size": 3, "pg_num": 128},
            }
        ]
    }

    operations = await adapter.diff(request.desired_state, live)

    assert len(operations) == 1
    assert operations[0].action == "noop"
    assert operations[0].node == "node1"
    assert operations[0].after_summary == {"size": 3, "pg_num": 128}

    write = _FakeWrite()
    result = await execute_operation(write, operations[0], "node1", confirm_destructive=False)
    assert result["result"] == "noop"
    assert write.calls == []


@pytest.mark.asyncio
async def test_netbox_shaped_changed_pool_remains_update() -> None:
    request = PlanRequest.model_validate(
        _netbox_operation_payload(
            operation_type="update",
            desired={"size": 2, "pg_num": 128},
        )
    )
    adapter = ProxmoxCephProviderAdapter([])
    live = {
        "resources": [
            {
                "kind": "pool",
                "target_ref": "rbd",
                "node": "node1",
                "summary": {"size": 3, "pg_num": 128},
            }
        ]
    }

    operations = await adapter.diff(request.desired_state, live)

    assert operations[0].action == "update"
    assert operations[0].after_summary == {"size": 2, "pg_num": 128}
