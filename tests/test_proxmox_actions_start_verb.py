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
from proxbox_api.services.idempotency import get_idempotency_cache


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
    journal_mock = AsyncMock(return_value=journal_entry)

    patches = [
        patch("proxbox_api.routes.proxmox_actions._open_proxmox_session", open_session),
        patch("proxbox_api.routes.proxmox_actions.get_netbox_async_session", nb_session),
        patch("proxbox_api.routes.proxmox_actions.resolve_proxmox_node", node_mock),
        patch("proxbox_api.routes.proxmox_actions.resolve_netbox_vm_id", netbox_id_mock),
        patch("proxbox_api.routes.proxmox_actions.get_vm_status", status_mock),
        patch("proxbox_api.routes.proxmox_actions.start_vm", start_mock),
        patch(
            "proxbox_api.routes.proxmox_actions.write_verb_journal_entry",
            journal_mock,
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
        "journal": journal_mock,
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
    handles["journal"].assert_awaited_once()
    journal_kwargs = handles["journal"].call_args.kwargs
    assert journal_kwargs["netbox_vm_id"] == 42
    assert journal_kwargs["kind"] == "info"
    assert "verb: start" in journal_kwargs["comments"]
    assert "actor: alice@netbox" in journal_kwargs["comments"]
    assert "result: ok" in journal_kwargs["comments"]
    assert "idempotency_key: key-abc" in journal_kwargs["comments"]


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


def test_start_qemu_no_matching_netbox_vm_still_dispatches(client: TestClient):
    """When no NetBox VM carries the matching cf, dispatch proceeds (no audit URL)."""
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(netbox_vm_id=None)
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post("/proxmox/qemu/100/start", params={"endpoint_id": endpoint_id})
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "ok"
    assert "journal_entry_url" not in body
    handles["journal"].assert_not_awaited()
    handles["start"].assert_awaited_once()
