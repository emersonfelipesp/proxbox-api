"""Security and durability tests for the Ceph v2 control plane (issue #258)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlmodel import Session, select

from proxbox_api.app.exceptions import register_exception_handlers
from proxbox_api.ceph import endpoint_binding
from proxbox_api.ceph import v2_routes as v2_routes_module
from proxbox_api.ceph.v2_engine import (
    CephApprovalError,
    approval_recovery_metadata,
    canonical_plan_digest,
)
from proxbox_api.ceph.v2_providers.base import CephWriteGateDenied
from proxbox_api.ceph.v2_routes import router as ceph_v2_router
from proxbox_api.ceph.v2_schemas import PlanResponse
from proxbox_api.database import (
    CephApprovalRecord,
    CephOperationEventRecord,
    CephOperationRunRecord,
    CephPlanRecord,
    ProxmoxEndpoint,
    get_async_session,
)
from proxbox_api.session.proxmox_core import ProxmoxSession, SensitiveString
from proxbox_api.session.proxmox_providers import proxmox_sessions_dep

REQUESTER = {"X-Proxbox-Actor": "alice"}
APPROVER = {"X-Proxbox-Actor": "bob"}
CREATE_UPID = "UPID:node1:00000001:00000002:00000003:cephcreate:rbd:root@pam:"
SECOND_CREATE_UPID = "UPID:node1:0000000A:0000000B:0000000C:cephcreate:second:root@pam:"
UPDATE_UPID = "UPID:node1:00000004:00000005:00000006:cephset:rbd:root@pam:"
DELETE_UPID = "UPID:node1:00000007:00000008:00000009:cephremove:rbd:root@pam:"
pytestmark = pytest.mark.asyncio(loop_scope="session")


class _FakeWrite:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.after_call: Any = None
        self.error: Exception | None = None
        self.result_override: str | None = None

    def _record(self, value: str) -> int:
        self.calls.append(value)
        if self.after_call is not None:
            self.after_call(len(self.calls))
        return len(self.calls)

    async def pool_create(self, node: str, name: str, **_kwargs: Any) -> str:
        call_number = self._record(f"create:{node}:{name}")
        if self.error is not None:
            raise self.error
        return self.result_override or (CREATE_UPID if call_number == 1 else SECOND_CREATE_UPID)

    async def pool_set(self, node: str, name: str, **_kwargs: Any) -> str:
        self._record(f"update:{node}:{name}")
        if self.error is not None:
            raise self.error
        return self.result_override or UPDATE_UPID

    async def pool_delete(
        self,
        node: str,
        name: str,
        *,
        confirm_destroy: bool = False,
        **_kwargs: Any,
    ) -> str:
        assert confirm_destroy is True
        self._record(f"delete:{node}:{name}")
        if self.error is not None:
            raise self.error
        return self.result_override or DELETE_UPID


class _FakeClusterRead:
    def __init__(self) -> None:
        self.error: BaseException | None = None

    async def status(self) -> dict[str, str]:
        if self.error is not None:
            raise self.error
        return {"health": "HEALTH_OK"}

    async def metadata(self) -> dict[str, str]:
        return {"version": "test"}

    async def flags(self) -> list[dict[str, str]]:
        return []


class _FakeNodeRead:
    async def osds(self, _node: str) -> list[dict[str, str]]:
        return []

    async def pools(self, _node: str) -> list[dict[str, str]]:
        return []

    async def filesystems(self, _node: str) -> list[dict[str, str]]:
        return []

    async def rules(self, _node: str) -> list[dict[str, str]]:
        return []


class _AsyncSessionFacade:
    """Tiny awaitable facade over the same SQLite semantics used in production."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, instance: Any) -> None:
        self._session.add(instance)

    async def exec(self, statement: Any) -> Any:
        return self._session.exec(statement)

    async def get(self, model: Any, identity: Any) -> Any:
        return self._session.get(model, identity)

    async def commit(self) -> None:
        self._session.commit()

    async def rollback(self) -> None:
        self._session.rollback()

    async def refresh(self, instance: Any) -> None:
        self._session.refresh(instance)


class _FakeBoundSession:
    """Connection-shape-compatible session created from one local DB schema."""

    def __init__(self, config: Any) -> None:
        self.db_endpoint_id = config.db_endpoint_id
        self.ip_address = config.ip_address
        self.domain = config.domain
        self.http_port = config.http_port
        self.user = config.user
        self.password = SensitiveString(config.password)
        self.token_name = config.token.name
        self.token_value = SensitiveString(config.token.value)
        self.ssl = config.ssl
        self.timeout = config.timeout if config.timeout is not None else 5
        self.connect_timeout = None
        self.max_retries = config.max_retries if config.max_retries is not None else 0
        self.retry_backoff = (
            float(config.retry_backoff) if config.retry_backoff is not None else 0.5
        )
        self.cluster_status = [{"name": "node1"}]
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class _Harness:
    client: Any
    endpoint: ProxmoxEndpoint
    px: Any
    calls: list[str]
    write: _FakeWrite
    app: FastAPI
    selected_endpoint_id: int
    created_sessions: list[_FakeBoundSession]
    task_status: Any
    cluster_read: _FakeClusterRead

    @property
    def endpoint_id(self) -> int:
        return self.selected_endpoint_id


@pytest_asyncio.fixture(loop_scope="session")
async def ceph_v2_harness(db_engine, db_session, monkeypatch) -> _Harness:
    endpoint = ProxmoxEndpoint(
        name="ceph-lab",
        ip_address="192.0.2.10",
        username="root@pam",
        enabled=True,
        allow_writes=True,
    )
    db_session.add(endpoint)
    db_session.commit()
    db_session.refresh(endpoint)
    assert endpoint.id is not None
    endpoint_id = endpoint.id
    db_session.commit()
    px = SimpleNamespace(db_endpoint_id=endpoint_id)
    db_session.commit()
    calls: list[str] = []
    write = _FakeWrite(calls)
    created_sessions: list[_FakeBoundSession] = []

    async def create_exact_session(config: Any) -> _FakeBoundSession:
        created = _FakeBoundSession(config)
        created_sessions.append(created)
        return created

    monkeypatch.setattr(ProxmoxSession, "create", create_exact_session)

    monkeypatch.setattr(
        "proxbox_api.ceph.v2_providers.proxmox.cephwrite_importable",
        lambda: True,
    )
    cluster_read = _FakeClusterRead()
    fake_client = SimpleNamespace(
        write=write,
        cluster=cluster_read,
        nodes=_FakeNodeRead(),
    )
    monkeypatch.setattr(
        "proxbox_api.ceph.v2_providers.proxmox._client_for",
        lambda _px: fake_client,
    )
    monkeypatch.setattr(
        "proxbox_api.ceph.v2_providers.proxmox._node_names",
        lambda _px: ["node1"],
    )
    task_status = SimpleNamespace(value={"status": "stopped", "exitstatus": "OK"})

    async def get_task_status(_px: Any, _node: str, _upid: str) -> Any:
        if isinstance(task_status.value, BaseException):
            raise task_status.value
        return task_status.value

    monkeypatch.setattr(
        "proxbox_api.ceph.v2_providers.proxmox.get_node_task_status",
        get_task_status,
    )

    async def override_get_async_session():
        with Session(db_engine) as session:
            yield _AsyncSessionFacade(session)

    async def override_proxmox_sessions():
        return [px]

    test_app = FastAPI()
    register_exception_handlers(test_app)
    test_app.include_router(ceph_v2_router, prefix="/ceph/v2")
    test_app.dependency_overrides[get_async_session] = override_get_async_session
    test_app.dependency_overrides[proxmox_sessions_dep] = override_proxmox_sessions
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        yield _Harness(
            client,
            endpoint,
            px,
            calls,
            write,
            test_app,
            endpoint_id,
            created_sessions,
            task_status,
            cluster_read,
        )


def _plan_body(endpoint_id: int, action: str = "create") -> dict[str, Any]:
    return {
        "provider": "proxmox",
        "endpoint_id": endpoint_id,
        "operations": [{"kind": "pool", "target_ref": "rbd", "action": action, "node": "node1"}],
        "netbox_branch_schema_id": "branch-123",
    }


