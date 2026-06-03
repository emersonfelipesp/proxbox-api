"""End-to-end contract tests for the netbox-ceph CephOperation payload (issue #226).

Proves that the payload the ``netbox-ceph`` orchestrator posts to
``/ceph/v2/plan`` and ``/ceph/v2/apply`` is coerced into a real
:class:`DesiredStateBundle`, flows through the Proxmox adapter ``diff`` into a
``ProviderOperation``, and is executed by the Proxmox write executor — the full
NetBox -> Proxmox Ceph write chain, with an injected fake ``CephWrite`` (no live
cluster).
"""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api.ceph.v2_providers.proxmox import ProxmoxCephProviderAdapter
from proxbox_api.ceph.v2_providers.proxmox_writer import execute_operation
from proxbox_api.ceph.v2_schemas import ApplyRequest, PlanRequest


def _netbox_operation_payload(
    operation_type: str = "create",
    target_kind: str = "pool",
    target_ref: str = "rbd",
    desired: dict[str, Any] | None = None,
    *,
    is_destructive: bool = False,
    confirmed: bool = False,
) -> dict[str, Any]:
    """Mirror ``netbox_ceph.api.views._operation_payload`` output."""
    return {
        "id": 1,
        "cluster_id": 7,
        "provider_id": 3,
        "provider_kind": "proxmox",
        "provider_name": "pve-cluster",
        "operation_type": operation_type,
        "target_kind": target_kind,
        "target_ref": target_ref,
        "desired": desired if desired is not None else {"size": 3, "pg_num": 128},
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

    async def pool_delete(
        self, node: str, name: str, *, confirm_destroy: bool = False, **kwargs: Any
    ) -> str:
        if not confirm_destroy:
            raise ValueError("pool_delete is destructive; pass confirm_destroy=True")
        self.calls.append(("pool_delete", (node, name), {"confirm_destroy": confirm_destroy}))
        return "UPID:pool_delete"


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
    assert obj.payload == {"size": 3, "pg_num": 128}
    assert obj.provider == "proxmox"
    # branch id threads through from source_branch_schema_id
    assert request.source_branch_schema_id == "branch-abc"


def test_apply_request_maps_confirmed_to_confirm_destructive() -> None:
    confirmed = ApplyRequest.model_validate(
        _netbox_operation_payload(operation_type="delete", is_destructive=True, confirmed=True)
    )
    assert confirmed.confirm_destructive is True
    assert confirmed.desired_state is not None
    assert confirmed.desired_state.objects[0].action == "delete"

    unconfirmed = ApplyRequest.model_validate(
        _netbox_operation_payload(operation_type="delete", is_destructive=True, confirmed=False)
    )
    assert unconfirmed.confirm_destructive is False


def test_plan_request_without_target_kind_is_unaffected() -> None:
    # A native bundle payload still works (additive coercion).
    request = PlanRequest.model_validate(
        {"provider": "proxmox", "desired_state": {"objects": [{"kind": "pool", "target_ref": "x"}]}}
    )
    assert request.desired_state.objects[0].kind == "pool"


# --------------------------------------------------------------------------- #
# Full chain: payload -> coercion -> adapter.diff -> executor
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
    assert op.after_summary == {"size": 3, "pg_num": 128}

    write = _FakeWrite()
    result = await execute_operation(write, op, "node1", confirm_destructive=False)
    assert result["upid"] == "UPID:pool_create"
    assert write.calls[0][0] == "pool_create"
    assert write.calls[0][1] == ("node1", "rbd")
    assert write.calls[0][2] == {"size": 3, "pg_num": 128}


@pytest.mark.asyncio
async def test_full_chain_pool_delete_requires_confirmation() -> None:
    payload = _netbox_operation_payload(
        operation_type="delete", is_destructive=True, confirmed=True
    )
    apply_request = ApplyRequest.model_validate(payload)
    adapter = ProxmoxCephProviderAdapter([])
    # live state contains the pool so diff plans a delete
    live = {"resources": [{"kind": "pool", "target_ref": "rbd", "summary": {"size": 3}}]}
    operations = await adapter.diff(apply_request.desired_state, live)
    op = operations[0]
    assert op.action == "delete"

    write = _FakeWrite()
    # confirmed=True -> confirm_destructive=True satisfies the executor
    result = await execute_operation(
        write, op, "node1", confirm_destructive=apply_request.confirm_destructive
    )
    assert result["upid"] == "UPID:pool_delete"

    # And without confirmation the destructive write is refused.
    with pytest.raises(ValueError, match="destructive"):
        await execute_operation(_FakeWrite(), op, "node1", confirm_destructive=False)
