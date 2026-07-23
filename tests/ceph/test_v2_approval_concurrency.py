"""Atomic single-use approval tests for the Ceph v2 apply engine."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.ceph.v2_engine import (
    CephApplyError,
    _append_event_checkpoint,
    _CephRunLeaseLost,
    apply_plan,
    canonical_plan_digest,
    load_persisted_plan,
    recover_stale_operation_run,
    utcnow,
)
from proxbox_api.ceph.v2_providers.proxmox import ProxmoxCephProviderAdapter
from proxbox_api.ceph.v2_providers.proxmox_writer import execute_operation
from proxbox_api.ceph.v2_schemas import ApplyRequest, PlanResponse, ProviderOperation
from proxbox_api.database import (
    CephApprovalRecord,
    CephOperationEventRecord,
    CephOperationRunRecord,
    CephPlanRecord,
    CephProviderTaskClaimRecord,
    _apply_sqlite_pragmas,
)

ATOMIC_UPID = "UPID:node1:00000001:00000002:00000003:cephcreate:rbd:root@pam:"
INTERRUPT_UPID = "UPID:node1:00000004:00000005:00000006:cephcreate:rbd:root@pam:"


async def _cancel_repeatedly_while_inner_task_is_blocked(
    task: asyncio.Task[Any],
    count: int,
) -> None:
    """Deliver each cancellation on a separate event-loop turn."""

    for _ in range(count):
        task.cancel()
        await asyncio.sleep(0)


class _CountingAdapter:
    """Minimal adapter that records every provider mutation attempt."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def apply(
        self,
        operation: ProviderOperation,
        *,
        confirm_destructive: bool,
    ) -> dict[str, Any]:
        assert confirm_destructive is True
        self.calls.append(operation.id or operation.target_ref)
        await asyncio.sleep(0)
        return {"provider_task_ref": ATOMIC_UPID, "node": "node1"}

    async def wait_for_terminal(self, node: str, upid: str) -> dict[str, str]:
        assert node == "node1"
        assert upid == ATOMIC_UPID
        return {"state": "completed", "code": "provider_task_completed"}


def _canonical_plan(endpoint_id: int = 17) -> PlanResponse:
    now = utcnow()
    plan = PlanResponse(
        id=str(uuid4()),
        provider="proxmox",
        endpoint_id=endpoint_id,
        endpoint_config_revision="a" * 64,
        requester="alice",
        operations=[
            ProviderOperation(
                id=str(uuid4()),
                provider="proxmox",
                kind="pool",
                target_ref="rbd",
                action="create",
                node="node1",
            )
        ],
        created_at=now,
        expires_at=now + timedelta(minutes=15),
    )
    plan.digest = canonical_plan_digest(plan)
    return plan


def _seed_control_plane(
    sync_engine: Any,
    *,
    plan: PlanResponse,
    raw_token: str,
    approval_id: str,
) -> None:
    SQLModel.metadata.create_all(sync_engine)
    with Session(sync_engine) as session:
        session.add(
            CephPlanRecord(
                id=plan.id,
                provider=plan.provider,
                endpoint_id=plan.endpoint_id,
                endpoint_config_revision=plan.endpoint_config_revision,
                requester=plan.requester or "",
                source_branch_schema_id=plan.source_branch_schema_id,
                digest=plan.digest,
                plan_payload=plan.model_dump(mode="json"),
                created_at=plan.created_at.timestamp(),
                expires_at=plan.expires_at.timestamp(),
            )
        )
        session.add(
            CephApprovalRecord(
                id=approval_id,
                plan_id=plan.id,
                plan_digest=plan.digest,
                endpoint_id=plan.endpoint_id,
                endpoint_config_revision=plan.endpoint_config_revision,
                requester="alice",
                approver="bob",
                token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
                created_at=plan.created_at.timestamp(),
                expires_at=plan.expires_at.timestamp(),
            )
        )
        session.commit()


