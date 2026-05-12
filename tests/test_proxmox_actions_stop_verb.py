"""Tests for the stop verb wire-up (issue #376 sub-PR D).

Mirrors ``tests/test_proxmox_actions_start_verb.py``: pins §4 idempotency,
§4.2 state-based no-op (``already_stopped``), §6 audit invariant and §7.3
response shape for the stop verb on /proxmox/{qemu,lxc}/{vmid}/stop.
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
    engine = create_engine(
        f"sqlite:///{sqlite_file}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)

    async_url = str(engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    async_engine = create_async_engine(
        async_url, connect_args={"check_same_thread": False}
    )
    session_factory = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )

    def _override_get_session():
        with Session(engine) as session:
            yield session

    async def _override_get_async_session():
        async with session_factory() as session:
            yield session

    with Session(engine) as session:
        raw_key = "test-api-key-for-stop-verb-suite"
        ApiKey.store_key(session, raw_key, label="test-stop-verb")

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
    status_payload=SimpleNamespace(status="running"),
    stop_result="UPID:pve-node-01:0001:stop",
    journal_entry: dict | None = None,
    stop_side_effect=None,
):
    if journal_entry is None:
        journal_entry = {"id": 790, "url": "/api/extras/journal-entries/790/"}

    open_session = AsyncMock(return_value=object())
    nb_session = AsyncMock(return_value=object())
    node_mock = AsyncMock(return_value=node_or_response)
    netbox_id_mock = AsyncMock(return_value=netbox_vm_id)
    status_mock = AsyncMock(return_value=status_payload)
    stop_mock = AsyncMock(return_value=stop_result, side_effect=stop_side_effect)
    journal_mock = AsyncMock(return_value=journal_entry)

    patches = [
        patch(
            "proxbox_api.routes.proxmox_actions._open_proxmox_session", open_session
        ),
        patch(
            "proxbox_api.routes.proxmox_actions.get_netbox_async_session", nb_session
        ),
        patch("proxbox_api.routes.proxmox_actions.resolve_proxmox_node", node_mock),
        patch(
            "proxbox_api.routes.proxmox_actions.resolve_netbox_vm_id", netbox_id_mock
        ),
        patch("proxbox_api.routes.proxmox_actions.get_vm_status", status_mock),
        patch("proxbox_api.routes.proxmox_actions.stop_vm", stop_mock),
        patch(
            "proxbox_api.routes.proxmox_actions.write_verb_journal_entry",
            journal_mock,
        ),
    ]

    return {
        "patches": patches,
        "node": node_mock,
        "status": status_mock,
        "stop": stop_mock,
        "journal": journal_mock,
    }


def test_stop_qemu_success_returns_response_shape_and_writes_journal(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/qemu/100/stop",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "key-xyz", "X-Proxbox-Actor": "alice@netbox"},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 200
    body = resp.json()
    assert body["verb"] == "stop"
    assert body["vmid"] == 100
    assert body["vm_type"] == "qemu"
    assert body["endpoint_id"] == endpoint_id
    assert body["result"] == "ok"
    assert body["proxmox_task_upid"] == "UPID:pve-node-01:0001:stop"
    assert body["journal_entry_url"] == "/api/extras/journal-entries/790/"

    handles["stop"].assert_awaited_once()
    handles["journal"].assert_awaited_once()
    journal_kwargs = handles["journal"].call_args.kwargs
    assert journal_kwargs["kind"] == "info"
    assert "verb: stop" in journal_kwargs["comments"]
    assert "actor: alice@netbox" in journal_kwargs["comments"]


def test_stop_qemu_idempotency_key_reuse_returns_cached_response(client: TestClient):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        r1 = client.post(
            "/proxmox/qemu/100/stop",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "stop-key-reuse"},
        )
        r2 = client.post(
            "/proxmox/qemu/100/stop",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "stop-key-reuse"},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    assert handles["stop"].await_count == 1
    assert handles["journal"].await_count == 1


def test_stop_qemu_already_stopped_skips_dispatch_but_writes_journal(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(status_payload=SimpleNamespace(status="stopped"))
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/qemu/100/stop", params={"endpoint_id": endpoint_id}
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "already_stopped"
    assert "proxmox_task_upid" not in body
    handles["stop"].assert_not_awaited()
    handles["journal"].assert_awaited_once()


def test_stop_qemu_proxmox_dispatch_failure_writes_warning_journal(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(
        stop_side_effect=ProxmoxAPIError(message="cannot acquire lock")
    )
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/qemu/100/stop", params={"endpoint_id": endpoint_id}
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 502
    body = resp.json()
    assert body["result"] == "failed"
    assert body["reason"] == "proxmox_dispatch_failed"
    assert "cannot acquire lock" in body["detail"]
    handles["journal"].assert_awaited_once()
    assert handles["journal"].call_args.kwargs["kind"] == "warning"


def test_stop_lxc_routes_through_same_dispatch(client: TestClient):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/lxc/101/stop", params={"endpoint_id": endpoint_id}
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 200
    body = resp.json()
    assert body["vm_type"] == "lxc"
    assert body["vmid"] == 101
    node_call = handles["node"].call_args
    assert node_call.args[1] == "lxc" or node_call.kwargs.get("vm_type") == "lxc"


def test_stop_qemu_no_matching_netbox_vm_still_dispatches(client: TestClient):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(netbox_vm_id=None)
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/qemu/100/stop", params={"endpoint_id": endpoint_id}
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "ok"
    assert "journal_entry_url" not in body
    handles["journal"].assert_not_awaited()
    handles["stop"].assert_awaited_once()