async def _create_plan(harness: _Harness, action: str = "create") -> dict[str, Any]:
    response = await harness.client.post(
        "/ceph/v2/plans",
        json=_plan_body(harness.endpoint_id, action),
        headers=REQUESTER,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _approve(
    harness: _Harness, plan_id: str, headers: dict[str, str] = APPROVER
) -> dict[str, Any]:
    response = await harness.client.post(
        f"/ceph/v2/plans/{plan_id}/approvals",
        json={"endpoint_id": harness.endpoint_id},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _apply(harness: _Harness, plan_id: str, token: str):
    return await harness.client.post(
        f"/ceph/v2/plans/{plan_id}/apply",
        json={
            "plan_id": plan_id,
            "endpoint_id": harness.endpoint_id,
            "approval_token": token,
        },
        headers=REQUESTER,
    )


async def test_capabilities_are_endpoint_scoped_and_fail_closed(ceph_v2_harness):
    harness = ceph_v2_harness
    unscoped = await harness.client.get("/ceph/v2/capabilities", params={"provider": "proxmox"})
    assert unscoped.status_code == 200
    assert unscoped.json()["providers"][0]["apply"] is False

    scoped = await harness.client.get(
        "/ceph/v2/capabilities",
        params={"provider": "proxmox", "endpoint_id": harness.endpoint_id},
    )
    assert scoped.status_code == 200
    assert scoped.json()["providers"][0]["endpoint_id"] == harness.endpoint_id
    assert scoped.json()["providers"][0]["apply"] is True


async def test_ceph_write_execution_is_default_off_until_gateway_is_trusted(
    ceph_v2_harness,
    monkeypatch,
):
    harness = ceph_v2_harness
    plan = await _create_plan(harness)
    sessions_before = len(harness.created_sessions)
    monkeypatch.delenv("PROXBOX_ENABLE_CEPH_V2_WRITES", raising=False)
    monkeypatch.delenv("PROXBOX_CEPH_TRUSTED_ACTOR_GATEWAY", raising=False)

    capabilities = await harness.client.get(
        "/ceph/v2/capabilities",
        params={"provider": "proxmox", "endpoint_id": harness.endpoint_id},
    )
    assert capabilities.status_code == 200
    assert capabilities.json()["providers"][0]["apply"] is False
    assert len(harness.created_sessions) == sessions_before

    approval = await harness.client.post(
        f"/ceph/v2/plans/{plan['id']}/approvals",
        json={"endpoint_id": harness.endpoint_id},
        headers=APPROVER,
    )
    assert approval.status_code == 503
    assert approval.json()["detail"]["reason"] == "ceph_write_execution_disabled"

    apply = await _apply(harness, plan["id"], "not-consumed")
    assert apply.status_code == 503
    assert apply.json()["detail"]["reason"] == "ceph_write_execution_disabled"
    assert len(harness.created_sessions) == sessions_before


async def test_endpoint_revision_is_stable_and_bound_across_plan_and_approval(
    ceph_v2_harness,
    db_engine,
):
    harness = ceph_v2_harness
    first = await _create_plan(harness)
    second = await _create_plan(harness)
    revision = first["endpoint_config_revision"]
    assert revision == second["endpoint_config_revision"]
    assert len(revision) == 64

    approval = await _approve(harness, first["id"])
    assert approval["endpoint_config_revision"] == revision
    with Session(db_engine) as session:
        plan_record = session.get(CephPlanRecord, first["id"])
        approval_record = session.get(CephApprovalRecord, approval["id"])
        assert plan_record is not None and approval_record is not None
        assert plan_record.endpoint_config_revision == revision
        assert approval_record.endpoint_config_revision == revision


async def test_same_endpoint_id_retargeting_is_rejected_between_every_authority_phase(
    ceph_v2_harness,
    db_engine,
):
    harness = ceph_v2_harness
    plan = await _create_plan(harness)
    with Session(db_engine) as session:
        endpoint = session.get(ProxmoxEndpoint, harness.endpoint_id)
        assert endpoint is not None
        endpoint.domain = "retargeted-before-approval.invalid"
        session.add(endpoint)
        session.commit()

    rejected_approval = await harness.client.post(
        f"/ceph/v2/plans/{plan['id']}/approvals",
        json={"endpoint_id": harness.endpoint_id},
        headers=APPROVER,
    )
    assert rejected_approval.status_code == 409
    assert rejected_approval.json()["detail"]["reason"] == "endpoint_configuration_changed"

    fresh_plan = await _create_plan(harness)
    approval = await _approve(harness, fresh_plan["id"])
    with Session(db_engine) as session:
        endpoint = session.get(ProxmoxEndpoint, harness.endpoint_id)
        assert endpoint is not None
        endpoint.domain = "retargeted-before-apply.invalid"
        session.add(endpoint)
        session.commit()

    rejected_apply = await _apply(harness, fresh_plan["id"], approval["token"])
    assert rejected_apply.status_code == 409
    assert rejected_apply.json()["detail"]["reason"] == "endpoint_configuration_changed"
    with Session(db_engine) as session:
        approval_record = session.get(CephApprovalRecord, approval["id"])
        assert approval_record is not None
        assert approval_record.consumed_at is None


async def test_duplicate_approval_post_returns_validated_recovery_metadata(
    ceph_v2_harness,
):
    harness = ceph_v2_harness
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])

    duplicate = await harness.client.post(
        f"/ceph/v2/plans/{plan['id']}/approvals",
        json={"endpoint_id": harness.endpoint_id},
        headers=APPROVER,
    )
    assert duplicate.status_code == 409
    recovery = duplicate.json()["detail"]
    assert recovery == {
        "reason": "approval_already_issued",
        "detail": (
            "This canonical plan already has an approval authority; recover its status by id."
        ),
        "approval_id": approval["id"],
        "plan_id": plan["id"],
        "plan_digest": plan["digest"],
        "endpoint_id": harness.endpoint_id,
        "endpoint_config_revision": plan["endpoint_config_revision"],
        "requester": "alice",
        "approver": "bob",
        "operation_run_id": None,
    }
    assert "token" not in duplicate.text.casefold()

    applied = await _apply(harness, plan["id"], approval["token"])
    assert applied.status_code == 200
    consumed_duplicate = await harness.client.post(
        f"/ceph/v2/plans/{plan['id']}/approvals",
        json={"endpoint_id": harness.endpoint_id},
        headers=APPROVER,
    )
    assert consumed_duplicate.status_code == 409
    assert consumed_duplicate.json()["detail"]["operation_run_id"] == applied.json()["id"]


async def test_duplicate_approval_recovery_rejects_tampered_linked_run(
    ceph_v2_harness,
    db_session,
):
    harness = ceph_v2_harness
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])
    applied = await _apply(harness, plan["id"], approval["token"])
    assert applied.status_code == 200

    run = db_session.get(CephOperationRunRecord, applied.json()["id"])
    assert run is not None
    run.approver = "mallory"
    db_session.add(run)
    db_session.commit()

    duplicate = await harness.client.post(
        f"/ceph/v2/plans/{plan['id']}/approvals",
        json={"endpoint_id": harness.endpoint_id},
        headers=APPROVER,
    )
    status = await harness.client.get(f"/ceph/v2/approvals/{approval['id']}")

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["reason"] == "approval_recovery_integrity_failed"
    assert status.status_code == 409
    assert status.json()["detail"]["reason"] == "approval_recovery_integrity_failed"


async def test_exact_local_session_is_created_once_per_endpoint_scoped_request(
    ceph_v2_harness,
):
    harness = ceph_v2_harness
    plan = await _create_plan(harness)
    assert len(harness.created_sessions) == 1
    assert harness.created_sessions[-1].db_endpoint_id == harness.endpoint_id

    approval = await _approve(harness, plan["id"])
    assert len(harness.created_sessions) == 2

    applied = await _apply(harness, plan["id"], approval["token"])
    assert applied.status_code == 200
    assert len(harness.created_sessions) == 3
    assert all(item.db_endpoint_id == harness.endpoint_id for item in harness.created_sessions)


async def test_generic_session_dependency_with_colliding_id_is_never_write_authority(
    ceph_v2_harness,
):
    harness = ceph_v2_harness
    impostor = SimpleNamespace(db_endpoint_id=harness.endpoint_id, marker="netbox-impostor")

    async def colliding_generic_sessions():
        return [impostor, impostor]

    harness.app.dependency_overrides[proxmox_sessions_dep] = colliding_generic_sessions
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])
    applied = await _apply(harness, plan["id"], approval["token"])

    assert applied.status_code == 200
    assert harness.calls == ["create:node1:rbd"]
    assert all(item is not impostor for item in harness.created_sessions)