@pytest.mark.skipif(
    sys.version_info >= (3, 14),
    reason="aiosqlite connection worker does not complete under this local 3.14 toolchain",
)
def test_concurrent_apply_consumes_one_approval_exactly_once(tmp_path) -> None:
    """Two production AsyncSessions race; only one reaches the provider adapter."""

    database_path = tmp_path / "ceph-approval-race.db"
    sync_engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)

    plan = _canonical_plan()
    raw_token = secrets.token_urlsafe(48)
    approval_id = str(uuid4())
    _seed_control_plane(
        sync_engine,
        plan=plan,
        raw_token=raw_token,
        approval_id=approval_id,
    )
    sync_engine.dispose()

    adapter = _CountingAdapter()

    async def race() -> tuple[list[tuple[str, str | None]], CephApprovalRecord]:
        async_engine = create_async_engine(
            f"sqlite+aiosqlite:///{database_path}",
            connect_args={"check_same_thread": False, "timeout": 5},
        )
        session_factory = async_sessionmaker(
            async_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        async def attempt() -> tuple[str, str | None]:
            async with session_factory() as session:
                persisted = await load_persisted_plan(session, plan.id)
                try:
                    run = await apply_plan(
                        persisted,
                        ApplyRequest(
                            plan_id=plan.id,
                            endpoint_id=plan.endpoint_id,
                            approval_token=raw_token,
                            actor="alice",
                        ),
                        adapter,
                        session,
                    )
                except CephApplyError as exc:
                    return str(exc.detail["reason"]), exc.detail.get("operation_run_id")
                return run.status, run.id

        try:
            outcomes = await asyncio.wait_for(
                asyncio.gather(attempt(), attempt()),
                timeout=10,
            )
            async with session_factory() as session:
                approval = await session.get(CephApprovalRecord, approval_id)
                assert approval is not None
                result = await session.exec(
                    select(CephOperationRunRecord).where(CephOperationRunRecord.plan_id == plan.id)
                )
                assert len(result.all()) == 1
                return outcomes, approval
        finally:
            await async_engine.dispose()

    outcomes, approval = asyncio.run(race())
    by_status = {status: run_id for status, run_id in outcomes}
    assert set(by_status) == {"completed", "approval_replayed"}
    assert by_status["completed"] is not None
    assert by_status["approval_replayed"] == by_status["completed"]
    assert approval.operation_run_id == by_status["completed"]
    assert approval.consumed_by == "alice"
    assert approval.consumed_at is not None
    assert adapter.calls == [plan.operations[0].id]


def test_sync_sqlite_race_consumes_one_approval_exactly_once(tmp_path) -> None:
    """Independent SQLite connections prove the conditional update is atomic locally."""

    database_path = tmp_path / "ceph-approval-sync-race.db"
    sync_engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    plan = _canonical_plan()
    raw_token = secrets.token_urlsafe(48)
    approval_id = str(uuid4())
    _seed_control_plane(
        sync_engine,
        plan=plan,
        raw_token=raw_token,
        approval_id=approval_id,
    )

    adapter = _CountingAdapter()
    ready = Barrier(2)

    def attempt() -> tuple[str, str | None]:
        with Session(sync_engine) as session:
            persisted = asyncio.run(load_persisted_plan(session, plan.id))
            ready.wait(timeout=5)
            try:
                run = asyncio.run(
                    apply_plan(
                        persisted,
                        ApplyRequest(
                            plan_id=plan.id,
                            endpoint_id=plan.endpoint_id,
                            approval_token=raw_token,
                            actor="alice",
                        ),
                        adapter,
                        session,
                    )
                )
            except CephApplyError as exc:
                return str(exc.detail["reason"]), exc.detail.get("operation_run_id")
            return run.status, run.id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = [
                future.result(timeout=10) for future in [pool.submit(attempt) for _ in range(2)]
            ]
        with Session(sync_engine) as session:
            approval = session.get(CephApprovalRecord, approval_id)
            assert approval is not None
            runs = session.exec(
                select(CephOperationRunRecord).where(CephOperationRunRecord.plan_id == plan.id)
            ).all()
    finally:
        sync_engine.dispose()

    by_status = {status: run_id for status, run_id in outcomes}
    assert set(by_status) == {"completed", "approval_replayed"}
    assert by_status["completed"] is not None
    assert by_status["approval_replayed"] == by_status["completed"]
    assert len(runs) == 1
    assert approval.operation_run_id == by_status["completed"]
    assert approval.consumed_by == "alice"
    assert approval.consumed_at is not None
    assert adapter.calls == [plan.operations[0].id]


class _SimulatedCrash(BaseException):
    """Represents abrupt process loss outside normal exception handling."""


class _InterruptingAdapter:
    def __init__(self, failure: BaseException, *, after_submission: bool = False) -> None:
        self.failure = failure
        self.after_submission = after_submission
        self.calls = 0

    async def apply(
        self,
        operation: ProviderOperation,
        *,
        confirm_destructive: bool,
    ) -> dict[str, Any]:
        assert operation.target_ref == "rbd"
        assert confirm_destructive is True
        self.calls += 1
        if not self.after_submission:
            raise self.failure
        return {"upid": INTERRUPT_UPID, "node": "node1"}

    async def wait_for_terminal(self, node: str, upid: str) -> dict[str, str]:
        assert node == "node1"
        assert upid == INTERRUPT_UPID
        raise self.failure


def _interrupted_run(
    tmp_path: Any,
    adapter: _InterruptingAdapter,
) -> tuple[CephOperationRunRecord, list[CephOperationEventRecord]]:
    database_path = tmp_path / f"ceph-interrupt-{uuid4().hex}.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    plan = _canonical_plan()
    raw_token = secrets.token_urlsafe(48)
    _seed_control_plane(
        sync_engine,
        plan=plan,
        raw_token=raw_token,
        approval_id=str(uuid4()),
    )
    try:
        with Session(sync_engine) as session:
            persisted = asyncio.run(load_persisted_plan(session, plan.id))
            with pytest.raises(type(adapter.failure)):
                asyncio.run(
                    apply_plan(
                        persisted,
                        ApplyRequest(
                            plan_id=plan.id,
                            endpoint_id=plan.endpoint_id,
                            approval_token=raw_token,
                            actor="alice",
                        ),
                        adapter,
                        session,
                    )
                )
        with Session(sync_engine) as session:
            run = session.exec(
                select(CephOperationRunRecord).where(CephOperationRunRecord.plan_id == plan.id)
            ).one()
            events = list(
                session.exec(
                    select(CephOperationEventRecord)
                    .where(CephOperationEventRecord.run_id == run.id)
                    .order_by(CephOperationEventRecord.sequence)
                ).all()
            )
            session.expunge(run)
            for item in events:
                session.expunge(item)
            return run, events
    finally:
        sync_engine.dispose()


def test_hard_crash_leaves_nonterminal_leased_dispatching_intent(tmp_path) -> None:
    adapter = _InterruptingAdapter(_SimulatedCrash("hard crash"))
    run, events = _interrupted_run(tmp_path, adapter)

    assert adapter.calls == 1
    assert run.status == "dispatching"
    assert run.lease_owner is not None
    assert run.lease_expires_at is not None and run.lease_expires_at > time.time()
    assert [item.event for item in events] == ["approval_consumed", "dispatch_intent"]
    assert events[-1].status == "dispatching"


def test_dispatch_cancellation_persists_outcome_unknown_checkpoint(tmp_path) -> None:
    adapter = _InterruptingAdapter(asyncio.CancelledError())
    run, events = _interrupted_run(tmp_path, adapter)

    assert run.status == "outcome_unknown"
    assert [item.event for item in events] == [
        "approval_consumed",
        "dispatch_intent",
        "dispatch_cancelled",
    ]


def test_task_poll_cancellation_persists_submitted_and_unknown_checkpoints(tmp_path) -> None:
    adapter = _InterruptingAdapter(asyncio.CancelledError(), after_submission=True)
    run, events = _interrupted_run(tmp_path, adapter)

    assert run.status == "outcome_unknown"
    assert run.provider_task_refs == [INTERRUPT_UPID]
    assert [item.event for item in events] == [
        "approval_consumed",
        "dispatch_intent",
        "provider_task_submitted",
        "provider_task_poll_cancelled",
    ]


@pytest.mark.parametrize(
    "endpoint_ids",
    [(17, 17), (17, 18)],
    ids=("same-endpoint", "cross-endpoint"),
)
def test_task_reference_claim_is_durable_across_sequential_runs(
    tmp_path,
    endpoint_ids: tuple[int, int],
) -> None:
    database_path = tmp_path / "ceph-task-claim-sequential.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    plans = [_canonical_plan(endpoint_id) for endpoint_id in endpoint_ids]
    tokens = [secrets.token_urlsafe(48), secrets.token_urlsafe(48)]
    for plan, token in zip(plans, tokens, strict=True):
        _seed_control_plane(
            sync_engine,
            plan=plan,
            raw_token=token,
            approval_id=str(uuid4()),
        )

    outcomes = []
    try:
        for plan, token in zip(plans, tokens, strict=True):
            with Session(sync_engine) as session:
                persisted = asyncio.run(load_persisted_plan(session, plan.id))
                outcomes.append(
                    asyncio.run(
                        apply_plan(
                            persisted,
                            ApplyRequest(
                                plan_id=plan.id,
                                endpoint_id=plan.endpoint_id,
                                approval_token=token,
                                actor="alice",
                            ),
                            _CountingAdapter(),
                            session,
                        )
                    )
                )
        with Session(sync_engine) as session:
            claims = list(session.exec(select(CephProviderTaskClaimRecord)).all())
    finally:
        sync_engine.dispose()

    assert [run.status for run in outcomes] == ["completed", "outcome_unknown"]
    assert outcomes[0].provider_task_refs == [ATOMIC_UPID]
    assert outcomes[1].provider_task_refs == []
    assert outcomes[1].events[-1].code == "provider_task_reference_reused"
    assert len(claims) == 1
    assert claims[0].run_id == outcomes[0].id


def test_sdk_proven_synchronous_pair_completes_through_full_engine(tmp_path) -> None:
    database_path = tmp_path / "ceph-sync-full-engine.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    plan = _canonical_plan()
    plan.operations = [
        ProviderOperation(
            id=str(uuid4()),
            provider="proxmox",
            kind="flag",
            target_ref="noout",
            action="create",
            node="node1",
        )
    ]
    plan.digest = canonical_plan_digest(plan)
    raw_token = secrets.token_urlsafe(48)
    _seed_control_plane(
        sync_engine,
        plan=plan,
        raw_token=raw_token,
        approval_id=str(uuid4()),
    )

    class _SynchronousWrite:
        async def flag_set(self, flag: str) -> None:
            assert flag == "noout"

    class _FullEngineAdapter(ProxmoxCephProviderAdapter):
        async def apply(
            self,
            operation: ProviderOperation,
            *,
            confirm_destructive: bool,
        ) -> dict[str, Any]:
            result = await execute_operation(
                _SynchronousWrite(),
                operation,
                "node1",
                confirm_destructive=confirm_destructive,
            )
            return {**result, "node": "node1"}

    try:
        with Session(sync_engine) as session:
            persisted = asyncio.run(load_persisted_plan(session, plan.id))
            run = asyncio.run(
                apply_plan(
                    persisted,
                    ApplyRequest(
                        plan_id=plan.id,
                        endpoint_id=plan.endpoint_id,
                        approval_token=raw_token,
                        actor="alice",
                    ),
                    _FullEngineAdapter(),
                    session,
                )
            )
            claims = list(session.exec(select(CephProviderTaskClaimRecord)).all())
    finally:
        sync_engine.dispose()

    assert run.status == "completed"
    assert run.provider_task_refs == []
    assert [event.event for event in run.events] == [
        "approval_consumed",
        "dispatch_intent",
        "dispatch_completed",
        "run_completed",
    ]
    assert run.events[-2].payload["result"]["completion_mode"] == "synchronous"
    assert claims == []


def test_task_based_none_result_is_unknown_through_full_engine(tmp_path) -> None:
    database_path = tmp_path / "ceph-task-none-full-engine.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    plan = _canonical_plan()
    raw_token = secrets.token_urlsafe(48)
    _seed_control_plane(
        sync_engine,
        plan=plan,
        raw_token=raw_token,
        approval_id=str(uuid4()),
    )

    class _TaskWriteReturningNone:
        async def pool_create(self, _node: str, _name: str, **_kwargs: Any) -> None:
            return None

    class _FullEngineAdapter(ProxmoxCephProviderAdapter):
        async def apply(
            self,
            operation: ProviderOperation,
            *,
            confirm_destructive: bool,
        ) -> dict[str, Any]:
            result = await execute_operation(
                _TaskWriteReturningNone(),
                operation,
                "node1",
                confirm_destructive=confirm_destructive,
            )
            return {**result, "node": "node1"}

    try:
        with Session(sync_engine) as session:
            persisted = asyncio.run(load_persisted_plan(session, plan.id))
            run = asyncio.run(
                apply_plan(
                    persisted,
                    ApplyRequest(
                        plan_id=plan.id,
                        endpoint_id=plan.endpoint_id,
                        approval_token=raw_token,
                        actor="alice",
                    ),
                    _FullEngineAdapter(),
                    session,
                )
            )
    finally:
        sync_engine.dispose()

    assert run.status == "outcome_unknown"
    assert run.provider_task_refs == []
    assert run.events[-1].code == "provider_task_reference_invalid"


@pytest.mark.parametrize(
    "endpoint_ids",
    [(17, 17), (17, 18)],
    ids=("same-endpoint", "cross-endpoint"),
)
def test_task_reference_claim_is_atomic_across_concurrent_runs(
    tmp_path,
    endpoint_ids: tuple[int, int],
) -> None:
    database_path = tmp_path / "ceph-task-claim-concurrent.db"
    sync_engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    plans = [_canonical_plan(endpoint_id) for endpoint_id in endpoint_ids]
    tokens = [secrets.token_urlsafe(48), secrets.token_urlsafe(48)]
    for plan, token in zip(plans, tokens, strict=True):
        _seed_control_plane(
            sync_engine,
            plan=plan,
            raw_token=token,
            approval_id=str(uuid4()),
        )

    dispatch_barrier = Barrier(2)

    class _SameTaskAdapter:
        def __init__(self) -> None:
            self.polls: list[str] = []

        async def apply(
            self,
            _operation: ProviderOperation,
            *,
            confirm_destructive: bool,
        ) -> dict[str, Any]:
            assert confirm_destructive is True
            dispatch_barrier.wait(timeout=5)
            return {"provider_task_ref": ATOMIC_UPID, "node": "node1"}

        async def wait_for_terminal(self, _node: str, upid: str) -> dict[str, str]:
            self.polls.append(upid)
            return {"state": "completed", "code": "provider_task_completed"}

    adapter = _SameTaskAdapter()

    def attempt(plan: PlanResponse, token: str):
        with Session(sync_engine) as session:
            persisted = asyncio.run(load_persisted_plan(session, plan.id))
            return asyncio.run(
                apply_plan(
                    persisted,
                    ApplyRequest(
                        plan_id=plan.id,
                        endpoint_id=plan.endpoint_id,
                        approval_token=token,
                        actor="alice",
                    ),
                    adapter,  # type: ignore[arg-type]
                    session,
                )
            )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(attempt, plan, token) for plan, token in zip(plans, tokens, strict=True)
            ]
            outcomes = [future.result(timeout=10) for future in futures]
        with Session(sync_engine) as session:
            claims = list(session.exec(select(CephProviderTaskClaimRecord)).all())
    finally:
        sync_engine.dispose()

    assert sorted(run.status for run in outcomes) == ["completed", "outcome_unknown"]
    assert len(claims) == 1
    assert adapter.polls == [ATOMIC_UPID]
    loser = next(run for run in outcomes if run.status == "outcome_unknown")
    assert loser.provider_task_refs == []
    assert loser.events[-1].code == "provider_task_reference_reused"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [2, 3], ids=("double-cancel", "triple-cancel"))
