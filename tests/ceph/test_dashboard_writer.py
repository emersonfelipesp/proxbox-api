"""Ceph Dashboard write-operation mapping tests (#98)."""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api.ceph.v2_providers.base import CephCapabilityUnsupported
from proxbox_api.ceph.v2_providers.dashboard_writer import (
    execute_dashboard_operation,
    operation_kinds,
)
from proxbox_api.ceph.v2_schemas import ProviderOperation


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        async def _call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, args, kwargs))
            return {"name": f"task-{name}"}

        return _call


def _op(kind: str, action: str, *, target_ref: str = "rbd/vm", **summary: Any) -> ProviderOperation:
    return ProviderOperation(kind=kind, target_ref=target_ref, action=action, after_summary=summary)


def test_operation_kinds_gated_on_availability() -> None:
    assert operation_kinds(False)["pool:create"] is False
    on = operation_kinds(True)
    assert on["pool:create"] is True
    assert on["rbd_snapshot:delete"] is True
    assert on["rgw_bucket:delete"] is False


async def test_pool_create_update_delete() -> None:
    client = _FakeClient()
    await execute_dashboard_operation(
        client, _op("pool", "create", target_ref="rbd", pool_name="rbd"), confirm_destructive=False
    )
    await execute_dashboard_operation(
        client, _op("pool", "update", target_ref="rbd", size=4), confirm_destructive=False
    )
    res = await execute_dashboard_operation(
        client, _op("pool", "delete", target_ref="rbd"), confirm_destructive=True
    )
    names = [c[0] for c in client.calls]
    assert names == ["pool_create", "pool_edit", "pool_delete"]
    # destructive confirm threaded
    assert client.calls[2][2]["confirm_destroy"] is True
    assert res["result"] == "applied"


async def test_rbd_image_and_snapshot_specs() -> None:
    client = _FakeClient()
    await execute_dashboard_operation(
        client,
        _op("rbd_image", "create", target_ref="rbd/vm-1", pool_name="rbd", name="vm-1", size=10),
        confirm_destructive=False,
    )
    await execute_dashboard_operation(
        client,
        _op(
            "rbd_snapshot",
            "create",
            target_ref="rbd/vm-1",
            pool_name="rbd",
            name="vm-1",
            snapshot_name="snap1",
        ),
        confirm_destructive=False,
    )
    assert client.calls[0][0] == "rbd_create"
    assert client.calls[1][0] == "rbd_snapshot_create"
    # snapshot create passes the image spec "rbd/vm-1" + snap name
    assert client.calls[1][1] == ("rbd/vm-1", "snap1")


async def test_unsupported_rgw_write_reported() -> None:
    client = _FakeClient()
    with pytest.raises(CephCapabilityUnsupported, match="rgw_bucket:delete"):
        await execute_dashboard_operation(
            client, _op("rgw_bucket", "delete", target_ref="backups"), confirm_destructive=True
        )


async def test_noop_is_passthrough() -> None:
    client = _FakeClient()
    res = await execute_dashboard_operation(
        client, _op("pool", "noop", target_ref="rbd"), confirm_destructive=False
    )
    assert res["result"] == "noop"
    assert client.calls == []
