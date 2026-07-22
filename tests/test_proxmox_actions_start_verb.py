"""Tests for the start verb wire-up (issue #376 sub-PR C).

Pins the contracts in ``docs/design/operational-verbs.md`` §4 (idempotency),
§4.2 (state-based no-op), §6 (audit invariant) and §7.3 (response shape).

The Proxmox-side I/O surface (``_open_proxmox_session``, ``get_vm_status``,
``start_vm``, ``resolve_proxmox_node``) and the NetBox-side I/O surface
(``get_netbox_async_session``, ``resolve_netbox_vm_id``,
``write_verb_journal_entry``) are patched on the route module so the gate +
dispatch + audit + cache contracts can be exercised without a live cluster.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.database import ApiKey, ProxmoxEndpoint, get_async_session, get_session
from proxbox_api.exception import ProxmoxAPIError
from proxbox_api.main import app
from proxbox_api.routes import proxmox_actions
from proxbox_api.services.idempotency import CacheKey, get_idempotency_cache


@pytest.fixture
def client(tmp_path: Path):
    sqlite_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{sqlite_file}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    async_url = str(engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    async_engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    def _override_get_session():
        with Session(engine) as session:
            yield session

    async def _override_get_async_session():
        async with session_factory() as session:
            yield session

    with Session(engine) as session:
        raw_key = "test-api-key-for-start-verb-suite"
        ApiKey.store_key(session, raw_key, label="test-start-verb")

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_async_session] = _override_get_async_session

    # Clear the singleton idempotency cache so prior-test entries can't leak in.
    asyncio.run(get_idempotency_cache().clear())

    with TestClient(app, headers={"X-Proxbox-API-Key": raw_key}) as test_client:
        test_client.engine = engine  # type: ignore[attr-defined]
        yield test_client
    app.dependency_overrides.clear()
    asyncio.run(async_engine.dispose())


def _make_endpoint(client: TestClient) -> int:
    with Session(client.engine) as session:  # type: ignore[attr-defined]
        endpoint = ProxmoxEndpoint(
            name="pve-prod",
            ip_address="10.0.0.10",
            port=8006,
            username="root@pam",
            verify_ssl=False,
            allow_writes=True,
        )
        session.add(endpoint)
        session.commit()
        session.refresh(endpoint)
        endpoint_id = endpoint.id
        assert endpoint_id is not None
        return endpoint_id


class _GateSession:
    def __init__(self, endpoint: ProxmoxEndpoint) -> None:
        self.endpoint = endpoint

    async def get(self, model: object, object_id: int) -> ProxmoxEndpoint | None:
        if model is ProxmoxEndpoint and object_id == self.endpoint.id:
            return self.endpoint
        return None


def _route_session() -> _GateSession:
    return _GateSession(
        ProxmoxEndpoint(
            id=73,
            name="pve-prod",
            ip_address="10.0.0.10",
            port=8006,
            username="root@pam",
            verify_ssl=False,
            allow_writes=True,
        )
    )


def _json_response(response) -> dict[str, object]:
    return json.loads(response.body)


async def _call_start(
    session: _GateSession,
    *,
    idempotency_key: str | None = None,
    actor: str = "proxbox-api",
):
    return await proxmox_actions._handle_start(
        "qemu",
        100,
        session,  # type: ignore[arg-type]
        session.endpoint.id,
        idempotency_key,
        actor,
    )


def _patch_route(
    *,
    proxmox_session=None,
    netbox_session=None,
    node_or_response="pve-node-01",
    netbox_vm_id: int | None = 42,
    status_payload=SimpleNamespace(status="stopped"),
    start_result="UPID:pve-node-01:0001:start",
    journal_entry: dict | None = None,
    start_side_effect=None,
    status_side_effect=None,
    journal_create_side_effect=None,
    journal_update_side_effect=None,
):
    """Patch every I/O symbol on the route module in one go.

    Returns a dict of mock handles so individual tests can assert call counts.
    """
    if journal_entry is None:
        journal_entry = {"id": 789, "url": "/api/extras/journal-entries/789/"}

    open_session = AsyncMock(return_value=proxmox_session or object())
    nb_session = AsyncMock(return_value=netbox_session or object())
    node_mock = AsyncMock(return_value=node_or_response)
    netbox_id_mock = AsyncMock(return_value=netbox_vm_id)
    status_mock = AsyncMock(return_value=status_payload, side_effect=status_side_effect)
    start_mock = AsyncMock(return_value=start_result, side_effect=start_side_effect)
    journal_create_mock = AsyncMock(
        return_value=journal_entry,
        side_effect=journal_create_side_effect,
    )
    journal_update_mock = AsyncMock(
        return_value=journal_entry,
        side_effect=journal_update_side_effect,
    )

    patches = [
        patch("proxbox_api.routes.proxmox_actions._open_proxmox_session", open_session),
        patch("proxbox_api.routes.proxmox_actions.get_netbox_async_session", nb_session),
        patch("proxbox_api.routes.proxmox_actions.resolve_proxmox_node", node_mock),
        patch("proxbox_api.routes.proxmox_actions.resolve_netbox_vm_id", netbox_id_mock),
        patch("proxbox_api.routes.proxmox_actions.get_vm_status", status_mock),
        patch("proxbox_api.routes.proxmox_actions.start_vm", start_mock),
        patch(
            "proxbox_api.routes.proxmox_actions.write_verb_journal_entry",
            journal_create_mock,
        ),
        patch(
            "proxbox_api.routes.proxmox_actions.update_verb_journal_entry",
            journal_update_mock,
        ),
    ]

    return {
        "patches": patches,
        "open_session": open_session,
        "nb_session": nb_session,
        "node": node_mock,
        "netbox_id": netbox_id_mock,
        "status": status_mock,
        "start": start_mock,
        "journal": journal_update_mock,
        "journal_create": journal_create_mock,
    }


def test_start_qemu_success_returns_response_shape_and_writes_journal(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/qemu/100/start",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "key-abc", "X-Proxbox-Actor": "alice@netbox"},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 200
    body = resp.json()
    assert body["verb"] == "start"
    assert body["vmid"] == 100
    assert body["vm_type"] == "qemu"
    assert body["endpoint_id"] == endpoint_id
    assert body["result"] == "ok"
    assert body["proxmox_task_upid"] == "UPID:pve-node-01:0001:start"
    assert body["journal_entry_url"] == "/api/extras/journal-entries/789/"
    assert "dispatched_at" in body

    handles["start"].assert_awaited_once()
    handles["journal_create"].assert_awaited_once()
    assert handles["journal_create"].call_args.kwargs["netbox_vm_id"] == 42
    handles["journal"].assert_awaited_once()
    journal_kwargs = handles["journal"].call_args.kwargs
    assert journal_kwargs["kind"] == "info"
    assert "verb: start" in journal_kwargs["comments"]
    assert "actor: alice@netbox" in journal_kwargs["comments"]
    assert "result: ok" in journal_kwargs["comments"]
    assert "idempotency_key: key-abc" in journal_kwargs["comments"]


def test_start_qemu_creates_writeahead_journal_before_dispatch(client: TestClient):
    endpoint_id = _make_endpoint(client)
    events: list[str] = []

    async def _create_journal(_nb, **kwargs):
        events.append("journal_create")
        assert kwargs["netbox_vm_id"] == 42
        assert kwargs["kind"] == "info"
        assert "result: in_progress" in kwargs["comments"]
        return {"id": 789, "url": "/api/extras/journal-entries/789/"}

    async def _start_vm(*_args, **_kwargs):
        events.append("start_dispatch")
        return "UPID:pve-node-01:0001:start"

    handles = _patch_route(
        start_side_effect=_start_vm,
        journal_create_side_effect=_create_journal,
    )
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post("/proxmox/qemu/100/start", params={"endpoint_id": endpoint_id})
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 200
    assert events.index("journal_create") < events.index("start_dispatch")
    handles["journal_create"].assert_awaited_once()
    handles["journal"].assert_awaited_once()


def test_start_qemu_journal_update_failure_is_visible_and_retried_without_redispatch(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(journal_update_side_effect=RuntimeError("netbox patch down"))
    for p in handles["patches"]:
        p.start()
    try:
        resp1 = client.post(
            "/proxmox/qemu/100/start",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "key-finalize-failure"},
        )
        resp2 = client.post(
            "/proxmox/qemu/100/start",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "key-finalize-failure"},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp1.status_code == 502
    body = resp1.json()
    assert body["result"] == "ok"
    assert body["proxmox_task_upid"] == "UPID:pve-node-01:0001:start"
    assert body["journal_entry_url"] == "/api/extras/journal-entries/789/"
    assert body["journal_finalized"] is False
    assert body["reason"] == "netbox_journal_finalization_failed"
    assert "netbox patch down" in body["finalization_error"]

    assert resp2.status_code == 502
    assert resp2.json()["journal_finalized"] is False
    handles["journal_create"].assert_awaited_once()
    assert "result: in_progress" in handles["journal_create"].call_args.kwargs["comments"]
    assert handles["journal"].await_count == 2
    handles["start"].assert_awaited_once()


def test_start_qemu_idempotency_retry_finalizes_existing_entry_without_redispatch(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(
        journal_update_side_effect=[
            RuntimeError("netbox patch down"),
            {"id": 789, "url": "/api/extras/journal-entries/789/"},
        ],
    )
    for p in handles["patches"]:
        p.start()
    try:
        resp1 = client.post(
            "/proxmox/qemu/100/start",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "key-finalize-retry"},
        )
        resp2 = client.post(
            "/proxmox/qemu/100/start",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "key-finalize-retry"},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp1.status_code == 502
    assert resp1.json()["journal_finalized"] is False
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["result"] == "ok"
    assert body["journal_entry_url"] == "/api/extras/journal-entries/789/"
    assert body.get("journal_finalized", True) is True
    handles["journal_create"].assert_awaited_once()
    assert handles["journal"].await_count == 2
    handles["start"].assert_awaited_once()


def test_start_qemu_writeahead_journal_create_failure_blocks_dispatch(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(journal_create_side_effect=RuntimeError("netbox create down"))
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post("/proxmox/qemu/100/start", params={"endpoint_id": endpoint_id})
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 409
    body = resp.json()
    assert body["reason"] == "netbox_vm_identity_required_for_audit"
    assert body["verb"] == "start"
    assert body["vmid"] == 100
    assert body["endpoint_id"] == endpoint_id
    handles["journal_create"].assert_awaited_once()
    handles["status"].assert_not_awaited()
    handles["start"].assert_not_awaited()
    handles["journal"].assert_not_awaited()


def test_start_qemu_idempotency_key_reuse_returns_cached_response(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        resp1 = client.post(
            "/proxmox/qemu/100/start",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "key-reuse-1"},
        )
        resp2 = client.post(
            "/proxmox/qemu/100/start",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "key-reuse-1"},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json() == resp2.json()
    # The cached response means the dispatch + journal happened only once.
    assert handles["start"].await_count == 1
    assert handles["journal"].await_count == 1


@pytest.mark.asyncio
async def test_start_qemu_concurrent_same_idempotency_key_single_dispatches():
    route_session = _route_session()
    dispatch_started = asyncio.Event()
    finish_dispatch = asyncio.Event()

    async def _start_vm(*_args, **_kwargs):
        dispatch_started.set()
        await finish_dispatch.wait()
        return "UPID:pve-node-01:0001:start"

    handles = _patch_route(start_side_effect=_start_vm)
    for patcher in handles["patches"]:
        patcher.start()
    try:
        first = asyncio.create_task(
            _call_start(route_session, idempotency_key="start-single-flight")
        )
        await asyncio.wait_for(dispatch_started.wait(), timeout=1)
        second = asyncio.create_task(
            _call_start(route_session, idempotency_key="start-single-flight")
        )
        await asyncio.sleep(0)
        assert handles["journal_create"].await_count == 1
        assert handles["start"].await_count == 1

        finish_dispatch.set()
        first_response, second_response = await asyncio.gather(first, second)
    finally:
        for patcher in handles["patches"]:
            patcher.stop()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert _json_response(first_response) == _json_response(second_response)
    handles["journal_create"].assert_awaited_once()
    handles["start"].assert_awaited_once()
    handles["journal"].assert_awaited_once()


@pytest.mark.asyncio
async def test_start_qemu_concurrent_unfinalized_cache_hit_retries_once():
    route_session = _route_session()
    cache = get_idempotency_cache()
    await cache.clear()
    cache_key = CacheKey(
        endpoint_id=73,
        verb="start",
        vmid=100,
        key="start-finalize-single-flight",
    )
    metadata = proxmox_actions._journal_finalization_retry_metadata(
        journal_entry_id=789,
        journal_entry_url="/api/extras/journal-entries/789/",
        kind="info",
        comments="verb: start\nresult: ok",
        interrupted_comments="verb: start\nresult: interrupted",
        failed_comments="verb: start\nresult: failed",
        terminal_status_code=200,
    )
    await cache.store(
        cache_key,
        {
            "verb": "start",
            "vmid": 100,
            "vm_type": "qemu",
            "endpoint_id": 73,
            "result": "ok",
            "dispatched_at": "2026-07-22T12:00:00Z",
            "proxmox_task_upid": "UPID:pve-node-01:0001:start",
            "journal_entry_url": "/api/extras/journal-entries/789/",
            "journal_finalized": False,
            "finalization_error": "netbox patch down",
            "reason": "netbox_journal_finalization_failed",
        },
        status_code=502,
        journal_finalization=metadata,
    )
    retry_started = asyncio.Event()
    finish_retry = asyncio.Event()

    async def _update_journal(_nb, **_kwargs):
        retry_started.set()
        await finish_retry.wait()
        return {"id": 789, "url": "/api/extras/journal-entries/789/"}

    handles = _patch_route(journal_update_side_effect=_update_journal)
    for patcher in handles["patches"]:
        patcher.start()
    try:
        first = asyncio.create_task(
            _call_start(route_session, idempotency_key="start-finalize-single-flight")
        )
        await asyncio.wait_for(retry_started.wait(), timeout=1)
        second = asyncio.create_task(
            _call_start(route_session, idempotency_key="start-finalize-single-flight")
        )
        await asyncio.sleep(0)
        assert handles["journal"].await_count == 1

        finish_retry.set()
        first_response, second_response = await asyncio.gather(first, second)
    finally:
        for patcher in handles["patches"]:
            patcher.stop()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert _json_response(first_response) == _json_response(second_response)
    handles["journal"].assert_awaited_once()
    handles["journal_create"].assert_not_awaited()
    handles["start"].assert_not_awaited()
    cached = await cache.get_entry(cache_key)
    assert cached is not None
    assert cached.status_code == 200
    assert cached.journal_finalization is None
    assert "journal_finalized" not in cached.response
    assert "finalization_error" not in cached.response


def test_start_qemu_already_running_skips_dispatch_but_writes_journal(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(status_payload=SimpleNamespace(status="running"))
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post("/proxmox/qemu/100/start", params={"endpoint_id": endpoint_id})
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "already_running"
    assert "proxmox_task_upid" not in body
    # No dispatch — but the journal entry is still written (§6.2).
    handles["start"].assert_not_awaited()
    handles["journal"].assert_awaited_once()
    assert handles["journal"].call_args.kwargs["kind"] == "info"


def test_start_qemu_proxmox_dispatch_failure_writes_warning_journal(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(start_side_effect=ProxmoxAPIError(message="lock conflict"))
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post("/proxmox/qemu/100/start", params={"endpoint_id": endpoint_id})
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 502
    body = resp.json()
    assert body["result"] == "failed"
    assert body["reason"] == "proxmox_dispatch_failed"
    assert "lock conflict" in body["detail"]
    # Failure path still writes exactly one journal entry, kind=warning (§6.2).
    handles["journal"].assert_awaited_once()
    assert handles["journal"].call_args.kwargs["kind"] == "warning"
    assert "error_detail: " in handles["journal"].call_args.kwargs["comments"]


def test_start_qemu_idempotency_key_reuse_after_dispatch_failure_returns_cached_failure(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(start_side_effect=ProxmoxAPIError(message="lock conflict"))
    for p in handles["patches"]:
        p.start()
    try:
        resp1 = client.post(
            "/proxmox/qemu/100/start",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "key-dispatch-failure"},
        )
        resp2 = client.post(
            "/proxmox/qemu/100/start",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "key-dispatch-failure"},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp1.status_code == 502
    assert resp2.status_code == 502
    assert resp1.json() == resp2.json()
    handles["journal_create"].assert_awaited_once()
    handles["start"].assert_awaited_once()
    handles["journal"].assert_awaited_once()


@pytest.mark.asyncio
async def test_start_qemu_cancelled_writeahead_create_finalizes_same_entry_and_caches_retry():
    route_session = _route_session()
    cache = get_idempotency_cache()
    await cache.clear()
    create_committed = asyncio.Event()
    release_create = asyncio.Event()

    async def _create_journal(_nb, **kwargs):
        assert "result: in_progress" in kwargs["comments"]
        create_committed.set()
        await release_create.wait()
        return {"id": 789, "url": "/api/extras/journal-entries/789/"}

    handles = _patch_route(journal_create_side_effect=_create_journal)
    for patcher in handles["patches"]:
        patcher.start()
    try:
        first = asyncio.create_task(
            _call_start(
                route_session,
                idempotency_key="key-cancel-writeahead-create",
                actor="alice@netbox",
            )
        )
        await asyncio.wait_for(create_committed.wait(), timeout=1)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        release_create.set()
        with pytest.raises(asyncio.CancelledError):
            await first

        retry = await _call_start(
            route_session,
            idempotency_key="key-cancel-writeahead-create",
            actor="alice@netbox",
        )
    finally:
        for patcher in handles["patches"]:
            patcher.stop()

    handles["journal_create"].assert_awaited_once()
    handles["journal"].assert_awaited_once()
    journal_kwargs = handles["journal"].call_args.kwargs
    assert journal_kwargs["journal_entry_id"] == 789
    assert journal_kwargs["kind"] == "warning"
    assert "result: interrupted" in journal_kwargs["comments"]
    handles["status"].assert_not_awaited()
    handles["start"].assert_not_awaited()

    assert retry.status_code == 500
    body = _json_response(retry)
    assert body["result"] == "interrupted"
    assert body["journal_entry_url"] == "/api/extras/journal-entries/789/"
    assert body["reason"] == "proxmox_dispatch_interrupted"
    cached = await cache.get_entry(
        CacheKey(
            endpoint_id=73,
            verb="start",
            vmid=100,
            key="key-cancel-writeahead-create",
        )
    )
    assert cached is not None
    assert cached.journal_finalization is None


@pytest.mark.asyncio
async def test_start_qemu_cancelled_dispatch_cleanup_survives_second_cancel_and_caches():
    route_session = _route_session()
    cache = get_idempotency_cache()
    await cache.clear()
    dispatch_started = asyncio.Event()
    patch_started = asyncio.Event()
    release_patch = asyncio.Event()
    patched_comments: list[str] = []

    async def _start_vm(*_args, **_kwargs):
        dispatch_started.set()
        await asyncio.Event().wait()

    async def _update_journal(_nb, **kwargs):
        patch_started.set()
        await release_patch.wait()
        patched_comments.append(kwargs["comments"])
        return {"id": 789, "url": "/api/extras/journal-entries/789/"}

    handles = _patch_route(
        start_side_effect=_start_vm,
        journal_update_side_effect=_update_journal,
    )
    for patcher in handles["patches"]:
        patcher.start()
    try:
        first = asyncio.create_task(
            _call_start(
                route_session,
                idempotency_key="key-cancel-dispatch-finalize",
                actor="alice@netbox",
            )
        )
        await asyncio.wait_for(dispatch_started.wait(), timeout=1)
        first.cancel()
        await asyncio.wait_for(patch_started.wait(), timeout=1)
        first.cancel()
        await asyncio.sleep(0)
        release_patch.set()
        with pytest.raises(asyncio.CancelledError):
            await first

        retry = await _call_start(
            route_session,
            idempotency_key="key-cancel-dispatch-finalize",
            actor="alice@netbox",
        )
    finally:
        for patcher in handles["patches"]:
            patcher.stop()

    handles["journal_create"].assert_awaited_once()
    handles["journal"].assert_awaited_once()
    handles["start"].assert_awaited_once()
    assert len(patched_comments) == 1
    assert "result: interrupted" in patched_comments[0]

    assert retry.status_code == 500
    body = _json_response(retry)
    assert body["result"] == "interrupted"
    assert body["journal_entry_url"] == "/api/extras/journal-entries/789/"
    assert body["reason"] == "proxmox_dispatch_interrupted"
    cached = await cache.get_entry(
        CacheKey(
            endpoint_id=73,
            verb="start",
            vmid=100,
            key="key-cancel-dispatch-finalize",
        )
    )
    assert cached is not None
    assert cached.journal_finalization is None
    assert cached.response.get("journal_finalized") is not False


@pytest.mark.asyncio
async def test_start_empty_idempotency_key_is_unkeyed_and_not_serialized():
    route_session = _route_session()
    cache = get_idempotency_cache()
    await cache.clear()
    dispatch_count = 0
    both_dispatched = asyncio.Event()
    release_dispatch = asyncio.Event()

    async def _start_vm(*_args, **_kwargs):
        nonlocal dispatch_count
        dispatch_count += 1
        if dispatch_count == 2:
            both_dispatched.set()
        await release_dispatch.wait()
        return "UPID:pve-node-01:0001:start"

    handles = _patch_route(start_side_effect=_start_vm)
    for patcher in handles["patches"]:
        patcher.start()
    try:
        first = asyncio.create_task(_call_start(route_session, idempotency_key=""))
        second = asyncio.create_task(_call_start(route_session, idempotency_key=""))
        await asyncio.wait_for(both_dispatched.wait(), timeout=1)
        release_dispatch.set()
        first_response, second_response = await asyncio.gather(first, second)
    finally:
        for patcher in handles["patches"]:
            patcher.stop()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert handles["journal_create"].await_count == 2
    assert handles["start"].await_count == 2
    assert handles["journal"].await_count == 2
    assert cache._entries == {}
    assert cache._flights == {}


def test_start_lxc_routes_through_same_dispatch(client: TestClient):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post("/proxmox/lxc/101/start", params={"endpoint_id": endpoint_id})
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 200
    body = resp.json()
    assert body["vm_type"] == "lxc"
    assert body["vmid"] == 101
    # The node resolver was invoked with vm_type="lxc".
    node_call = handles["node"].call_args
    assert node_call.args[1] == "lxc" or node_call.kwargs.get("vm_type") == "lxc"


def test_start_qemu_no_matching_netbox_vm_fails_closed_before_dispatch(client: TestClient):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(netbox_vm_id=None)
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post("/proxmox/qemu/100/start", params={"endpoint_id": endpoint_id})
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 409
    body = resp.json()
    assert body["reason"] == "netbox_vm_identity_required_for_audit"
    assert body["verb"] == "start"
    assert body["vmid"] == 100
    assert body["endpoint_id"] == endpoint_id
    handles["status"].assert_not_awaited()
    handles["journal"].assert_not_awaited()
    handles["start"].assert_not_awaited()


@pytest.mark.asyncio
async def test_audit_and_respond_cancelled_during_finalize_shields_terminal_patch():
    started = asyncio.Event()
    finish = asyncio.Event()
    patched_comments: list[str] = []

    async def _update_journal(_nb, **kwargs):
        started.set()
        try:
            await finish.wait()
        except asyncio.CancelledError:
            patched_comments.append("cancelled")
            raise
        patched_comments.append(kwargs["comments"])
        return {"id": 789, "url": "/api/extras/journal-entries/789/"}

    endpoint = ProxmoxEndpoint(
        id=7,
        name="pve-prod",
        ip_address="10.0.0.10",
        username="root@pam",
        allow_writes=True,
    )
    cache = get_idempotency_cache()
    await cache.clear()
    cache_key = CacheKey(endpoint_id=7, verb="start", vmid=100, key="key-cancel-finalize")

    with patch(
        "proxbox_api.routes.proxmox_actions.update_verb_journal_entry",
        AsyncMock(side_effect=_update_journal),
    ) as journal_update:
        task = asyncio.create_task(
            proxmox_actions._audit_and_respond(
                nb=object(),
                netbox_vm_id=42,
                writeahead_journal_entry={
                    "id": 789,
                    "url": "/api/extras/journal-entries/789/",
                },
                verb="start",
                vm_type="qemu",
                vmid=100,
                endpoint=endpoint,
                actor="alice@netbox",
                dispatched_at="2026-07-22T12:00:00Z",
                idempotency_key="key-cancel-finalize",
                cache=cache,
                cache_key=cache_key,
                result="ok",
                kind="info",
                proxmox_task_upid="UPID:pve-node-01:0001:start",
                error_detail=None,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert journal_update.await_count == 1
    assert len(patched_comments) == 1
    assert patched_comments[0] != "cancelled"
    assert "result: ok" in patched_comments[0]
    cached = await cache.get_entry(cache_key)
    assert cached is not None
    assert cached.response["journal_finalized"] is False
    assert cached.journal_finalization is not None


@pytest.mark.asyncio
async def test_audit_and_respond_cancelled_finalize_marks_interrupted_then_reraises():
    endpoint = ProxmoxEndpoint(
        id=7,
        name="pve-prod",
        ip_address="10.0.0.10",
        username="root@pam",
        allow_writes=True,
    )
    cache = get_idempotency_cache()
    await cache.clear()
    journal_update = AsyncMock(
        side_effect=[
            asyncio.CancelledError(),
            {"id": 789, "url": "/api/extras/journal-entries/789/"},
        ]
    )

    with patch("proxbox_api.routes.proxmox_actions.update_verb_journal_entry", journal_update):
        with pytest.raises(asyncio.CancelledError):
            await proxmox_actions._audit_and_respond(
                nb=object(),
                netbox_vm_id=42,
                writeahead_journal_entry={
                    "id": 789,
                    "url": "/api/extras/journal-entries/789/",
                },
                verb="start",
                vm_type="qemu",
                vmid=100,
                endpoint=endpoint,
                actor="alice@netbox",
                dispatched_at="2026-07-22T12:00:00Z",
                idempotency_key=None,
                cache=cache,
                cache_key=None,
                result="ok",
                kind="info",
                proxmox_task_upid="UPID:pve-node-01:0001:start",
                error_detail=None,
            )

    assert journal_update.await_count == 2
    retry_kwargs = journal_update.await_args_list[1].kwargs
    assert retry_kwargs["journal_entry_id"] == 789
    assert retry_kwargs["kind"] == "warning"
    assert "result: interrupted" in retry_kwargs["comments"]