@pytest.mark.parametrize(
    ("body", "status", "reason"),
    [
        ({"provider": "proxmox", "operations": []}, 422, "endpoint_id_required"),
        (
            {"provider": "proxmox", "endpoint_id": 999999, "operations": []},
            404,
            "endpoint_missing",
        ),
    ],
)
async def test_plan_requires_existing_exact_endpoint(ceph_v2_harness, body, status, reason):
    response = await ceph_v2_harness.client.post("/ceph/v2/plans", json=body, headers=REQUESTER)
    assert response.status_code == status
    assert response.json()["detail"]["reason"] == reason


async def test_netbox_payload_requires_mapped_endpoint_and_crosses_http_plan_route(
    ceph_v2_harness,
):
    harness = ceph_v2_harness
    desired = {"size": 3, "pg_num": 128}
    payload = {
        "id": 1,
        "cluster_id": 7,
        "provider_id": 3,
        "provider_kind": "proxmox",
        "provider_name": "pve-cluster",
        "provider": "proxmox",
        "endpoint_id": harness.endpoint_id,
        "operation_type": "create",
        "target_kind": "pool",
        "target_ref": "rbd",
        "execution_node": "node1",
        "desired": desired,
        "desired_state": {
            "objects": [
                {
                    "kind": "pool",
                    "target_ref": "rbd",
                    "action": "create",
                    "provider": "proxmox",
                    "node": "node1",
                    "payload": desired,
                }
            ]
        },
        "is_destructive": False,
        "confirmation_required": False,
        "source_branch_schema_id": "branch-abc",
    }
    missing_endpoint = dict(payload)
    missing_endpoint.pop("endpoint_id")

    rejected = await harness.client.post(
        "/ceph/v2/plans",
        json=missing_endpoint,
        headers={"X-Proxbox-Actor": "netbox-ceph"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["reason"] == "endpoint_id_required"

    accepted = await harness.client.post(
        "/ceph/v2/plans",
        json=payload,
        headers={"X-Proxbox-Actor": "netbox-ceph"},
    )
    assert accepted.status_code == 200, accepted.text
    plan = accepted.json()
    assert plan["provider"] == "proxmox"
    assert plan["endpoint_id"] == harness.endpoint_id
    assert len(plan["endpoint_config_revision"]) == 64
    assert len(plan["operations"]) == 1
    assert plan["operations"][0]["node"] == "node1"
    assert plan["operations"][0]["after_summary"] == desired


@pytest.mark.parametrize("headers", [{}, {"X-Proxbox-Actor": "   "}])
async def test_plan_requires_nonempty_actor_header(ceph_v2_harness, headers):
    response = await ceph_v2_harness.client.post(
        "/ceph/v2/plans",
        json=_plan_body(ceph_v2_harness.endpoint_id),
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "actor_required"


async def test_plan_rejects_secret_shaped_actor_without_echo(ceph_v2_harness):
    response = await ceph_v2_harness.client.post(
        "/ceph/v2/plans",
        json=_plan_body(ceph_v2_harness.endpoint_id),
        headers={"X-Proxbox-Actor": "token:actor-canary"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "actor_invalid"
    assert "actor-canary" not in response.text


async def test_plan_rejects_disabled_endpoint_and_generic_query_selector(
    ceph_v2_harness, db_session
):
    harness = ceph_v2_harness
    harness.endpoint.enabled = False
    db_session.add(harness.endpoint)
    db_session.commit()
    disabled = await harness.client.post(
        "/ceph/v2/plans", json=_plan_body(harness.endpoint_id), headers=REQUESTER
    )
    assert disabled.status_code == 403
    assert disabled.json()["detail"]["reason"] == "endpoint_disabled"

    harness.endpoint.enabled = True
    db_session.add(harness.endpoint)
    db_session.commit()

    sessions_before = len(harness.created_sessions)
    forbidden = await harness.client.post(
        "/ceph/v2/plans?source=netbox",
        json=_plan_body(harness.endpoint_id),
        headers=REQUESTER,
    )
    assert forbidden.status_code == 422
    assert forbidden.json()["detail"]["reason"] == "endpoint_selector_query_forbidden"
    assert len(harness.created_sessions) == sessions_before


async def test_duplicate_exact_local_resolution_is_rejected() -> None:
    endpoint = ProxmoxEndpoint(
        id=91,
        name="duplicate",
        ip_address="192.0.2.91",
        username="root@pam",
    )

    class _DuplicateResult:
        def all(self) -> list[ProxmoxEndpoint]:
            return [endpoint, endpoint]

    class _DuplicateSession:
        async def rollback(self) -> None:
            return None

        async def exec(self, _statement: Any) -> _DuplicateResult:
            return _DuplicateResult()

    with pytest.raises(CephWriteGateDenied) as captured:
        await endpoint_binding._exact_local_endpoint(_DuplicateSession(), 91)
    assert captured.value.reason == "endpoint_session_ambiguous"


async def test_factory_session_with_colliding_endpoint_id_is_rejected(
    ceph_v2_harness,
    monkeypatch,
):
    async def mismatched_create(config: Any) -> _FakeBoundSession:
        created = _FakeBoundSession(config)
        created.db_endpoint_id = (config.db_endpoint_id or 0) + 1
        return created

    monkeypatch.setattr(ProxmoxSession, "create", mismatched_create)
    response = await ceph_v2_harness.client.post(
        "/ceph/v2/plans",
        json=_plan_body(ceph_v2_harness.endpoint_id),
        headers=REQUESTER,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "endpoint_session_binding_mismatch"
    assert ceph_v2_harness.calls == []


async def test_actor_body_and_selector_validation_precede_session_creation(ceph_v2_harness):
    harness = ceph_v2_harness
    before = len(harness.created_sessions)

    missing_actor = await harness.client.post(
        "/ceph/v2/plans",
        json=_plan_body(harness.endpoint_id),
    )
    assert missing_actor.status_code == 400

    mismatched_actor_body = _plan_body(harness.endpoint_id)
    mismatched_actor_body["actor"] = "carol"
    actor_mismatch = await harness.client.post(
        "/ceph/v2/plans",
        json=mismatched_actor_body,
        headers=REQUESTER,
    )
    assert actor_mismatch.status_code == 409

    forbidden_selector = await harness.client.post(
        "/ceph/v2/plans?domain=attacker.invalid",
        json=_plan_body(harness.endpoint_id),
        headers=REQUESTER,
    )
    assert forbidden_selector.status_code == 422
    assert len(harness.created_sessions) == before


async def test_allow_writes_false_blocks_plan_and_capability(ceph_v2_harness, db_session):
    harness = ceph_v2_harness
    harness.endpoint.allow_writes = False
    db_session.add(harness.endpoint)
    db_session.commit()

    capabilities = (
        await harness.client.get(
            "/ceph/v2/capabilities",
            params={"provider": "proxmox", "endpoint_id": harness.endpoint_id},
        )
    ).json()["providers"][0]
    assert capabilities["apply"] is False
    plan = await _create_plan(harness)
    assert plan["blocked_actions"]
    assert plan["operations"][0]["supported"] is False


async def test_plan_is_durable_canonical_and_survives_process_cache_clear(
    ceph_v2_harness, db_session
):
    plan = await _create_plan(ceph_v2_harness)
    assert len(plan["digest"]) == 64
    assert plan["requester"] == "alice"
    assert plan["endpoint_id"] == ceph_v2_harness.endpoint_id
    fetched = await ceph_v2_harness.client.get(f"/ceph/v2/plans/{plan['id']}")
    assert fetched.status_code == 200
    record = db_session.get(CephPlanRecord, plan["id"])
    assert record is not None
    assert record.digest == plan["digest"]


async def test_plan_tampering_and_expiry_fail_closed(ceph_v2_harness, db_session):
    tampered = await _create_plan(ceph_v2_harness)
    record = db_session.get(CephPlanRecord, tampered["id"])
    assert record is not None
    record.plan_payload = {**record.plan_payload, "provider": "external"}
    db_session.add(record)
    db_session.commit()
    response = await ceph_v2_harness.client.get(f"/ceph/v2/plans/{tampered['id']}")
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "plan_integrity_failed"

    expired = await _create_plan(ceph_v2_harness)
    expired_record = db_session.get(CephPlanRecord, expired["id"])
    assert expired_record is not None
    expired_plan = PlanResponse.model_validate(expired_record.plan_payload)
    expired_plan.expires_at = datetime.fromtimestamp(time.time() - 1, timezone.utc)
    expired_plan.digest = canonical_plan_digest(expired_plan)
    expired_record.digest = expired_plan.digest
    expired_record.expires_at = expired_plan.expires_at.timestamp()
    expired_record.plan_payload = expired_plan.model_dump(mode="json")
    db_session.add(expired_record)
    db_session.commit()
    response = await ceph_v2_harness.client.post(
        f"/ceph/v2/plans/{expired['id']}/approvals",
        json={"endpoint_id": ceph_v2_harness.endpoint_id},
        headers=APPROVER,
    )
    assert response.status_code == 410


async def test_two_person_approval_is_opaque_hashed_and_audited(ceph_v2_harness, db_session):
    harness = ceph_v2_harness
    plan = await _create_plan(harness)
    same_actor = await harness.client.post(
        f"/ceph/v2/plans/{plan['id']}/approvals",
        json={"endpoint_id": harness.endpoint_id},
        headers=REQUESTER,
    )
    assert same_actor.status_code == 409
    assert same_actor.json()["detail"]["reason"] == "two_person_approval_required"

    approval = await _approve(harness, plan["id"])
    duplicate = await harness.client.post(
        f"/ceph/v2/plans/{plan['id']}/approvals",
        json={"endpoint_id": harness.endpoint_id},
        headers={"X-Proxbox-Actor": "carol"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["reason"] == "approval_already_issued"
    assert len(harness.created_sessions) == 2
    assert approval["token"] not in {
        plan["id"],
        f"confirm:{plan['id']}",
        f"confirm-destructive:{plan['id']}",
    }
    record = db_session.get(CephApprovalRecord, approval["id"])
    assert record is not None
    assert record.token_hash != approval["token"]
    assert len(record.token_hash) == 64

    applied = await _apply(harness, plan["id"], approval["token"])
    assert applied.status_code == 200, applied.text
    run = applied.json()
    assert run["status"] == "completed"
    assert run["endpoint_id"] == harness.endpoint_id
    assert run["plan_digest"] == plan["digest"]
    assert run["requester"] == "alice"
    assert run["approver"] == "bob"
    assert run["approval_id"] == approval["id"]
    assert harness.calls == ["create:node1:rbd"]

    db_session.expire_all()
    consumed = db_session.get(CephApprovalRecord, approval["id"])
    assert consumed is not None
    assert consumed.consumed_at is not None
    assert consumed.consumed_by == "alice"
    assert consumed.operation_run_id == run["id"]


async def test_approve_and_apply_require_nonempty_actor_headers(ceph_v2_harness):
    harness = ceph_v2_harness
    plan = await _create_plan(harness)
    for headers in ({}, {"X-Proxbox-Actor": "  "}):
        response = await harness.client.post(
            f"/ceph/v2/plans/{plan['id']}/approvals",
            json={"endpoint_id": harness.endpoint_id},
            headers=headers,
        )
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "actor_required"

    approval = await _approve(harness, plan["id"])
    for headers in ({}, {"X-Proxbox-Actor": "  "}):
        response = await harness.client.post(
            f"/ceph/v2/plans/{plan['id']}/apply",
            json={
                "endpoint_id": harness.endpoint_id,
                "approval_token": approval["token"],
            },
            headers=headers,
        )
        assert response.status_code == 400
        assert response.json()["detail"]["reason"] == "actor_required"
    assert (await _apply(harness, plan["id"], approval["token"])).status_code == 200


async def test_only_requester_can_consume_approval_without_burning_token(
    ceph_v2_harness, db_session
):
    harness = ceph_v2_harness
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])
    payload = {
        "plan_id": plan["id"],
        "endpoint_id": harness.endpoint_id,
        "approval_token": approval["token"],
    }
    for actor in ("bob", "carol"):
        sessions_before = len(harness.created_sessions)
        rejected = await harness.client.post(
            f"/ceph/v2/plans/{plan['id']}/apply",
            json=payload,
            headers={"X-Proxbox-Actor": actor},
        )
        assert rejected.status_code == 403
        assert rejected.json()["detail"]["reason"] == "approval_requester_mismatch"
        assert len(harness.created_sessions) == sessions_before

    db_session.expire_all()
    record = db_session.get(CephApprovalRecord, approval["id"])
    assert record is not None
    assert record.consumed_at is None
    assert record.operation_run_id is None
    assert (await _apply(harness, plan["id"], approval["token"])).status_code == 200


async def test_non_proxmox_apply_fails_closed_without_durable_selector_gate(
    ceph_v2_harness,
):
    harness = ceph_v2_harness
    response = await harness.client.post(
        "/ceph/v2/plans",
        json={"provider": "dashboard", "operations": []},
        headers=REQUESTER,
    )
    assert response.status_code == 200, response.text
    plan = response.json()
    approval = await harness.client.post(
        f"/ceph/v2/plans/{plan['id']}/approvals",
        json={},
        headers=APPROVER,
    )
    assert approval.status_code == 409
    assert approval.json()["detail"]["reason"] == "durable_provider_write_gate_unavailable"
    apply = await harness.client.post(
        f"/ceph/v2/plans/{plan['id']}/apply",
        json={"provider": "dashboard", "approval_token": "untrusted"},
        headers=REQUESTER,
    )
    assert apply.status_code == 409
    assert apply.json()["detail"]["reason"] == "durable_provider_write_gate_unavailable"


async def test_legacy_confirmation_predictable_tokens_and_wrong_endpoint_are_rejected(
    ceph_v2_harness,
):
    harness = ceph_v2_harness
    plan = await _create_plan(harness, action="delete")
    for payload in (
        {"endpoint_id": harness.endpoint_id, "confirm_destructive": True},
        {
            "endpoint_id": harness.endpoint_id,
            "approval_token": f"confirm-destructive:{plan['id']}",
        },
        {"endpoint_id": harness.endpoint_id + 1, "approval_token": "opaque-looking"},
    ):
        response = await harness.client.post(
            f"/ceph/v2/plans/{plan['id']}/apply",
            json=payload,
            headers=REQUESTER,
        )
        assert response.status_code in {409, 404}
    assert harness.calls == []


async def test_approval_expiry_and_sequential_replay_are_rejected(ceph_v2_harness, db_session):
    harness = ceph_v2_harness
    expired_plan = await _create_plan(harness)
    expired_approval = await _approve(harness, expired_plan["id"])
    record = db_session.get(CephApprovalRecord, expired_approval["id"])
    assert record is not None
    record.expires_at = time.time() - 1
    db_session.add(record)
    db_session.commit()
    expired = await _apply(harness, expired_plan["id"], expired_approval["token"])
    assert expired.status_code == 410
    assert expired.json()["detail"]["reason"] == "approval_expired"

    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])
    first = await _apply(harness, plan["id"], approval["token"])
    assert first.status_code == 200
    sessions_before_replay = len(harness.created_sessions)
    replay = await _apply(harness, plan["id"], approval["token"])
    assert replay.status_code == 409
    recovery = replay.json()["detail"]
    assert recovery["reason"] == "approval_replayed"
    assert recovery["approval_id"] == approval["id"]
    assert recovery["operation_run_id"] == first.json()["id"]
    assert len(harness.created_sessions) == sessions_before_replay
    recovered = await harness.client.get(f"/ceph/v2/operations/{recovery['operation_run_id']}")
    assert recovered.status_code == 200
    assert recovered.json()["id"] == first.json()["id"]


async def test_approval_status_lookup_exposes_only_safe_metadata(
    ceph_v2_harness,
    db_session,
):
    harness = ceph_v2_harness
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])

    before = await harness.client.get(f"/ceph/v2/approvals/{approval['id']}")
    assert before.status_code == 200
    before_payload = before.json()
    assert before_payload["operation_run_id"] is None
    assert before_payload["consumed_at"] is None
    assert "token" not in before_payload
    assert "token_hash" not in before_payload
    assert approval["token"] not in before.text

    applied = await _apply(harness, plan["id"], approval["token"])
    after = await harness.client.get(f"/ceph/v2/approvals/{approval['id']}")
    assert after.status_code == 200
    after_payload = after.json()
    assert after_payload["operation_run_id"] == applied.json()["id"]
    assert after_payload["consumed_by"] == "alice"
    assert after_payload["consumed_at"] is not None
    assert approval["token"] not in after.text

    record = db_session.get(CephApprovalRecord, approval["id"])
    assert record is not None
    assert record.token_hash not in after.text


async def test_unknown_kind_action_is_blocked_without_approval_or_write(
    ceph_v2_harness,
    db_session,
):
    harness = ceph_v2_harness
    body = _plan_body(harness.endpoint_id)
    body["operations"] = [
        {"kind": "pool", "target_ref": "rbd", "action": "teleport", "node": "node1"},
    ]
    created = await harness.client.post("/ceph/v2/plans", json=body, headers=REQUESTER)
    assert created.status_code == 200
    plan = created.json()
    assert plan["operations"][0]["supported"] is False
    assert plan["blocked_actions"][0]["action"] == "teleport"
    sessions_before_approval = len(harness.created_sessions)

    approval = await harness.client.post(
        f"/ceph/v2/plans/{plan['id']}/approvals",
        json={"endpoint_id": harness.endpoint_id},
        headers=APPROVER,
    )
    assert approval.status_code == 409
    assert approval.json()["detail"]["reason"] == "plan_not_approvable"
    assert len(harness.created_sessions) == sessions_before_approval
    approvals = list(
        db_session.exec(
            select(CephApprovalRecord).where(CephApprovalRecord.plan_id == plan["id"])
        ).all()
    )
    assert approvals == []
    assert harness.calls == []


@pytest.mark.parametrize(
    "operation",
    [
        {"kind": "pool", "target_ref": "rbd", "action": "create"},
        {
            "kind": "pool",
            "target_ref": "rbd",
            "action": "create",
            "node": "node2",
        },
        {
            "kind": "pool",
            "target_ref": "rbd",
            "action": "create",
            "node": "node1",
            "after_summary": {"unsupported_option": True},
        },
    ],
)
async def test_plan_blocks_missing_wrong_node_or_untyped_payload_before_approval(
    ceph_v2_harness,
    operation,
):
    harness = ceph_v2_harness
    body = _plan_body(harness.endpoint_id)
    body["operations"] = [operation]

    created = await harness.client.post("/ceph/v2/plans", json=body, headers=REQUESTER)

    assert created.status_code == 200
    plan = created.json()
    assert plan["operations"][0]["supported"] is False
    approval = await harness.client.post(
        f"/ceph/v2/plans/{plan['id']}/approvals",
        json={"endpoint_id": harness.endpoint_id},
        headers=APPROVER,
    )
    assert approval.status_code == 409
    assert approval.json()["detail"]["reason"] == "plan_not_approvable"
    assert harness.calls == []


async def test_apply_envelope_mismatch_precedes_session_creation(ceph_v2_harness):
    harness = ceph_v2_harness
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])
    before = len(harness.created_sessions)

    response = await harness.client.post(
        f"/ceph/v2/plans/{plan['id']}/apply",
        json={
            "plan_id": plan["id"],
            "endpoint_id": harness.endpoint_id + 1,
            "approval_token": approval["token"],
        },
        headers=REQUESTER,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "apply_endpoint_mismatch"
    assert len(harness.created_sessions) == before
    assert harness.calls == []


async def test_allow_writes_is_rechecked_after_approval_before_dispatch(
    ceph_v2_harness, db_session
):
    harness = ceph_v2_harness
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])
    harness.endpoint.allow_writes = False
    db_session.add(harness.endpoint)
    db_session.commit()

    response = await _apply(harness, plan["id"], approval["token"])
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "endpoint_writes_disabled"
    assert harness.calls == []


