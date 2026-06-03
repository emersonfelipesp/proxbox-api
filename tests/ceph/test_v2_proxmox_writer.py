"""Unit tests for the Proxmox-backed Ceph v2 write executor (issue #224).

Covers the ``(kind, action) -> CephWrite`` mapping, destructive-confirmation
threading, node resolution, UPID surfacing, capability gating, and the
``ProxmoxCephProviderAdapter.apply()`` integration — all with an injected fake
``CephWrite`` so no live Proxmox/Ceph cluster is required.
"""

from __future__ import annotations

from typing import Any

import pytest

from proxbox_api.ceph.v2_providers import proxmox as proxmox_adapter
from proxbox_api.ceph.v2_providers.base import CephCapabilityUnsupported
from proxbox_api.ceph.v2_providers.proxmox import ProxmoxCephProviderAdapter
from proxbox_api.ceph.v2_providers.proxmox_writer import (
    execute_operation,
    operation_kinds,
    resolve_node,
)
from proxbox_api.ceph.v2_schemas import ProviderOperation


class _FakeWrite:
    """Records CephWrite calls and enforces destructive confirmation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> str:
        self.calls.append((name, args, kwargs))
        return f"UPID:{name}"

    async def pool_create(self, node: str, name: str, **kwargs: Any) -> str:
        return self._record("pool_create", node, name, **kwargs)

    async def pool_set(self, node: str, name: str, **kwargs: Any) -> str:
        return self._record("pool_set", node, name, **kwargs)

    async def pool_delete(
        self, node: str, name: str, *, confirm_destroy: bool = False, **kwargs: Any
    ) -> str:
        if not confirm_destroy:
            raise ValueError("pool_delete is destructive; pass confirm_destroy=True")
        return self._record("pool_delete", node, name, confirm_destroy=confirm_destroy, **kwargs)

    async def flag_set(self, flag: str) -> str:
        return self._record("flag_set", flag)

    async def flag_unset(self, flag: str) -> str:
        return self._record("flag_unset", flag)

    async def osd_create(self, node: str, dev: str, **kwargs: Any) -> str:
        return self._record("osd_create", node, dev, **kwargs)

    async def osd_delete(
        self, node: str, osdid: Any, *, confirm_destroy: bool = False, **kwargs: Any
    ) -> str:
        if not confirm_destroy:
            raise ValueError("osd_delete is destructive; pass confirm_destroy=True")
        return self._record("osd_delete", node, osdid, confirm_destroy=confirm_destroy, **kwargs)

    async def osd_in(self, node: str, osdid: Any) -> str:
        return self._record("osd_in", node, osdid)

    async def osd_out(self, node: str, osdid: Any) -> str:
        return self._record("osd_out", node, osdid)

    async def mon_create(self, node: str, monid: str, **kwargs: Any) -> str:
        return self._record("mon_create", node, monid, **kwargs)

    async def mon_delete(self, node: str, monid: str, *, confirm_destroy: bool = False) -> str:
        if not confirm_destroy:
            raise ValueError("mon_delete is destructive")
        return self._record("mon_delete", node, monid, confirm_destroy=confirm_destroy)

    async def mgr_create(self, node: str, mgr_id: str) -> str:
        return self._record("mgr_create", node, mgr_id)

    async def mgr_delete(self, node: str, mgr_id: str, *, confirm_destroy: bool = False) -> str:
        if not confirm_destroy:
            raise ValueError("mgr_delete is destructive")
        return self._record("mgr_delete", node, mgr_id, confirm_destroy=confirm_destroy)

    async def mds_create(self, node: str, name: str, **kwargs: Any) -> str:
        return self._record("mds_create", node, name, **kwargs)

    async def mds_delete(self, node: str, name: str, *, confirm_destroy: bool = False) -> str:
        if not confirm_destroy:
            raise ValueError("mds_delete is destructive")
        return self._record("mds_delete", node, name, confirm_destroy=confirm_destroy)

    async def cephfs_create(self, node: str, name: str, **kwargs: Any) -> str:
        return self._record("cephfs_create", node, name, **kwargs)


def _op(kind: str, action: str, target: str = "", **after: Any) -> ProviderOperation:
    return ProviderOperation(
        id=f"op-{kind}-{action}",
        provider="proxmox",
        kind=kind,
        target_ref=target,
        action=action,
        after_summary=after,
    )


# --------------------------------------------------------------------------- #
# operation_kinds + resolve_node
# --------------------------------------------------------------------------- #


def test_operation_kinds_gated_by_write_availability() -> None:
    enabled = operation_kinds(True)
    assert enabled["pool:create"] is True
    assert enabled["pool:delete"] is True
    assert enabled["osd:update"] is True
    # Always-unsupported kinds stay False even when writes are enabled.
    assert enabled["filesystem:delete"] is False
    assert enabled["crush_rule:create"] is False

    disabled = operation_kinds(False)
    assert all(value is False for value in disabled.values())


def test_resolve_node_prefers_payload_then_first_node() -> None:
    assert resolve_node(_op("pool", "create", "p", node="nodeA"), ["node1"]) == "nodeA"
    assert resolve_node(_op("pool", "create", "p"), ["node1", "node2"]) == "node1"
    with pytest.raises(CephCapabilityUnsupported):
        resolve_node(_op("pool", "create", "p"), [])


# --------------------------------------------------------------------------- #
# execute_operation mapping
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pool_create_update_map_to_write() -> None:
    write = _FakeWrite()
    res = await execute_operation(
        write, _op("pool", "create", "rbd", size=3, pg_num=128), "node1", confirm_destructive=False
    )
    assert res["upid"] == "UPID:pool_create"
    assert res["result"] == "applied"
    name, args, kwargs = write.calls[0]
    assert name == "pool_create"
    assert args == ("node1", "rbd")
    assert kwargs == {"size": 3, "pg_num": 128}

    await execute_operation(
        write, _op("pool", "update", "rbd", size=2), "node1", confirm_destructive=False
    )
    assert write.calls[1][0] == "pool_set"


@pytest.mark.asyncio
async def test_pool_delete_requires_confirmation() -> None:
    write = _FakeWrite()
    op = _op("pool", "delete", "rbd")
    with pytest.raises(ValueError, match="destructive"):
        await execute_operation(write, op, "node1", confirm_destructive=False)

    res = await execute_operation(write, op, "node1", confirm_destructive=True)
    assert res["upid"] == "UPID:pool_delete"
    assert write.calls[-1][2]["confirm_destroy"] is True


@pytest.mark.asyncio
async def test_flag_set_and_unset() -> None:
    write = _FakeWrite()
    await execute_operation(
        write, _op("flag", "create", "noout"), "node1", confirm_destructive=False
    )
    await execute_operation(
        write, _op("flag", "delete", "noout"), "node1", confirm_destructive=False
    )
    assert [c[0] for c in write.calls] == ["flag_set", "flag_unset"]
    # flag helpers take only the flag name (no node).
    assert write.calls[0][1] == ("noout",)


@pytest.mark.asyncio
async def test_osd_lifecycle() -> None:
    write = _FakeWrite()
    await execute_operation(
        write, _op("osd", "create", "", dev="/dev/sdb"), "node1", confirm_destructive=False
    )
    assert write.calls[-1][0] == "osd_create"
    assert write.calls[-1][1] == ("node1", "/dev/sdb")

    await execute_operation(
        write, _op("osd", "update", "5", **{"in": True}), "node1", confirm_destructive=False
    )
    assert write.calls[-1][0] == "osd_in"
    await execute_operation(
        write, _op("osd", "update", "5", **{"in": False}), "node1", confirm_destructive=False
    )
    assert write.calls[-1][0] == "osd_out"

    res = await execute_operation(
        write, _op("osd", "delete", "5"), "node1", confirm_destructive=True
    )
    assert res["upid"] == "UPID:osd_delete"


@pytest.mark.asyncio
async def test_osd_create_without_dev_is_blocked() -> None:
    write = _FakeWrite()
    with pytest.raises(CephCapabilityUnsupported, match="dev"):
        await execute_operation(write, _op("osd", "create", ""), "node1", confirm_destructive=False)


@pytest.mark.asyncio
async def test_osd_update_without_in_flag_is_blocked() -> None:
    write = _FakeWrite()
    with pytest.raises(CephCapabilityUnsupported, match="'in'"):
        await execute_operation(
            write, _op("osd", "update", "5"), "node1", confirm_destructive=False
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["mon", "mgr", "mds"])
async def test_mon_mgr_mds_lifecycle(kind: str) -> None:
    write = _FakeWrite()
    await execute_operation(write, _op(kind, "create", "x"), "node1", confirm_destructive=False)
    assert write.calls[-1][0] == f"{kind}_create"
    with pytest.raises(ValueError, match="destructive"):
        await execute_operation(write, _op(kind, "delete", "x"), "node1", confirm_destructive=False)
    res = await execute_operation(
        write, _op(kind, "delete", "x"), "node1", confirm_destructive=True
    )
    assert res["upid"] == f"UPID:{kind}_delete"


@pytest.mark.asyncio
async def test_filesystem_create_and_blocked_delete() -> None:
    write = _FakeWrite()
    res = await execute_operation(
        write, _op("filesystem", "create", "cephfs", pg_num=64), "node1", confirm_destructive=False
    )
    assert res["upid"] == "UPID:cephfs_create"
    with pytest.raises(CephCapabilityUnsupported):
        await execute_operation(
            write, _op("filesystem", "delete", "cephfs"), "node1", confirm_destructive=True
        )


@pytest.mark.asyncio
async def test_crush_rule_is_explicitly_unsupported() -> None:
    write = _FakeWrite()
    with pytest.raises(CephCapabilityUnsupported):
        await execute_operation(
            write, _op("crush_rule", "create", "r1"), "node1", confirm_destructive=False
        )


@pytest.mark.asyncio
async def test_noop_returns_without_calling_write() -> None:
    write = _FakeWrite()
    res = await execute_operation(
        write, _op("pool", "noop", "rbd"), "node1", confirm_destructive=False
    )
    assert res["result"] == "noop"
    assert "upid" not in res
    assert write.calls == []


# --------------------------------------------------------------------------- #
# Adapter integration
# --------------------------------------------------------------------------- #


class _FakeClient:
    def __init__(self, write: _FakeWrite) -> None:
        self.write = write


@pytest.mark.asyncio
async def test_adapter_apply_dispatches_through_write(monkeypatch: pytest.MonkeyPatch) -> None:
    write = _FakeWrite()
    monkeypatch.setattr(proxmox_adapter, "_client_for", lambda _px: _FakeClient(write))
    monkeypatch.setattr(proxmox_adapter, "_node_names", lambda _px: ["node1"])

    adapter = ProxmoxCephProviderAdapter([object()])
    res = await adapter.apply(_op("pool", "create", "rbd", size=3), confirm_destructive=False)
    assert res["upid"] == "UPID:pool_create"
    assert write.calls[0][1] == ("node1", "rbd")


@pytest.mark.asyncio
async def test_adapter_apply_without_session_is_blocked() -> None:
    adapter = ProxmoxCephProviderAdapter([])
    with pytest.raises(CephCapabilityUnsupported, match="No Proxmox session"):
        await adapter.apply(_op("pool", "create", "rbd"), confirm_destructive=False)


@pytest.mark.asyncio
async def test_adapter_apply_without_write_support_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoWriteClient:
        write = None

    monkeypatch.setattr(proxmox_adapter, "_client_for", lambda _px: _NoWriteClient())
    monkeypatch.setattr(proxmox_adapter, "_node_names", lambda _px: ["node1"])
    adapter = ProxmoxCephProviderAdapter([object()])
    with pytest.raises(CephCapabilityUnsupported, match="CephWrite"):
        await adapter.apply(_op("pool", "create", "rbd"), confirm_destructive=False)


@pytest.mark.asyncio
async def test_adapter_capabilities_reflect_write_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = ProxmoxCephProviderAdapter([])

    monkeypatch.setattr(proxmox_adapter, "cephwrite_importable", lambda: True)
    caps = await adapter.capabilities()
    assert caps.apply is True
    assert caps.destructive_operations is True
    assert caps.operation_kinds["pool:create"] is True

    monkeypatch.setattr(proxmox_adapter, "cephwrite_importable", lambda: False)
    caps = await adapter.capabilities()
    assert caps.apply is False
    assert caps.destructive_operations is False
    assert caps.operation_kinds["pool:create"] is False