async def test_cancellation_during_atomic_task_claim_retains_submitted_upid(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_count: int,
) -> None:
    from proxbox_api.ceph import v2_engine as engine_module

    database_path = tmp_path / "ceph-task-claim-cancel.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    plan = _canonical_plan()
    raw_token = secrets.token_urlsafe(48)
    _seed_control_plane(
        sync_engine,
        plan=plan,
        raw_token=raw_token,
        approval_id=str(uuid4()),
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    original = engine_module._claim_and_checkpoint_provider_task

    async def paused_claim(*args: Any, **kwargs: Any):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(engine_module, "_claim_and_checkpoint_provider_task", paused_claim)
    try:
        with Session(sync_engine) as session:
            persisted = await load_persisted_plan(session, plan.id)
            task = asyncio.create_task(
                apply_plan(
                    persisted,
                    ApplyRequest(
                        plan_id=plan.id,
                        endpoint_id=plan.endpoint_id,
                        approval_token=raw_token,
                        actor="alice",
                    ),
                    _CountingAdapter(),
                    session,
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=2)
            await _cancel_repeatedly_while_inner_task_is_blocked(task, cancel_count)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            session.expire_all()
            run = session.exec(
                select(CephOperationRunRecord).where(CephOperationRunRecord.plan_id == plan.id)
            ).one()
            events = list(
                session.exec(
                    select(CephOperationEventRecord)
                    .where(CephOperationEventRecord.run_id == run.id)
                    .order_by(CephOperationEventRecord.sequence)
                ).all()
            )
            claims = list(session.exec(select(CephProviderTaskClaimRecord)).all())
    finally:
        sync_engine.dispose()

    assert run.status == "outcome_unknown"
    assert run.provider_task_refs == [ATOMIC_UPID]
    assert [item.event for item in events] == [
        "approval_consumed",
        "dispatch_intent",
        "provider_task_submitted",
        "provider_task_poll_cancelled",
    ]
    assert len(claims) == 1
    assert claims[0].run_id == run.id


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [2, 3], ids=("double-cancel", "triple-cancel"))
async def test_repeated_cancellation_cannot_interrupt_cancellation_checkpoint(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_count: int,
) -> None:
    from proxbox_api.ceph import v2_engine as engine_module

    database_path = tmp_path / f"ceph-cancel-checkpoint-{cancel_count}.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    plan = _canonical_plan()
    raw_token = secrets.token_urlsafe(48)
    _seed_control_plane(
        sync_engine,
        plan=plan,
        raw_token=raw_token,
        approval_id=str(uuid4()),
    )
    claim_entered = asyncio.Event()
    claim_release = asyncio.Event()
    cancel_checkpoint_entered = asyncio.Event()
    cancel_checkpoint_release = asyncio.Event()
    original_claim = engine_module._claim_and_checkpoint_provider_task
    original_append = engine_module._append_event_checkpoint

    async def paused_claim(*args: Any, **kwargs: Any):
        claim_entered.set()
        await claim_release.wait()
        return await original_claim(*args, **kwargs)

    async def paused_cancel_checkpoint(*args: Any, **kwargs: Any):
        if kwargs.get("event") == "provider_task_poll_cancelled":
            cancel_checkpoint_entered.set()
            await cancel_checkpoint_release.wait()
        return await original_append(*args, **kwargs)

    monkeypatch.setattr(engine_module, "_claim_and_checkpoint_provider_task", paused_claim)
    monkeypatch.setattr(engine_module, "_append_event_checkpoint", paused_cancel_checkpoint)
    try:
        with Session(sync_engine) as session:
            persisted = await load_persisted_plan(session, plan.id)
            task = asyncio.create_task(
                apply_plan(
                    persisted,
                    ApplyRequest(
                        plan_id=plan.id,
                        endpoint_id=plan.endpoint_id,
                        approval_token=raw_token,
                        actor="alice",
                    ),
                    _CountingAdapter(),
                    session,
                )
            )
            await asyncio.wait_for(claim_entered.wait(), timeout=2)
            await _cancel_repeatedly_while_inner_task_is_blocked(task, 1)
            claim_release.set()
            await asyncio.wait_for(cancel_checkpoint_entered.wait(), timeout=2)
            await _cancel_repeatedly_while_inner_task_is_blocked(task, cancel_count - 1)
            cancel_checkpoint_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            session.expire_all()
            run = session.exec(
                select(CephOperationRunRecord).where(CephOperationRunRecord.plan_id == plan.id)
            ).one()
            events = list(
                session.exec(
                    select(CephOperationEventRecord)
                    .where(CephOperationEventRecord.run_id == run.id)
                    .order_by(CephOperationEventRecord.sequence)
                ).all()
            )
    finally:
        claim_release.set()
        cancel_checkpoint_release.set()
        sync_engine.dispose()

    assert run.status == "outcome_unknown"
    assert run.provider_task_refs == [ATOMIC_UPID]
    assert [item.event for item in events] == [
        "approval_consumed",
        "dispatch_intent",
        "provider_task_submitted",
        "provider_task_poll_cancelled",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [2, 3], ids=("double-cancel", "triple-cancel"))
async def test_cancellation_while_sdk_returns_task_is_deferred_until_claim(
    tmp_path,
    cancel_count: int,
) -> None:
    database_path = tmp_path / "ceph-sdk-task-result-cancel.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    plan = _canonical_plan()
    raw_token = secrets.token_urlsafe(48)
    _seed_control_plane(
        sync_engine,
        plan=plan,
        raw_token=raw_token,
        approval_id=str(uuid4()),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    class _ReturningTaskAdapter:
        async def apply(
            self,
            _operation: ProviderOperation,
            *,
            confirm_destructive: bool,
        ) -> dict[str, Any]:
            assert confirm_destructive is True
            entered.set()
            await release.wait()
            return {"provider_task_ref": ATOMIC_UPID, "node": "node1"}

        async def wait_for_terminal(self, _node: str, _upid: str) -> dict[str, str]:
            pytest.fail("a deferred cancellation must stop before task polling")

    try:
        with Session(sync_engine) as session:
            persisted = await load_persisted_plan(session, plan.id)
            task = asyncio.create_task(
                apply_plan(
                    persisted,
                    ApplyRequest(
                        plan_id=plan.id,
                        endpoint_id=plan.endpoint_id,
                        approval_token=raw_token,
                        actor="alice",
                    ),
                    _ReturningTaskAdapter(),  # type: ignore[arg-type]
                    session,
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=2)
            await _cancel_repeatedly_while_inner_task_is_blocked(task, cancel_count)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            session.expire_all()
            run = session.exec(
                select(CephOperationRunRecord).where(CephOperationRunRecord.plan_id == plan.id)
            ).one()
            events = list(
                session.exec(
                    select(CephOperationEventRecord)
                    .where(CephOperationEventRecord.run_id == run.id)
                    .order_by(CephOperationEventRecord.sequence)
                ).all()
            )
            claims = list(session.exec(select(CephProviderTaskClaimRecord)).all())
    finally:
        sync_engine.dispose()

    assert run.status == "outcome_unknown"
    assert run.provider_task_refs == [ATOMIC_UPID]
    assert [event.event for event in events] == [
        "approval_consumed",
        "dispatch_intent",
        "provider_task_submitted",
        "provider_task_poll_cancelled",
    ]
    assert len(claims) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [2, 3], ids=("double-cancel", "triple-cancel"))
async def test_cancellation_while_sdk_returns_sync_is_deferred_until_checkpoint(
    tmp_path,
    cancel_count: int,
) -> None:
    database_path = tmp_path / "ceph-sdk-sync-result-cancel.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    plan = _canonical_plan()
    plan.operations = [
        ProviderOperation(
            id=str(uuid4()),
            provider="proxmox",
            kind="flag",
            target_ref="noout",
            action="create",
            node="node1",
        )
    ]
    plan.digest = canonical_plan_digest(plan)
    raw_token = secrets.token_urlsafe(48)
    _seed_control_plane(
        sync_engine,
        plan=plan,
        raw_token=raw_token,
        approval_id=str(uuid4()),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    class _ReturningSynchronousAdapter(ProxmoxCephProviderAdapter):
        async def apply(
            self,
            _operation: ProviderOperation,
            *,
            confirm_destructive: bool,
        ) -> dict[str, Any]:
            assert confirm_destructive is True
            entered.set()
            await release.wait()
            return {
                "result": "completed",
                "completion_mode": "synchronous",
                "node": "node1",
            }

    try:
        with Session(sync_engine) as session:
            persisted = await load_persisted_plan(session, plan.id)
            task = asyncio.create_task(
                apply_plan(
                    persisted,
                    ApplyRequest(
                        plan_id=plan.id,
                        endpoint_id=plan.endpoint_id,
                        approval_token=raw_token,
                        actor="alice",
                    ),
                    _ReturningSynchronousAdapter(),
                    session,
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=2)
            await _cancel_repeatedly_while_inner_task_is_blocked(task, cancel_count)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            session.expire_all()
            run = session.exec(
                select(CephOperationRunRecord).where(CephOperationRunRecord.plan_id == plan.id)
            ).one()
            events = list(
                session.exec(
                    select(CephOperationEventRecord)
                    .where(CephOperationEventRecord.run_id == run.id)
                    .order_by(CephOperationEventRecord.sequence)
                ).all()
            )
    finally:
        sync_engine.dispose()

    assert run.status == "outcome_unknown"
    assert run.provider_task_refs == []
    assert [event.event for event in events] == [
        "approval_consumed",
        "dispatch_intent",
        "dispatch_completed",
        "synchronous_completion_cancelled",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [2, 3], ids=("double-cancel", "triple-cancel"))
async def test_cancellation_during_synchronous_checkpoint_retains_completion(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_count: int,
) -> None:
    from proxbox_api.ceph import v2_engine as engine_module

    database_path = tmp_path / "ceph-sync-completion-cancel.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    plan = _canonical_plan()
    plan.operations = [
        ProviderOperation(
            id=str(uuid4()),
            provider="proxmox",
            kind="flag",
            target_ref="noout",
            action="create",
            node="node1",
        )
    ]
    plan.digest = canonical_plan_digest(plan)
    raw_token = secrets.token_urlsafe(48)
    _seed_control_plane(
        sync_engine,
        plan=plan,
        raw_token=raw_token,
        approval_id=str(uuid4()),
    )

    class _SynchronousAdapter:
        async def apply(
            self,
            _operation: ProviderOperation,
            *,
            confirm_destructive: bool,
        ) -> dict[str, Any]:
            assert confirm_destructive is True
            return {
                "result": "completed",
                "completion_mode": "synchronous",
                "node": "node1",
            }

        def declares_synchronous_success(
            self,
            operation: ProviderOperation,
            result: dict[str, Any],
        ) -> bool:
            return bool(
                f"{operation.kind}:{operation.action}" == "flag:create"
                and result.get("completion_mode") == "synchronous"
            )

    entered = asyncio.Event()
    release = asyncio.Event()
    original = engine_module._append_synchronous_completion

    async def paused_completion(*args: Any, **kwargs: Any):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(engine_module, "_append_synchronous_completion", paused_completion)
    try:
        with Session(sync_engine) as session:
            persisted = await load_persisted_plan(session, plan.id)
            task = asyncio.create_task(
                apply_plan(
                    persisted,
                    ApplyRequest(
                        plan_id=plan.id,
                        endpoint_id=plan.endpoint_id,
                        approval_token=raw_token,
                        actor="alice",
                    ),
                    _SynchronousAdapter(),  # type: ignore[arg-type]
                    session,
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=2)
            await _cancel_repeatedly_while_inner_task_is_blocked(task, cancel_count)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            session.expire_all()
            run = session.exec(
                select(CephOperationRunRecord).where(CephOperationRunRecord.plan_id == plan.id)
            ).one()
            events = list(
                session.exec(
                    select(CephOperationEventRecord)
                    .where(CephOperationEventRecord.run_id == run.id)
                    .order_by(CephOperationEventRecord.sequence)
                ).all()
            )
    finally:
        sync_engine.dispose()

    assert run.status == "outcome_unknown"
    assert run.provider_task_refs == []
    assert [item.event for item in events] == [
        "approval_consumed",
        "dispatch_intent",
        "dispatch_completed",
        "synchronous_completion_cancelled",
    ]
    assert events[-2].payload["result"]["completion_mode"] == "synchronous"


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_count", [2, 3], ids=("double-cancel", "triple-cancel"))
async def test_repeated_cancellation_waits_for_provider_task_completion_checkpoint(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_count: int,
) -> None:
    from proxbox_api.ceph import v2_engine as engine_module

    database_path = tmp_path / f"ceph-task-completion-cancel-{cancel_count}.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    plan = _canonical_plan()
    raw_token = secrets.token_urlsafe(48)
    _seed_control_plane(
        sync_engine,
        plan=plan,
        raw_token=raw_token,
        approval_id=str(uuid4()),
    )
    completion_entered = asyncio.Event()
    completion_release = asyncio.Event()
    original_append = engine_module._append_event_checkpoint

    async def paused_task_completion(*args: Any, **kwargs: Any):
        if kwargs.get("event") == "provider_task_completed":
            completion_entered.set()
            await completion_release.wait()
        return await original_append(*args, **kwargs)

    monkeypatch.setattr(engine_module, "_append_event_checkpoint", paused_task_completion)
    try:
        with Session(sync_engine) as session:
            persisted = await load_persisted_plan(session, plan.id)
            task = asyncio.create_task(
                apply_plan(
                    persisted,
                    ApplyRequest(
                        plan_id=plan.id,
                        endpoint_id=plan.endpoint_id,
                        approval_token=raw_token,
                        actor="alice",
                    ),
                    _CountingAdapter(),
                    session,
                )
            )
            await asyncio.wait_for(completion_entered.wait(), timeout=2)
            await _cancel_repeatedly_while_inner_task_is_blocked(task, cancel_count)
            completion_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            session.expire_all()
            run = session.exec(
                select(CephOperationRunRecord).where(CephOperationRunRecord.plan_id == plan.id)
            ).one()
            events = list(
                session.exec(
                    select(CephOperationEventRecord)
                    .where(CephOperationEventRecord.run_id == run.id)
                    .order_by(CephOperationEventRecord.sequence)
                ).all()
            )
    finally:
        completion_release.set()
        sync_engine.dispose()

    assert run.status == "outcome_unknown"
    assert run.provider_task_refs == [ATOMIC_UPID]
    assert [item.event for item in events] == [
        "approval_consumed",
        "dispatch_intent",
        "provider_task_submitted",
        "provider_task_completed",
        "provider_task_completion_cancelled",
    ]


@pytest.mark.parametrize(
    ("crash_point", "prior_event", "task_refs"),
    [
        ("after_consumption", "approval_consumed", []),
        ("after_upid_persistence", "provider_task_submitted", ["UPID:durable-task"]),
        ("after_terminal_observation", "provider_task_completed", ["UPID:durable-task"]),
        ("between_operations", "operation_completed", ["UPID:first-operation"]),
    ],
)
def test_expired_run_lease_recovers_every_crash_checkpoint_as_outcome_unknown(
    tmp_path,
    crash_point: str,
    prior_event: str,
    task_refs: list[str],
) -> None:
    database_path = tmp_path / f"ceph-stale-{crash_point}.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    SQLModel.metadata.create_all(sync_engine)
    now = time.time()
    run_id = str(uuid4())
    with Session(sync_engine) as session:
        session.add(
            CephOperationRunRecord(
                id=run_id,
                plan_id="plan-crash-injection",
                endpoint_id=17,
                endpoint_config_revision="b" * 64,
                plan_digest="c" * 64,
                requester="alice",
                approver="bob",
                approval_id="approval-crash-injection",
                status="running",
                actor="alice",
                provider="proxmox",
                request_summary={"crash_point": crash_point},
                provider_task_refs=task_refs,
                created_at=now - 20,
                updated_at=now - 10,
                lease_expires_at=now - 1,
                result_summary={"completed": 1 if "terminal" in crash_point else 0},
            )
        )
        session.add(
            CephOperationEventRecord(
                run_id=run_id,
                sequence=0,
                event=prior_event,
                status="running",
                code=prior_event,
                message="Durable crash-injection checkpoint.",
                provider_task_ref=task_refs[-1] if task_refs else None,
                created_at=now - 10,
            )
        )
        session.commit()
        record = session.get(CephOperationRunRecord, run_id)
        assert record is not None
        operation = asyncio.run(recover_stale_operation_run(session, record))
        events = session.exec(
            select(CephOperationEventRecord)
            .where(CephOperationEventRecord.run_id == run_id)
            .order_by(CephOperationEventRecord.sequence)
        ).all()

    sync_engine.dispose()
    assert operation.status == "outcome_unknown"
    assert operation.provider_task_refs == task_refs
    assert operation.lease_expires_at is None
    assert operation.lease_owner is None
    assert operation.result_summary["reason"] == "run_lease_expired"
    assert "Never replay" in operation.result_summary["recovery"]["action"]
    assert [item.event for item in events] == [prior_event, "run_lease_expired"]


def test_stale_lease_owner_cannot_append_or_terminalize_live_run(tmp_path) -> None:
    database_path = tmp_path / "ceph-owner-cas.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    SQLModel.metadata.create_all(sync_engine)
    now = time.time()
    run_id = str(uuid4())
    with Session(sync_engine) as session:
        session.add(
            CephOperationRunRecord(
                id=run_id,
                status="dispatching",
                provider="proxmox",
                lease_owner="current-owner",
                lease_expires_at=now + 60,
            )
        )
        session.commit()

        stale = CephOperationRunRecord(
            id=run_id,
            status="dispatching",
            provider="proxmox",
            lease_owner="stale-owner",
            lease_expires_at=now + 60,
        )
        with pytest.raises(_CephRunLeaseLost):
            asyncio.run(
                _append_event_checkpoint(
                    session,
                    stale,
                    event="late_worker",
                    status="completed",
                    code="late_worker",
                    message="late worker must lose",
                )
            )
        current = session.get(CephOperationRunRecord, run_id)
        assert current is not None
        assert current.status == "dispatching"
        assert current.lease_owner == "current-owner"
        assert (
            session.exec(
                select(CephOperationEventRecord).where(CephOperationEventRecord.run_id == run_id)
            ).all()
            == []
        )
    sync_engine.dispose()


def test_non_noop_proxmox_result_without_valid_upid_is_outcome_unknown(tmp_path) -> None:
    database_path = tmp_path / "ceph-invalid-upid.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    plan = _canonical_plan()
    raw_token = secrets.token_urlsafe(48)
    _seed_control_plane(
        sync_engine,
        plan=plan,
        raw_token=raw_token,
        approval_id=str(uuid4()),
    )

    class _NoUpidAdapter:
        async def apply(
            self,
            _operation: ProviderOperation,
            *,
            confirm_destructive: bool,
        ) -> dict[str, Any]:
            assert confirm_destructive is True
            return {"provider_task_ref": "TASK:not-a-proxmox-upid", "node": "node1"}

    try:
        with Session(sync_engine) as session:
            persisted = asyncio.run(load_persisted_plan(session, plan.id))
            operation = asyncio.run(
                apply_plan(
                    persisted,
                    ApplyRequest(
                        plan_id=plan.id,
                        endpoint_id=plan.endpoint_id,
                        approval_token=raw_token,
                        actor="alice",
                    ),
                    _NoUpidAdapter(),  # type: ignore[arg-type]
                    session,
                )
            )
    finally:
        sync_engine.dispose()

    assert operation.status == "outcome_unknown"
    assert operation.provider_task_refs == []
    assert operation.events[-1].code == "provider_task_reference_invalid"


def test_malformed_terminal_state_cannot_be_promoted_to_success(tmp_path) -> None:
    database_path = tmp_path / "ceph-invalid-terminal-state.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    plan = _canonical_plan()
    raw_token = secrets.token_urlsafe(48)
    _seed_control_plane(
        sync_engine,
        plan=plan,
        raw_token=raw_token,
        approval_id=str(uuid4()),
    )

    class _MalformedTerminalAdapter:
        async def apply(
            self,
            _operation: ProviderOperation,
            *,
            confirm_destructive: bool,
        ) -> dict[str, Any]:
            assert confirm_destructive is True
            return {"provider_task_ref": ATOMIC_UPID, "node": "node1"}

        async def wait_for_terminal(self, _node: str, _upid: str) -> dict[str, str]:
            return {"state": "success", "code": "password=terminal-canary"}

    try:
        with Session(sync_engine) as session:
            persisted = asyncio.run(load_persisted_plan(session, plan.id))
            operation = asyncio.run(
                apply_plan(
                    persisted,
                    ApplyRequest(
                        plan_id=plan.id,
                        endpoint_id=plan.endpoint_id,
                        approval_token=raw_token,
                        actor="alice",
                    ),
                    _MalformedTerminalAdapter(),  # type: ignore[arg-type]
                    session,
                )
            )
    finally:
        sync_engine.dispose()

    assert operation.status == "outcome_unknown"
    assert operation.events[-1].code == "provider_task_status_invalid"
    assert "terminal-canary" not in repr(operation.model_dump(mode="json"))


def test_late_terminal_result_cannot_resurrect_an_expired_run_lease(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "ceph-expired-worker.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    plan = _canonical_plan()
    raw_token = secrets.token_urlsafe(48)
    _seed_control_plane(
        sync_engine,
        plan=plan,
        raw_token=raw_token,
        approval_id=str(uuid4()),
    )
    monkeypatch.setenv("PROXBOX_CEPH_RUN_LEASE_SECONDS", "1")

    class _LateTerminalAdapter:
        async def apply(
            self,
            _operation: ProviderOperation,
            *,
            confirm_destructive: bool,
        ) -> dict[str, Any]:
            assert confirm_destructive is True
            return {"provider_task_ref": ATOMIC_UPID, "node": "node1"}

        async def wait_for_terminal(self, _node: str, _upid: str) -> dict[str, str]:
            await asyncio.sleep(1.1)
            return {"state": "completed", "code": "provider_task_completed"}

    try:
        with Session(sync_engine) as session:
            persisted = asyncio.run(load_persisted_plan(session, plan.id))
            operation = asyncio.run(
                apply_plan(
                    persisted,
                    ApplyRequest(
                        plan_id=plan.id,
                        endpoint_id=plan.endpoint_id,
                        approval_token=raw_token,
                        actor="alice",
                    ),
                    _LateTerminalAdapter(),  # type: ignore[arg-type]
                    session,
                )
            )
    finally:
        sync_engine.dispose()

    assert operation.status == "outcome_unknown"
    assert operation.provider_task_refs == [ATOMIC_UPID]
    assert operation.events[-1].code == "run_lease_expired"
    assert all(event.code != "provider_task_completed" for event in operation.events)


def test_successful_apply_result_is_redacted_before_response_and_persistence(tmp_path) -> None:
    database_path = tmp_path / "ceph-secret-result.db"
    sync_engine = create_engine(f"sqlite:///{database_path}")
    event.listen(sync_engine, "connect", _apply_sqlite_pragmas)
    plan = _canonical_plan()
    raw_token = secrets.token_urlsafe(48)
    _seed_control_plane(
        sync_engine,
        plan=plan,
        raw_token=raw_token,
        approval_id=str(uuid4()),
    )
    canary = "https://operator:result-secret@pve.invalid?token=result-canary"

    class _SecretResultAdapter:
        async def apply(
            self,
            _operation: ProviderOperation,
            *,
            confirm_destructive: bool,
        ) -> dict[str, Any]:
            assert confirm_destructive is True
            return {
                "password": "plain-result-secret",
                "endpoint": canary,
                "nested": {"access_token": "raw-result-token"},
                "provider_task_ref": ATOMIC_UPID,
                "node": "node1",
            }

        async def wait_for_terminal(self, _node: str, _upid: str) -> dict[str, str]:
            return {"state": "completed", "code": "provider_task_completed"}

    try:
        with Session(sync_engine) as session:
            persisted = asyncio.run(load_persisted_plan(session, plan.id))
            run = asyncio.run(
                apply_plan(
                    persisted,
                    ApplyRequest(
                        plan_id=plan.id,
                        endpoint_id=plan.endpoint_id,
                        approval_token=raw_token,
                        actor="alice",
                    ),
                    _SecretResultAdapter(),  # type: ignore[arg-type]
                    session,
                )
            )
            event_rows = list(
                session.exec(
                    select(CephOperationEventRecord).where(
                        CephOperationEventRecord.run_id == run.id
                    )
                ).all()
            )
            record = session.get(CephOperationRunRecord, run.id)
            assert record is not None
            serialized = repr((run.model_dump(mode="json"), record, event_rows))
    finally:
        sync_engine.dispose()

    assert run.status == "completed"
    assert "plain-result-secret" not in serialized
    assert "result-secret" not in serialized
    assert "result-canary" not in serialized
    assert "raw-result-token" not in serialized