async def test_allow_writes_revocation_stops_second_mutation(ceph_v2_harness, db_engine):
    harness = ceph_v2_harness
    body = _plan_body(harness.endpoint_id)
    body["operations"] = [
        {"kind": "pool", "target_ref": "first", "action": "create", "node": "node1"},
        {"kind": "pool", "target_ref": "second", "action": "create", "node": "node1"},
    ]
    created = await harness.client.post(
        "/ceph/v2/plans",
        json=body,
        headers=REQUESTER,
    )
    assert created.status_code == 200, created.text
    plan = created.json()
    approval = await _approve(harness, plan["id"])

    def revoke_after_first_call(call_count: int) -> None:
        if call_count != 1:
            return
        with Session(db_engine) as session:
            endpoint = session.get(ProxmoxEndpoint, harness.endpoint_id)
            assert endpoint is not None
            endpoint.allow_writes = False
            session.add(endpoint)
            session.commit()

    harness.write.after_call = revoke_after_first_call
    response = await _apply(harness, plan["id"], approval["token"])

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["reason"] == "endpoint_writes_disabled"
    assert harness.calls == ["create:node1:first"]
    operation = await harness.client.get(f"/ceph/v2/operations/{detail['operation_run_id']}")
    assert operation.status_code == 200
    assert operation.json()["status"] == "failed"
    assert operation.json()["result_summary"]["applied"] == 1
    assert operation.json()["provider_task_refs"] == [CREATE_UPID]
    assert operation.json()["result_summary"]["results"][0]["target_ref"] == "first"


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("ip_address", "198.51.100.17"),
        ("password", "credential-drift-canary"),
        ("verify_ssl", False),
        ("timeout", 17),
        ("max_retries", 4),
        ("retry_backoff", 1.75),
    ],
)
async def test_connection_auth_tls_timeout_or_retry_drift_stops_second_mutation(
    ceph_v2_harness,
    db_engine,
    field,
    changed_value,
):
    harness = ceph_v2_harness
    body = _plan_body(harness.endpoint_id)
    body["operations"] = [
        {"kind": "pool", "target_ref": "first", "action": "create", "node": "node1"},
        {"kind": "pool", "target_ref": "second", "action": "create", "node": "node1"},
    ]
    plan_response = await harness.client.post("/ceph/v2/plans", json=body, headers=REQUESTER)
    assert plan_response.status_code == 200
    plan = plan_response.json()
    approval = await _approve(harness, plan["id"])

    def drift_after_first_call(call_count: int) -> None:
        if call_count != 1:
            return
        with Session(db_engine) as local_session:
            endpoint = local_session.get(ProxmoxEndpoint, harness.endpoint_id)
            assert endpoint is not None
            setattr(endpoint, field, changed_value)
            local_session.add(endpoint)
            local_session.commit()

    harness.write.after_call = drift_after_first_call
    response = await _apply(harness, plan["id"], approval["token"])

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "endpoint_configuration_changed"
    assert harness.calls == ["create:node1:first"]
    assert "credential-drift-canary" not in response.text


