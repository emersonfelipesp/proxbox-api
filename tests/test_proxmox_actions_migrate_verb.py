"""Tests for the migrate verb wire-up (issue #376 sub-PR F).

Pins ``docs/design/operational-verbs.md`` §4 (idempotency), §5
(cancellation), §6 (audit invariant), §7.1 (SSE channel and event
names) and §9 (preflight 400 rejection rules).

Migrate is the only async verb: the POST returns 202 with a
``proxmox_task_upid`` and ``sse_url``, the operator opens the SSE
stream to receive progress, and may cancel via the DELETE endpoint.
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
        raw_key = "test-api-key-for-migrate-verb-suite"
        ApiKey.store_key(session, raw_key, label="test-migrate-verb")

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_async_session] = _override_get_async_session

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
    node_or_response="pve-node-01",
    netbox_vm_id: int | None = 42,
    preflight_payload: dict | None = None,
    preflight_side_effect=None,
    migrate_result="UPID:pve-node-01:0001:migrate",
    migrate_side_effect=None,
    cancel_side_effect=None,
    task_status_payload=None,
    journal_entry: dict | None = None,
):
    if preflight_payload is None:
        preflight_payload = {
            "allowed_nodes": ["pve-node-02", "pve-node-03"],
            "local_disks": [],
            "local_resources": [],
            "running": 1,
        }
    if journal_entry is None:
        journal_entry = {"id": 792, "url": "/api/extras/journal-entries/792/"}

    open_session = AsyncMock(return_value=object())
    nb_session = AsyncMock(return_value=object())
    node_mock = AsyncMock(return_value=node_or_response)
    netbox_id_mock = AsyncMock(return_value=netbox_vm_id)
    preflight_mock = AsyncMock(return_value=preflight_payload, side_effect=preflight_side_effect)
    migrate_mock = AsyncMock(return_value=migrate_result, side_effect=migrate_side_effect)
    cancel_mock = AsyncMock(return_value=None, side_effect=cancel_side_effect)
    task_status_mock = AsyncMock(
        return_value=task_status_payload or SimpleNamespace(status="stopped", exitstatus="OK")
    )
    journal_mock = AsyncMock(return_value=journal_entry)

    patches = [
        patch("proxbox_api.routes.proxmox_actions._open_proxmox_session", open_session),
        patch("proxbox_api.routes.proxmox_actions.get_netbox_async_session", nb_session),
        patch("proxbox_api.routes.proxmox_actions.resolve_proxmox_node", node_mock),
        patch("proxbox_api.routes.proxmox_actions.resolve_netbox_vm_id", netbox_id_mock),
        patch("proxbox_api.routes.proxmox_actions.migrate_preflight", preflight_mock),
        patch("proxbox_api.routes.proxmox_actions.migrate_vm", migrate_mock),
        patch("proxbox_api.routes.proxmox_actions.cancel_task", cancel_mock),
        patch(
            "proxbox_api.routes.proxmox_actions.get_node_task_status",
            task_status_mock,
        ),
        patch(
            "proxbox_api.routes.proxmox_actions.write_verb_journal_entry",
            journal_mock,
        ),
    ]

    return {
        "patches": patches,
        "node": node_mock,
        "preflight": preflight_mock,
        "migrate": migrate_mock,
        "cancel": cancel_mock,
        "task_status": task_status_mock,
        "journal": journal_mock,
    }


# ---------------------------------------------------------------------------
# §9 preflight rejection
# ---------------------------------------------------------------------------


def test_migrate_qemu_target_not_allowed_returns_400(client: TestClient):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/qemu/100/migrate",
            params={"endpoint_id": endpoint_id},
            json={"target": "pve-bogus-node"},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 400
    body = resp.json()
    assert body["reason"] == "target_not_allowed"
    assert body["result"] == "rejected"
    assert "preflight" in body
    assert body["preflight"]["allowed_nodes"] == ["pve-node-02", "pve-node-03"]
    handles["migrate"].assert_not_awaited()
    handles["journal"].assert_awaited_once()
    assert handles["journal"].call_args.kwargs["kind"] == "warning"


def test_migrate_qemu_online_with_local_disks_returns_400(client: TestClient):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(
        preflight_payload={
            "allowed_nodes": ["pve-node-02"],
            "local_disks": [{"volid": "local:vm-100-disk-0"}],
            "local_resources": [],
            "running": 1,
        }
    )
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/qemu/100/migrate",
            params={"endpoint_id": endpoint_id},
            json={"target": "pve-node-02", "online": True},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 400
    body = resp.json()
    assert body["reason"] == "local_disks_block_online_migrate"
    handles["migrate"].assert_not_awaited()


def test_migrate_qemu_online_with_local_resources_returns_400(client: TestClient):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(
        preflight_payload={
            "allowed_nodes": ["pve-node-02"],
            "local_disks": [],
            "local_resources": [{"name": "hostpci0"}],
            "running": 1,
        }
    )
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/qemu/100/migrate",
            params={"endpoint_id": endpoint_id},
            json={"target": "pve-node-02", "online": True},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 400
    body = resp.json()
    assert body["reason"] == "local_resources_block_online_migrate"
    handles["migrate"].assert_not_awaited()


# ---------------------------------------------------------------------------
# Missing-target gate (validation runs after the §2.3 trust boundary)
# ---------------------------------------------------------------------------


def test_migrate_qemu_missing_target_returns_400_after_gate(client: TestClient):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/qemu/100/migrate",
            params={"endpoint_id": endpoint_id},
            json={},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 400
    body = resp.json()
    assert body["reason"] == "target_required"
    handles["preflight"].assert_not_awaited()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_migrate_qemu_success_returns_202_with_sse_url_and_journal(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/qemu/100/migrate",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "mig-key-1", "X-Proxbox-Actor": "alice@netbox"},
            json={"target": "pve-node-02", "online": False},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 202
    body = resp.json()
    assert body["verb"] == "migrate"
    assert body["vmid"] == 100
    assert body["vm_type"] == "qemu"
    assert body["endpoint_id"] == endpoint_id
    assert body["result"] == "accepted"
    assert body["proxmox_task_upid"] == "UPID:pve-node-01:0001:migrate"
    assert body["sse_url"] == ("/proxmox/qemu/100/migrate/UPID:pve-node-01:0001:migrate/stream")
    assert body["target"] == "pve-node-02"
    assert body["online"] is False
    assert body["source_node"] == "pve-node-01"
    assert body["journal_entry_url"] == "/api/extras/journal-entries/792/"
    handles["migrate"].assert_awaited_once()
    handles["journal"].assert_awaited_once()
    assert handles["journal"].call_args.kwargs["kind"] == "info"
    assert "verb: migrate" in handles["journal"].call_args.kwargs["comments"]


def test_migrate_qemu_idempotency_key_reuse_returns_cached_202(client: TestClient):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        r1 = client.post(
            "/proxmox/qemu/100/migrate",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "mig-reuse-1"},
            json={"target": "pve-node-02"},
        )
        r2 = client.post(
            "/proxmox/qemu/100/migrate",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "mig-reuse-1"},
            json={"target": "pve-node-02"},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json() == r2.json()
    assert handles["migrate"].await_count == 1
    assert handles["journal"].await_count == 1


def test_migrate_qemu_proxmox_dispatch_failure_writes_warning_journal(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(migrate_side_effect=ProxmoxAPIError(message="cluster lock held"))
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/qemu/100/migrate",
            params={"endpoint_id": endpoint_id},
            json={"target": "pve-node-02"},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 502
    body = resp.json()
    assert body["result"] == "failed"
    assert body["reason"] == "proxmox_dispatch_failed"
    assert "cluster lock held" in body["detail"]
    handles["journal"].assert_awaited_once()
    assert handles["journal"].call_args.kwargs["kind"] == "warning"


def test_migrate_lxc_routes_through_same_dispatch(client: TestClient):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/lxc/101/migrate",
            params={"endpoint_id": endpoint_id},
            json={"target": "pve-node-02"},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 202
    body = resp.json()
    assert body["vm_type"] == "lxc"
    assert body["vmid"] == 101
    node_call = handles["node"].call_args
    assert node_call.args[1] == "lxc" or node_call.kwargs.get("vm_type") == "lxc"


# ---------------------------------------------------------------------------
# Cancel endpoint (§5)
# ---------------------------------------------------------------------------


def test_migrate_qemu_cancel_returns_200_and_writes_journal(client: TestClient):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.delete(
            "/proxmox/qemu/100/migrate/UPID:pve-node-01:0001:migrate",
            params={"endpoint_id": endpoint_id},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 200
    body = resp.json()
    assert body["verb"] == "migrate"
    assert body["result"] == "cancel_requested"
    assert body["proxmox_task_upid"] == "UPID:pve-node-01:0001:migrate"
    handles["cancel"].assert_awaited_once()
    handles["journal"].assert_awaited_once()
    assert handles["journal"].call_args.kwargs["kind"] == "info"


def test_migrate_qemu_cancel_proxmox_failure_writes_warning(client: TestClient):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(cancel_side_effect=ProxmoxAPIError(message="task already gone"))
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.delete(
            "/proxmox/qemu/100/migrate/UPID:pve-node-01:0001:migrate",
            params={"endpoint_id": endpoint_id},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 502
    body = resp.json()
    assert body["result"] == "cancel_failed"
    assert body["reason"] == "proxmox_cancel_failed"
    handles["journal"].assert_awaited_once()
    assert handles["journal"].call_args.kwargs["kind"] == "warning"


# ---------------------------------------------------------------------------
# SSE stream endpoint (§7.1)
# ---------------------------------------------------------------------------


def test_migrate_qemu_stream_emits_dispatched_and_succeeded(client: TestClient):
    endpoint_id = _make_endpoint(client)
    # Task is already complete on the first poll → stream emits
    # ``migrate_dispatched`` then ``migrate_succeeded`` and closes.
    handles = _patch_route(task_status_payload=SimpleNamespace(status="stopped", exitstatus="OK"))
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.get(
            "/proxmox/qemu/100/migrate/UPID:pve-node-01:0001:migrate/stream",
            params={"endpoint_id": endpoint_id},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 200
    text = resp.text
    assert "event: migrate_dispatched" in text
    assert "event: migrate_succeeded" in text
    assert "UPID:pve-node-01:0001:migrate" in text


def test_migrate_qemu_stream_emits_failed_on_nonzero_exitstatus(client: TestClient):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(
        task_status_payload=SimpleNamespace(
            status="stopped", exitstatus="migration failed: timeout"
        )
    )
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.get(
            "/proxmox/lxc/101/migrate/UPID:pve-node-01:0002:migrate/stream",
            params={"endpoint_id": endpoint_id},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 200
    text = resp.text
    assert "event: migrate_dispatched" in text
    assert "event: migrate_failed" in text
    assert "migration failed: timeout" in text
