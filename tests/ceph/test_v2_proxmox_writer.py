"""Unit tests for the Proxmox-backed Ceph v2 write executor (issue #224).

Covers the ``(kind, action) -> CephWrite`` mapping, destructive-confirmation
threading, node resolution, UPID surfacing, capability gating, and the
``ProxmoxCephProviderAdapter.apply()`` integration — all with an injected fake
``CephWrite`` so no live Proxmox/Ceph cluster is required.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from proxbox_api.ceph.endpoint_binding import BoundProxmoxSession
from proxbox_api.ceph.v2_engine import _apply_with_lease_heartbeat
from proxbox_api.ceph.v2_providers import proxmox as proxmox_adapter
from proxbox_api.ceph.v2_providers.base import CephCapabilityUnsupported, CephWriteGateDenied
from proxbox_api.ceph.v2_providers.proxmox import ProxmoxCephProviderAdapter
from proxbox_api.ceph.v2_providers.proxmox_writer import (
    SYNCHRONOUS_OPERATION_KINDS,
    WRITE_OPERATION_KINDS,
    execute_operation,
    operation_kinds,
    resolve_node,
    validate_operation_payload,
)
from proxbox_api.ceph.v2_schemas import DesiredStateBundle, ProviderOperation
from proxbox_api.database import CephOperationRunRecord, ProxmoxEndpoint
from proxbox_api.session.proxmox_core import SensitiveString


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

    async def flag_set(self, flag: str) -> None:
        self._record("flag_set", flag)

    async def flag_unset(self, flag: str) -> None:
        self._record("flag_unset", flag)

    async def osd_create(self, node: str, dev: str, **kwargs: Any) -> str:
        return self._record("osd_create", node, dev, **kwargs)

    async def osd_delete(
        self, node: str, osdid: Any, *, confirm_destroy: bool = False, **kwargs: Any
    ) -> str:
        if not confirm_destroy:
            raise ValueError("osd_delete is destructive; pass confirm_destroy=True")
        return self._record("osd_delete", node, osdid, confirm_destroy=confirm_destroy, **kwargs)

    async def osd_in(self, node: str, osdid: Any) -> None:
        self._record("osd_in", node, osdid)

    async def osd_out(self, node: str, osdid: Any) -> None:
        self._record("osd_out", node, osdid)

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
    node = str(after.pop("node", "node1"))
    return ProviderOperation(
        id=f"op-{kind}-{action}",
        provider="proxmox",
        kind=kind,
        target_ref=target,
        action=action,
        node=node,
        after_summary=after,
    )


def _bound(endpoint_id: int = 7) -> tuple[BoundProxmoxSession, object]:
    endpoint = ProxmoxEndpoint(
        id=endpoint_id,
        name=f"endpoint-{endpoint_id}",
        ip_address="192.0.2.7",
        username="root@pam",
        enabled=True,
        allow_writes=True,
    )
    px = SimpleNamespace(
        db_endpoint_id=endpoint_id,
        ip_address=endpoint.ip_address,
        domain=endpoint.domain,
        http_port=endpoint.port,
        user=endpoint.username,
        password=SensitiveString(endpoint.get_decrypted_password()),
        token_name=endpoint.token_name,
        token_value=SensitiveString(endpoint.get_decrypted_token_value()),
        ssl=endpoint.verify_ssl,
        timeout=5,
        connect_timeout=None,
        max_retries=0,
        retry_backoff=0.5,
        cluster_status=[{"type": "node", "name": "node1"}],
    )
    return BoundProxmoxSession(endpoint=endpoint, session=px, binding_key=b"x" * 32), px


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
    assert disabled["pool:create"] is False
    assert disabled["pool:noop"] is True
    assert "noop" not in disabled


def test_resolve_node_requires_exact_plan_binding_without_fallback() -> None:
    assert resolve_node(_op("pool", "create", "p", node="nodeA"), ["nodeA", "node1"]) == "nodeA"
    with pytest.raises(CephCapabilityUnsupported, match="exact node"):
        resolve_node(ProviderOperation(kind="pool", action="create", target_ref="p"), ["node1"])
    with pytest.raises(CephCapabilityUnsupported, match="not present"):
        resolve_node(_op("pool", "create", "p", node="node2"), ["node1"])
    with pytest.raises(CephCapabilityUnsupported):
        resolve_node(_op("pool", "create", "p", node="node1"), [])


def test_typed_payload_rejects_unknown_keys_and_missing_required_fields() -> None:
    with pytest.raises(CephCapabilityUnsupported, match="payload is invalid"):
        validate_operation_payload(_op("pool", "create", "rbd", silently_dropped=True))
    with pytest.raises(CephCapabilityUnsupported, match="payload is invalid"):
        validate_operation_payload(_op("osd", "create", "/dev/sdb"))

    assert validate_operation_payload(
        _op("osd", "create", "/dev/sdb", dev="/dev/sdb", encrypted=True)
    ) == {"dev": "/dev/sdb", "encrypted": True}


@pytest.mark.asyncio
async def test_adapter_blocks_invalid_node_or_payload_during_planning() -> None:
    bound, _px = _bound()
    adapter = ProxmoxCephProviderAdapter(bound_session=bound)
    missing_node = ProviderOperation(kind="pool", action="create", target_ref="missing")
    unknown_key = _op("pool", "create", "bad", silently_dropped=True)
    valid = _op("pool", "create", "good", size=3)

    planned = await adapter.plan([missing_node, unknown_key, valid])

    assert [item.supported for item in planned] == [False, False, True]
    assert planned[2].after_summary == {"size": 3}


@pytest.mark.asyncio
async def test_diff_preserves_live_node_for_delete_when_summary_is_erased() -> None:
    adapter = ProxmoxCephProviderAdapter()
    desired = DesiredStateBundle.model_validate(
        {"objects": [{"kind": "pool", "target_ref": "old", "action": "delete"}]}
    )
    live = {
        "resources": [
            {
                "kind": "pool",
                "target_ref": "old",
                "node": "node1",
                "summary": {"pool_name": "old", "size": 3},
            }
        ]
    }

    operations = await adapter.diff(desired, live)

    assert operations[0].action == "delete"
    assert operations[0].node == "node1"
    assert operations[0].after_summary == {}


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
    assert res["result"] == "submitted"
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
@pytest.mark.parametrize(
    ("operation", "expected_call"),
    [
        (_op("flag", "create", "noout"), "flag_set"),
        (_op("flag", "update", "noout"), "flag_set"),
        (_op("flag", "delete", "noout"), "flag_unset"),
        (_op("osd", "update", "5", **{"in": True}), "osd_in"),
    ],
)
async def test_sdk_proven_synchronous_pairs_return_typed_completion(
    operation: ProviderOperation,
    expected_call: str,
) -> None:
    write = _FakeWrite()
    result = await execute_operation(
        write,
        operation,
        "node1",
        confirm_destructive=False,
    )
    adapter = ProxmoxCephProviderAdapter()

    assert f"{operation.kind}:{operation.action}" in SYNCHRONOUS_OPERATION_KINDS
    assert result["result"] == "completed"
    assert result["completion_mode"] == "synchronous"
    assert "upid" not in result
    assert adapter.declares_synchronous_success(operation, {**result, "node": "node1"}) is True
    assert write.calls[-1][0] == expected_call


@pytest.mark.asyncio
async def test_task_based_pair_returning_none_never_infers_synchronous_success() -> None:
    class _UnexpectedNoneWrite(_FakeWrite):
        async def pool_create(self, node: str, name: str, **kwargs: Any) -> None:
            self.calls.append(("pool_create", (node, name), kwargs))

    operation = _op("pool", "create", "rbd", size=3)
    result = await execute_operation(
        _UnexpectedNoneWrite(),
        operation,
        "node1",
        confirm_destructive=False,
    )

    assert result["result"] == "submitted"
    assert "completion_mode" not in result
    assert "upid" not in result
    assert ProxmoxCephProviderAdapter().declares_synchronous_success(operation, result) is False


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
    with pytest.raises(CephCapabilityUnsupported, match="payload is invalid"):
        await execute_operation(write, _op("osd", "create", ""), "node1", confirm_destructive=False)


@pytest.mark.asyncio
async def test_osd_update_without_in_flag_is_blocked() -> None:
    write = _FakeWrite()
    with pytest.raises(CephCapabilityUnsupported, match="payload is invalid"):
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

    gate_calls: list[str] = []

    async def gate(_bound_session: BoundProxmoxSession, _database: object) -> None:
        gate_calls.append("checked")

    monkeypatch.setattr(BoundProxmoxSession, "verify_fresh", gate)
    bound, _px = _bound()

    adapter = ProxmoxCephProviderAdapter(
        bound_session=bound,
        database_session=object(),
        writes_authorized=True,
    )
    res = await adapter.apply(_op("pool", "create", "rbd", size=3), confirm_destructive=False)
    assert res["upid"] == "UPID:pool_create"
    assert write.calls[0][1] == ("node1", "rbd")
    assert gate_calls == ["checked"]


@pytest.mark.asyncio
async def test_endpoint_gate_and_lease_heartbeat_serialize_one_database_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _SerializedSession:
        def __init__(self) -> None:
            self.active = False
            self.overlaps = 0
            self.renewals = 0

        async def _touch(self, delay: float = 0) -> None:
            if self.active:
                self.overlaps += 1
                raise RuntimeError("concurrent-session-use")
            self.active = True
            try:
                await asyncio.sleep(delay)
            finally:
                self.active = False

        async def hold_gate(self) -> None:
            await self._touch(0.45)

        async def rollback(self) -> None:
            await self._touch()

        async def exec(self, _statement: Any) -> Any:
            await self._touch()
            self.renewals += 1
            return SimpleNamespace(rowcount=1)

        async def commit(self) -> None:
            await self._touch()

        async def refresh(self, _instance: Any) -> None:
            await self._touch()

        def add(self, _instance: Any) -> None:
            return None

        async def get(self, _entity: Any, _identity: Any) -> Any:
            return None

    database = _SerializedSession()
    bound, _px = _bound()

    async def delayed_gate(
        _bound_session: BoundProxmoxSession,
        database_session: _SerializedSession,
    ) -> None:
        await database_session.hold_gate()

    async def dispatched(
        _write: Any,
        operation: ProviderOperation,
        _node: str,
        *,
        confirm_destructive: bool,
    ) -> dict[str, Any]:
        assert confirm_destructive is True
        return {"operation_id": operation.id, "upid": "UPID:synthetic", "result": "submitted"}

    monkeypatch.setenv("PROXBOX_CEPH_RUN_LEASE_SECONDS", "1")
    monkeypatch.setattr(proxmox_adapter, "_client_for", lambda _px: _FakeClient(_FakeWrite()))
    monkeypatch.setattr(proxmox_adapter, "_node_names", lambda _px: ["node1"])
    monkeypatch.setattr(proxmox_adapter, "execute_operation", dispatched)
    monkeypatch.setattr(BoundProxmoxSession, "verify_fresh", delayed_gate)
    adapter = ProxmoxCephProviderAdapter(
        bound_session=bound,
        database_session=database,
        writes_authorized=True,
    )
    run_record = CephOperationRunRecord(
        id="heartbeat-serialization",
        provider="proxmox",
        status="dispatching",
        lease_owner="worker",
        lease_expires_at=10**12,
    )

    result = await _apply_with_lease_heartbeat(
        database,
        run_record,
        adapter,
        _op("pool", "create", "rbd", size=3),
    )

    assert result["result"] == "submitted"
    assert database.overlaps == 0
    assert database.renewals >= 1


@pytest.mark.asyncio
async def test_adapter_apply_without_session_is_blocked() -> None:
    adapter = ProxmoxCephProviderAdapter(
        [SimpleNamespace(db_endpoint_id=7)],
        database_session=object(),
        writes_authorized=True,
    )
    with pytest.raises(CephWriteGateDenied, match="privately bound"):
        await adapter.apply(_op("pool", "create", "rbd"), confirm_destructive=False)


@pytest.mark.asyncio
async def test_adapter_apply_without_write_support_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoWriteClient:
        write = None

    monkeypatch.setattr(proxmox_adapter, "_client_for", lambda _px: _NoWriteClient())
    monkeypatch.setattr(proxmox_adapter, "_node_names", lambda _px: ["node1"])

    async def gate(_bound_session: BoundProxmoxSession, _database: object) -> None:
        return None

    monkeypatch.setattr(BoundProxmoxSession, "verify_fresh", gate)
    bound, _px = _bound()
    adapter = ProxmoxCephProviderAdapter(
        bound_session=bound,
        database_session=object(),
        writes_authorized=True,
    )
    with pytest.raises(CephCapabilityUnsupported, match="CephWrite"):
        await adapter.apply(_op("pool", "create", "rbd"), confirm_destructive=False)


@pytest.mark.asyncio
async def test_adapter_capabilities_reflect_write_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound, _px = _bound()
    adapter = ProxmoxCephProviderAdapter(
        bound_session=bound,
        database_session=object(),
        writes_authorized=True,
    )

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


@pytest.mark.asyncio
async def test_task_poll_timeout_is_outcome_unknown_on_exact_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound, px = _bound()
    observed: list[tuple[object, str, str]] = []

    async def running(session: object, node: str, upid: str) -> dict[str, str]:
        observed.append((session, node, upid))
        return {"status": "running", "exitstatus": ""}

    monkeypatch.setattr(proxmox_adapter, "get_node_task_status", running)
    adapter = ProxmoxCephProviderAdapter(
        bound_session=bound,
        database_session=object(),
        writes_authorized=True,
        task_poll_timeout=0,
        task_poll_interval=0,
    )
    outcome = await adapter.wait_for_terminal("node1", "UPID:timeout")

    assert outcome == {"state": "outcome_unknown", "code": "provider_task_timeout"}
    assert observed == [(px, "node1", "UPID:timeout")]


@pytest.mark.asyncio
async def test_task_poll_renews_worker_lease_until_terminal_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound, _px = _bound()
    statuses = iter(
        (
            {"status": "running", "exitstatus": ""},
            {"status": "stopped", "exitstatus": "OK"},
        )
    )
    heartbeats = 0

    async def status(_session: object, _node: str, _upid: str) -> dict[str, str]:
        return next(statuses)

    async def heartbeat() -> None:
        nonlocal heartbeats
        heartbeats += 1

    monkeypatch.setattr(proxmox_adapter, "get_node_task_status", status)
    adapter = ProxmoxCephProviderAdapter(
        bound_session=bound,
        database_session=object(),
        writes_authorized=True,
        task_poll_timeout=1,
        task_poll_interval=0,
    )

    outcome = await adapter.wait_for_terminal(
        "node1",
        "UPID:heartbeat",
        heartbeat=heartbeat,
    )

    assert outcome == {"state": "completed", "code": "provider_task_completed"}
    assert heartbeats == 2


@pytest.mark.parametrize("raw", ["not-a-number", "nan", "inf", "-inf"])
def test_task_poll_environment_rejects_nonfinite_or_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    monkeypatch.setenv("PROXBOX_CEPH_TASK_TIMEOUT", raw)

    assert (
        proxmox_adapter._bounded_float_env(
            "PROXBOX_CEPH_TASK_TIMEOUT",
            default=300.0,
            minimum=1.0,
            maximum=3600.0,
        )
        == 300.0
    )


@pytest.mark.asyncio
async def test_task_poll_transport_failure_is_secret_free_outcome_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound, _px = _bound()

    async def fail(_session: object, _node: str, _upid: str) -> dict[str, str]:
        raise RuntimeError("https://operator:secret@pve.invalid?token=canary")

    monkeypatch.setattr(proxmox_adapter, "get_node_task_status", fail)
    adapter = ProxmoxCephProviderAdapter(
        bound_session=bound,
        database_session=object(),
        writes_authorized=True,
    )
    outcome = await adapter.wait_for_terminal("node1", "UPID:transport")

    assert outcome == {
        "state": "outcome_unknown",
        "code": "provider_task_status_unavailable",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation_key",
    [
        key
        for key, supported in WRITE_OPERATION_KINDS.items()
        if supported and not key.endswith(":noop")
    ],
)
async def test_every_declared_mutation_passes_through_common_gate(
    monkeypatch: pytest.MonkeyPatch,
    operation_key: str,
) -> None:
    endpoint_id = 23
    gate_calls: list[str] = []
    dispatches: list[str] = []

    async def gate(_bound_session: BoundProxmoxSession, _database: object) -> None:
        gate_calls.append(operation_key)

    async def fake_execute(
        _write: Any,
        operation: ProviderOperation,
        _node: str,
        *,
        confirm_destructive: bool,
    ) -> dict[str, Any]:
        assert confirm_destructive is True
        dispatches.append(f"{operation.kind}:{operation.action}")
        return {"result": "applied"}

    monkeypatch.setattr(
        proxmox_adapter,
        "_client_for",
        lambda _px: SimpleNamespace(write=object()),
    )
    monkeypatch.setattr(proxmox_adapter, "_node_names", lambda _px: ["node1"])
    monkeypatch.setattr(proxmox_adapter, "execute_operation", fake_execute)
    monkeypatch.setattr(BoundProxmoxSession, "verify_fresh", gate)
    bound, _px = _bound(endpoint_id)
    adapter = ProxmoxCephProviderAdapter(
        bound_session=bound,
        database_session=object(),
        writes_authorized=True,
    )
    kind, action = operation_key.split(":", maxsplit=1)
    await adapter.apply(_op(kind, action, "target"), confirm_destructive=True)
    assert gate_calls == [operation_key]
    assert dispatches == [operation_key]


@pytest.mark.asyncio
async def test_adapter_never_falls_back_to_first_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = SimpleNamespace(db_endpoint_id=2)
    first = SimpleNamespace(db_endpoint_id=1)
    used: list[object] = []

    async def gate(_bound_session: BoundProxmoxSession, _database: object) -> None:
        return None

    monkeypatch.setattr(
        proxmox_adapter,
        "_client_for",
        lambda px: used.append(px) or SimpleNamespace(write=_FakeWrite()),
    )
    monkeypatch.setattr(proxmox_adapter, "_node_names", lambda _px: ["node1"])
    monkeypatch.setattr(BoundProxmoxSession, "verify_fresh", gate)
    bound, selected = _bound(2)
    adapter = ProxmoxCephProviderAdapter(
        [first],
        bound_session=bound,
        database_session=object(),
        writes_authorized=True,
    )
    await adapter.apply(_op("pool", "create", "rbd"), confirm_destructive=True)
    assert used == [selected]