async def test_endpoint_deletion_stops_second_mutation(ceph_v2_harness, db_engine):
    harness = ceph_v2_harness
    body = _plan_body(harness.endpoint_id)
    body["operations"] = [
        {"kind": "pool", "target_ref": "first", "action": "create", "node": "node1"},
        {"kind": "pool", "target_ref": "second", "action": "create", "node": "node1"},
    ]
    plan = (await harness.client.post("/ceph/v2/plans", json=body, headers=REQUESTER)).json()
    approval = await _approve(harness, plan["id"])

    def delete_after_first_call(call_count: int) -> None:
        if call_count != 1:
            return
        with Session(db_engine) as local_session:
            endpoint = local_session.get(ProxmoxEndpoint, harness.endpoint_id)
            assert endpoint is not None
            local_session.delete(endpoint)
            local_session.commit()

    harness.write.after_call = delete_after_first_call
    response = await _apply(harness, plan["id"], approval["token"])

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "endpoint_missing"
    assert harness.calls == ["create:node1:first"]


async def test_bound_session_drift_stops_second_mutation(ceph_v2_harness):
    harness = ceph_v2_harness
    body = _plan_body(harness.endpoint_id)
    body["operations"] = [
        {"kind": "pool", "target_ref": "first", "action": "create", "node": "node1"},
        {"kind": "pool", "target_ref": "second", "action": "create", "node": "node1"},
    ]
    plan = (await harness.client.post("/ceph/v2/plans", json=body, headers=REQUESTER)).json()
    approval = await _approve(harness, plan["id"])

    def mutate_session_after_first_call(call_count: int) -> None:
        if call_count == 1:
            harness.created_sessions[-1].ssl = not harness.created_sessions[-1].ssl

    harness.write.after_call = mutate_session_after_first_call
    response = await _apply(harness, plan["id"], approval["token"])

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "endpoint_session_binding_changed"
    assert harness.calls == ["create:node1:first"]


async def test_unchanged_binding_allows_each_mutation(ceph_v2_harness):
    harness = ceph_v2_harness
    body = _plan_body(harness.endpoint_id)
    body["operations"] = [
        {"kind": "pool", "target_ref": "first", "action": "create", "node": "node1"},
        {"kind": "pool", "target_ref": "second", "action": "create", "node": "node1"},
    ]
    plan = (await harness.client.post("/ceph/v2/plans", json=body, headers=REQUESTER)).json()
    approval = await _approve(harness, plan["id"])
    response = await _apply(harness, plan["id"], approval["token"])

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert harness.calls == ["create:node1:first", "create:node1:second"]


async def test_provider_operation_forbids_extras_and_secret_target_refs(ceph_v2_harness):
    harness = ceph_v2_harness
    extra = _plan_body(harness.endpoint_id)
    extra["operations"][0]["api_key"] = "top-level-secret"
    rejected_extra = await harness.client.post(
        "/ceph/v2/plans",
        json=extra,
        headers=REQUESTER,
    )
    assert rejected_extra.status_code == 422
    assert "top-level-secret" not in rejected_extra.text

    poisoned_location = _plan_body(harness.endpoint_id)
    poisoned_location["operations"][0]["password=location-canary"] = "ignored"
    rejected_location = await harness.client.post(
        "/ceph/v2/plans",
        json=poisoned_location,
        headers=REQUESTER,
    )
    assert rejected_location.status_code == 422
    assert "location-canary" not in rejected_location.text
    assert "[REDACTED_FIELD]" in rejected_location.text

    valid_syntax_location = _plan_body(harness.endpoint_id)
    valid_syntax_location["operations"][0]["opaqueCanaryField"] = "ignored"
    rejected_valid_syntax_location = await harness.client.post(
        "/ceph/v2/plans",
        json=valid_syntax_location,
        headers=REQUESTER,
    )
    assert rejected_valid_syntax_location.status_code == 422
    assert "opaqueCanaryField" not in rejected_valid_syntax_location.text
    assert "[REDACTED_FIELD]" in rejected_valid_syntax_location.text

    secret_ref = _plan_body(harness.endpoint_id)
    secret_ref["operations"][0]["target_ref"] = (
        "https://operator:target-secret@ceph.invalid?token=target-canary"
    )
    rejected_ref = await harness.client.post(
        "/ceph/v2/plans",
        json=secret_ref,
        headers=REQUESTER,
    )
    assert rejected_ref.status_code == 422
    assert "target-secret" not in rejected_ref.text
    assert "target-canary" not in rejected_ref.text

    secret_alias_ref = _plan_body(harness.endpoint_id)
    secret_alias_ref["operations"][0]["target_ref"] = "pool; passwd:alias-canary"
    rejected_alias = await harness.client.post(
        "/ceph/v2/plans",
        json=secret_alias_ref,
        headers=REQUESTER,
    )
    assert rejected_alias.status_code == 422
    assert "alias-canary" not in rejected_alias.text

    validation = await harness.client.post(
        "/ceph/v2/validate",
        json={"kind": "password:validation-canary"},
    )
    assert validation.status_code == 200
    assert validation.json()["valid"] is False
    assert "validation-canary" not in validation.text

    validation_extra = await harness.client.post(
        "/ceph/v2/validate",
        json={
            "operations": [
                {
                    "kind": "pool",
                    "target_ref": "rbd",
                    "opaqueValidationCanary": "ignored",
                }
            ]
        },
    )
    assert validation_extra.status_code == 200
    assert validation_extra.json()["valid"] is False
    assert "opaqueValidationCanary" not in validation_extra.text


async def test_credential_ref_is_opaque_and_operation_secrets_never_reach_db_api_or_sse(
    ceph_v2_harness,
    db_engine,
    monkeypatch,
):
    harness = ceph_v2_harness
    invalid = _plan_body(harness.endpoint_id)
    invalid["operations"][0]["after_summary"] = {
        "credential_ref": "https://vault.invalid/secret?token=raw"
    }
    invalid_response = await harness.client.post(
        "/ceph/v2/plans",
        json=invalid,
        headers=REQUESTER,
    )
    assert invalid_response.status_code == 422

    canaries = {
        "api_key": "api-key-canary",
        "apiKey": "api-camel-canary",
        "api_token": "api-token-field-canary",
        "apiToken": "api-token-camel-canary",
        "access_key": "access-key-canary",
        "accessKey": "access-camel-canary",
        "rgw_access_key": "rgw-access-canary",
        "rgwAccessKey": "rgw-camel-canary",
        "privateKey": "private-camel-canary",
        "authorization": "Bearer authorization-canary",
        "cookie": "session=cookie-canary",
        "keys": ["key-list-canary"],
        "access_token": "access-token-canary",
        "client_secret": "client-secret-field-canary",
        "credentials": {"password": "credential-canary"},
    }
    body = _plan_body(harness.endpoint_id)
    body["operations"][0]["after_summary"] = {"size": 3}
    body["operations"][0]["metadata"] = {
        **canaries,
        "credentialRef": "vault:ceph-prod-01",
    }
    plan_response = await harness.client.post(
        "/ceph/v2/plans",
        json=body,
        headers=REQUESTER,
    )
    assert plan_response.status_code == 200, plan_response.text
    plan = plan_response.json()
    summary = plan["operations"][0]["after_summary"]
    metadata = plan["operations"][0]["metadata"]
    assert metadata["credentialRef"] == "vault:ceph-prod-01"
    assert summary == {"size": 3}
    assert all(metadata[key] == "[REDACTED]" for key in canaries)

    async def secret_bearing_result(
        _write: Any,
        operation: Any,
        _node: str,
        *,
        confirm_destructive: bool,
    ) -> dict[str, Any]:
        assert confirm_destructive is True

        class NonJSONProviderValue:
            def __str__(self) -> str:
                return "apiToken=opaque-api-token-canary client_secret:opaque-client-canary"

        return {
            "operation_id": operation.id,
            "result": "submitted",
            "upid": CREATE_UPID,
            "nested": {
                **canaries,
                "safe_url": (
                    "https://operator:url-userinfo-canary@pve.invalid/result"
                    "?api_token=query-api-token-canary"
                    "&accessToken=query-access-token-canary"
                    "&client-secret=query-client-secret-canary"
                ),
                "safe_assignments": (
                    "apiToken=assignment-api-token-canary "
                    "access_token:assignment-access-token-canary "
                    "clientSecret=assignment-client-secret-canary"
                ),
                "failure": RuntimeError("client_secret=exception-client-canary"),
                "opaque": NonJSONProviderValue(),
            },
        }

    monkeypatch.setattr(
        "proxbox_api.ceph.v2_providers.proxmox.execute_operation",
        secret_bearing_result,
    )

    approval = await _approve(harness, plan["id"])
    applied = await _apply(harness, plan["id"], approval["token"])
    assert applied.status_code == 200, applied.text
    operation_id = applied.json()["id"]
    operation = await harness.client.get(f"/ceph/v2/operations/{operation_id}")
    events = await harness.client.get(f"/ceph/v2/operations/{operation_id}/events")

    with Session(db_engine) as session:
        plan_record = session.get(CephPlanRecord, plan["id"])
        run_record = session.get(CephOperationRunRecord, operation_id)
        event_records = session.exec(
            select(CephOperationEventRecord).where(CephOperationEventRecord.run_id == operation_id)
        ).all()
        persisted = repr((plan_record, run_record, event_records))
    serialized = "\n".join(
        (plan_response.text, applied.text, operation.text, events.text, persisted)
    )
    for canary in (
        "api-key-canary",
        "api-camel-canary",
        "api-token-field-canary",
        "api-token-camel-canary",
        "access-key-canary",
        "access-camel-canary",
        "rgw-access-canary",
        "rgw-camel-canary",
        "private-camel-canary",
        "authorization-canary",
        "cookie-canary",
        "key-list-canary",
        "access-token-canary",
        "client-secret-field-canary",
        "credential-canary",
        "url-userinfo-canary",
        "query-api-token-canary",
        "query-access-token-canary",
        "query-client-secret-canary",
        "assignment-api-token-canary",
        "assignment-access-token-canary",
        "assignment-client-secret-canary",
        "exception-client-canary",
        "opaque-api-token-canary",
        "opaque-client-canary",
    ):
        assert canary not in serialized


async def test_provider_exception_text_is_never_persisted_or_returned(
    ceph_v2_harness,
    db_session,
    caplog,
):
    harness = ceph_v2_harness
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])
    secret_url = "https://operator:super-secret@ceph.invalid/write?token=raw-canary"
    harness.write.error = RuntimeError(secret_url)

    response = await _apply(harness, plan["id"], approval["token"])

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "outcome_unknown"
    assert payload["errors"] == ["Provider dispatch outcome is unknown."]
    serialized_response = response.text
    assert "super-secret" not in serialized_response
    assert "raw-canary" not in serialized_response
    assert approval["token"] not in serialized_response

    record = db_session.get(CephOperationRunRecord, payload["id"])
    assert record is not None
    persisted = repr(
        {
            "request_summary": record.request_summary,
            "warnings": record.warnings,
            "errors": record.errors,
            "result_summary": record.result_summary,
        }
    )
    assert "super-secret" not in persisted
    assert "raw-canary" not in persisted
    assert approval["token"] not in persisted
    assert "super-secret" not in caplog.text
    assert "raw-canary" not in caplog.text


async def test_plan_read_failure_diagnostics_are_secret_free(
    ceph_v2_harness,
    caplog,
):
    harness = ceph_v2_harness
    canary = "postgresql://operator:read-secret@db.invalid/live?password=read-canary"
    harness.cluster_read.error = RuntimeError(canary)
    body = {
        "provider": "proxmox",
        "endpoint_id": harness.endpoint_id,
        "desired_state": {
            "objects": [
                {"kind": "pool", "target_ref": "rbd", "action": "ensure"},
            ]
        },
    }
    response = await harness.client.post("/ceph/v2/plans", json=body, headers=REQUESTER)

    assert response.status_code == 502
    assert response.json()["detail"]["reason"] == "provider_state_unavailable"
    assert "read-secret" not in response.text
    assert "read-canary" not in response.text
    assert "RuntimeError" not in response.text
    assert "read-secret" not in caplog.text
    assert "read-canary" not in caplog.text


async def test_session_creation_failure_diagnostics_are_secret_free(
    ceph_v2_harness,
    monkeypatch,
    caplog,
):
    canary = "https://operator:create-secret@pve.invalid?token=create-canary"

    async def fail_create(_config: Any) -> object:
        raise RuntimeError(canary)

    monkeypatch.setattr(ProxmoxSession, "create", fail_create)
    response = await ceph_v2_harness.client.post(
        "/ceph/v2/plans",
        json=_plan_body(ceph_v2_harness.endpoint_id),
        headers=REQUESTER,
    )

    assert response.status_code == 502
    assert response.json()["detail"]["reason"] == "endpoint_session_unavailable"
    assert "create-secret" not in response.text
    assert "create-canary" not in response.text
    assert "create-secret" not in caplog.text


async def test_unhandled_ceph_failure_never_echoes_or_logs_exception_text(caplog) -> None:
    app = FastAPI()
    register_exception_handlers(app)
    canary = "https://operator:unhandled-secret@pve.invalid?token=unhandled-canary"

    @app.get("/ceph/v2/fail")
    async def fail() -> None:
        raise RuntimeError(canary)

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/ceph/v2/fail")

    assert response.status_code == 500
    assert response.json()["correlation_id"]
    assert "unhandled-secret" not in response.text
    assert "unhandled-canary" not in response.text
    assert "unhandled-secret" not in caplog.text
    assert "unhandled-canary" not in caplog.text
    assert "create-canary" not in caplog.text


async def test_reconcile_metrics_database_and_sse_diagnostics_are_secret_free(
    ceph_v2_harness,
    db_session,
    caplog,
):
    harness = ceph_v2_harness
    canary = "https://operator:read-secret@ceph.invalid/read?token=read-canary"
    harness.cluster_read.error = RuntimeError(canary)

    reconcile = await harness.client.post(
        "/ceph/v2/reconcile",
        json={"provider": "proxmox", "endpoint_id": harness.endpoint_id, "scope": {}},
    )
    assert reconcile.status_code == 200
    run = reconcile.json()
    assert run["status"] == "failed"
    metrics = await harness.client.get("/ceph/v2/metrics", params={"provider": "proxmox"})
    assert metrics.status_code == 200
    assert metrics.json()["metrics"] == {}
    sse = await harness.client.get(f"/ceph/v2/operations/{run['id']}/events")
    assert sse.status_code == 200

    record = db_session.get(CephOperationRunRecord, run["id"])
    assert record is not None
    serialized = "\n".join((reconcile.text, metrics.text, sse.text, repr(record)))
    assert "read-secret" not in serialized
    assert "read-canary" not in serialized
    assert "RuntimeError" not in serialized
    assert "read-secret" not in caplog.text
    assert "read-canary" not in caplog.text


async def test_successful_metrics_payload_is_redacted_at_route_boundary(
    ceph_v2_harness,
    monkeypatch,
    caplog,
):
    canary = "https://operator:metric-secret@metrics.invalid?token=metric-canary"

    class _MetricsAdapter:
        async def metrics(self, _scope: dict[str, Any]) -> dict[str, Any]:
            return {
                "password": "plain-metric-secret",
                "endpoint": canary,
                "nested": {"bearer_token": "raw-bearer"},
            }

    async def build_metrics_adapter(*_args: Any, **_kwargs: Any) -> _MetricsAdapter:
        return _MetricsAdapter()

    monkeypatch.setattr(v2_routes_module, "build_adapter", build_metrics_adapter)
    response = await ceph_v2_harness.client.get(
        "/ceph/v2/metrics",
        params={"provider": "prometheus"},
    )

    assert response.status_code == 200
    assert response.json()["metrics"]["password"] == "[REDACTED]"
    assert response.json()["metrics"]["nested"]["bearer_token"] == "[REDACTED]"
    assert "plain-metric-secret" not in response.text
    assert "metric-secret" not in response.text
    assert "metric-canary" not in response.text
    assert "metric-secret" not in caplog.text


async def test_metrics_adapter_construction_failure_is_secret_free(
    ceph_v2_harness,
    monkeypatch,
    caplog,
):
    canary = "https://operator:build-secret@metrics.invalid?token=build-canary"

    async def fail_build(*_args: Any, **_kwargs: Any) -> object:
        raise RuntimeError(canary)

    monkeypatch.setattr(v2_routes_module, "build_adapter", fail_build)
    response = await ceph_v2_harness.client.get(
        "/ceph/v2/metrics",
        params={"provider": "prometheus"},
    )

    assert response.status_code == 200
    assert response.json()["metrics"] == {}
    assert "build-secret" not in response.text
    assert "build-canary" not in response.text
    assert "RuntimeError" not in response.text
    assert "build-secret" not in caplog.text
    assert "build-canary" not in caplog.text


async def test_upid_submission_polls_to_failed_terminal_state_with_ordered_evidence(
    ceph_v2_harness,
    db_session,
):
    harness = ceph_v2_harness
    harness.task_status.value = {"status": "stopped", "exitstatus": "TASK ERROR"}
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])
    response = await _apply(harness, plan["id"], approval["token"])

    assert response.status_code == 200
    run = response.json()
    assert run["status"] == "failed"
    assert run["provider_task_refs"] == [CREATE_UPID]
    assert run["result_summary"]["applied"] == 0
    assert [event["event"] for event in run["events"]] == [
        "approval_consumed",
        "dispatch_intent",
        "provider_task_submitted",
        "provider_task_failed",
    ]
    assert [event["sequence"] for event in run["events"]] == [0, 1, 2, 3]

    rows = list(
        db_session.exec(
            select(CephOperationEventRecord)
            .where(CephOperationEventRecord.run_id == run["id"])
            .order_by(CephOperationEventRecord.sequence)
        ).all()
    )
    assert [row.event for row in rows] == [
        "approval_consumed",
        "dispatch_intent",
        "provider_task_submitted",
        "provider_task_failed",
    ]


async def test_task_status_transport_failure_is_outcome_unknown_and_not_retried(
    ceph_v2_harness,
):
    harness = ceph_v2_harness
    canary = "https://operator:task-secret@ceph.invalid/status?token=task-canary"
    harness.task_status.value = RuntimeError(canary)
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])
    response = await _apply(harness, plan["id"], approval["token"])

    assert response.status_code == 200
    run = response.json()
    assert run["status"] == "outcome_unknown"
    assert run["provider_task_refs"] == [CREATE_UPID]
    assert run["events"][-1]["code"] == "provider_task_status_unavailable"
    assert "task-secret" not in response.text
    assert "task-canary" not in response.text

    calls_before = list(harness.calls)
    replay = await _apply(harness, plan["id"], approval["token"])
    assert replay.status_code == 409
    assert replay.json()["detail"]["operation_run_id"] == run["id"]
    assert harness.calls == calls_before


async def test_successful_upid_has_submitted_then_completed_evidence(ceph_v2_harness):
    harness = ceph_v2_harness
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])
    response = await _apply(harness, plan["id"], approval["token"])

    assert response.status_code == 200
    run = response.json()
    assert run["status"] == "completed"
    assert [event["event"] for event in run["events"]] == [
        "approval_consumed",
        "dispatch_intent",
        "provider_task_submitted",
        "provider_task_completed",
        "run_completed",
    ]
    assert run["events"][2]["status"] == "running"
    assert run["events"][3]["status"] == "running"
    assert run["events"][4]["status"] == "completed"


async def test_sdk_await_remains_nonterminal_dispatching_with_owned_live_lease(
    ceph_v2_harness,
    db_session,
    monkeypatch,
):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_dispatch(
        _write: Any,
        operation: Any,
        _node: str,
        *,
        confirm_destructive: bool,
    ) -> dict[str, Any]:
        assert confirm_destructive is True
        entered.set()
        await release.wait()
        return {"operation_id": operation.id, "upid": CREATE_UPID}

    monkeypatch.setattr(
        "proxbox_api.ceph.v2_providers.proxmox.execute_operation",
        blocked_dispatch,
    )
    plan = await _create_plan(ceph_v2_harness)
    approval = await _approve(ceph_v2_harness, plan["id"])
    apply_task = asyncio.create_task(_apply(ceph_v2_harness, plan["id"], approval["token"]))
    await asyncio.wait_for(entered.wait(), timeout=2)

    db_session.expire_all()
    record = db_session.exec(
        select(CephOperationRunRecord).where(CephOperationRunRecord.plan_id == plan["id"])
    ).one()
    assert record.status == "dispatching"
    assert record.lease_owner is not None
    assert record.lease_expires_at is not None and record.lease_expires_at > time.time()
    assert (
        db_session.exec(
            select(CephOperationEventRecord)
            .where(CephOperationEventRecord.run_id == record.id)
            .order_by(CephOperationEventRecord.sequence)
        )
        .all()[-1]
        .status
        == "dispatching"
    )

    release.set()
    response = await apply_task
    assert response.status_code == 200
    assert response.json()["status"] == "completed"


async def test_real_proxmox_upid_with_realm_is_valid_and_polled(ceph_v2_harness):
    harness = ceph_v2_harness
    real_upid = "UPID:node1:00001CB4:0001DA12:69AA4164:cephcreate:rbd:root@pam:"
    harness.write.result_override = real_upid
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])

    response = await _apply(harness, plan["id"], approval["token"])

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
    assert response.json()["provider_task_refs"] == [real_upid]


async def test_non_upid_proxmox_result_is_outcome_unknown(ceph_v2_harness):
    harness = ceph_v2_harness
    harness.write.result_override = "task-finished-without-upid"
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])

    response = await _apply(harness, plan["id"], approval["token"])

    assert response.status_code == 200
    run = response.json()
    assert run["status"] == "outcome_unknown"
    assert run["provider_task_refs"] == []
    assert run["events"][-1]["code"] == "provider_task_reference_invalid"


async def test_structurally_invalid_upid_is_not_execution_evidence(ceph_v2_harness):
    harness = ceph_v2_harness
    harness.write.result_override = "UPID:not-a-real-task:password=upid-canary"
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])

    response = await _apply(harness, plan["id"], approval["token"])

    assert response.status_code == 200
    assert response.json()["status"] == "outcome_unknown"
    assert response.json()["provider_task_refs"] == []
    assert response.json()["events"][-1]["code"] == "provider_task_reference_invalid"
    assert "upid-canary" not in response.text


async def test_upid_node_must_match_exact_plan_node(ceph_v2_harness):
    harness = ceph_v2_harness
    harness.write.result_override = "UPID:node2:00000001:00000002:00000003:cephcreate:rbd:root@pam:"
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])

    response = await _apply(harness, plan["id"], approval["token"])

    assert response.status_code == 200
    run = response.json()
    assert run["status"] == "outcome_unknown"
    assert run["provider_task_refs"] == []
    assert run["events"][-1]["code"] == "provider_task_node_mismatch"


async def test_upid_must_be_previously_unseen_for_each_mutation(ceph_v2_harness):
    harness = ceph_v2_harness
    harness.write.result_override = CREATE_UPID
    body = _plan_body(harness.endpoint_id)
    body["operations"] = [
        {"kind": "pool", "target_ref": "first", "action": "create", "node": "node1"},
        {"kind": "pool", "target_ref": "second", "action": "create", "node": "node1"},
    ]
    plan = (await harness.client.post("/ceph/v2/plans", json=body, headers=REQUESTER)).json()
    approval = await _approve(harness, plan["id"])

    response = await _apply(harness, plan["id"], approval["token"])

    assert response.status_code == 200
    run = response.json()
    assert run["status"] == "outcome_unknown"
    assert run["provider_task_refs"] == [CREATE_UPID]
    assert run["events"][-1]["code"] == "provider_task_reference_reused"


async def test_mutation_rejects_multiple_distinct_upids(ceph_v2_harness, monkeypatch):
    second = "UPID:node1:0000000D:0000000E:0000000F:cephcreate:rbd:root@pam:"

    async def multiple_refs(
        _write: Any,
        operation: Any,
        _node: str,
        *,
        confirm_destructive: bool,
    ) -> dict[str, Any]:
        assert confirm_destructive is True
        return {
            "operation_id": operation.id,
            "upid": CREATE_UPID,
            "provider_task_refs": [second],
        }

    monkeypatch.setattr(
        "proxbox_api.ceph.v2_providers.proxmox.execute_operation",
        multiple_refs,
    )
    plan = await _create_plan(ceph_v2_harness)
    approval = await _approve(ceph_v2_harness, plan["id"])
    response = await _apply(ceph_v2_harness, plan["id"], approval["token"])

    assert response.status_code == 200
    assert response.json()["status"] == "outcome_unknown"
    assert response.json()["provider_task_refs"] == []
    assert response.json()["events"][-1]["code"] == "provider_task_reference_invalid"


async def test_not_found_path_identifiers_are_not_reflected(ceph_v2_harness):
    response = await ceph_v2_harness.client.get("/ceph/v2/operations/password%3Dnot-found-canary")

    assert response.status_code == 404
    assert response.json()["detail"] == "Operation not found."
    assert "not-found-canary" not in response.text


async def test_recovery_metadata_rejects_noncanonical_or_tampered_bindings() -> None:
    now = time.time()
    baseline = {
        "id": str(uuid4()),
        "plan_id": str(uuid4()),
        "plan_digest": "a" * 64,
        "endpoint_id": 17,
        "endpoint_config_revision": "b" * 64,
        "requester": "alice",
        "approver": "bob",
        "token_hash": "c" * 64,
        "created_at": now,
        "expires_at": now + 60,
    }
    for override in (
        {"id": "approval=password=recovery-canary"},
        {"plan_id": "plan=password=recovery-canary"},
        {"plan_digest": "d" * 63},
        {"endpoint_config_revision": "e" * 63},
        {"operation_run_id": "run=password=recovery-canary"},
    ):
        record = CephApprovalRecord(**{**baseline, **override})
        with pytest.raises(CephApprovalError) as raised:
            approval_recovery_metadata(record)
        assert raised.value.reason == "approval_recovery_integrity_failed"
        assert "recovery-canary" not in raised.value.detail
        await asyncio.sleep(0)


async def test_operation_lookup_sse_and_reconcile_remain_read_only(ceph_v2_harness):
    harness = ceph_v2_harness
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])
    run = (await _apply(harness, plan["id"], approval["token"])).json()
    lookup = await harness.client.get(f"/ceph/v2/operations/{run['id']}")
    assert lookup.status_code == 200
    events = await harness.client.get(f"/ceph/v2/operations/{run['id']}/events")
    assert events.status_code == 200
    assert run["id"] in events.text

    writes_before = list(harness.calls)
    reconcile = await harness.client.post(
        "/ceph/v2/reconcile",
        json={"provider": "proxmox", "endpoint_id": harness.endpoint_id, "scope": {}},
    )
    assert reconcile.status_code == 200
    assert reconcile.json()["result_summary"]["result"] == "read_only_reconcile"
    assert harness.calls == writes_before


async def test_flat_apply_requires_preexisting_plan(ceph_v2_harness):
    response = await ceph_v2_harness.client.post(
        "/ceph/v2/apply",
        json={"endpoint_id": ceph_v2_harness.endpoint_id, "confirmed": True},
        headers=REQUESTER,
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "persisted_plan_required"


async def test_audit_records_do_not_contain_raw_approval_token(ceph_v2_harness, db_session):
    harness = ceph_v2_harness
    plan = await _create_plan(harness)
    approval = await _approve(harness, plan["id"])
    response = await _apply(harness, plan["id"], approval["token"])
    assert response.status_code == 200
    runs = list(db_session.exec(select(CephOperationRunRecord)).all())
    serialized = repr([run.request_summary for run in runs])
    assert approval["token"] not in serialized
