"""Tests for the snapshot verb wire-up (issue #376 sub-PR E).

Pins ``docs/design/operational-verbs.md`` §4 (idempotency), §4.2
(snapshot is **always dispatched** — no state-based no-op), §6 (audit
invariant on success **and** failure), §7.3 (response shape) and §13
(default snapshot name when ``snapname`` is omitted).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
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
        raw_key = "test-api-key-for-snapshot-verb-suite"
        ApiKey.store_key(session, raw_key, label="test-snapshot-verb")

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
    snapshot_result="UPID:pve-node-01:0001:snapshot",
    journal_entry: dict | None = None,
    snapshot_side_effect=None,
):
    if journal_entry is None:
        journal_entry = {"id": 791, "url": "/api/extras/journal-entries/791/"}

    open_session = AsyncMock(return_value=object())
    nb_session = AsyncMock(return_value=object())
    node_mock = AsyncMock(return_value=node_or_response)
    netbox_id_mock = AsyncMock(return_value=netbox_vm_id)
    snapshot_mock = AsyncMock(
        return_value=snapshot_result, side_effect=snapshot_side_effect
    )
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
        patch(
            "proxbox_api.routes.proxmox_actions.create_vm_snapshot", snapshot_mock
        ),
        patch(
            "proxbox_api.routes.proxmox_actions.write_verb_journal_entry",
            journal_mock,
        ),
    ]

    return {
        "patches": patches,
        "node": node_mock,
        "snapshot": snapshot_mock,
        "journal": journal_mock,
    }


def test_snapshot_qemu_success_returns_response_shape_and_writes_journal(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/qemu/100/snapshot",
            params={"endpoint_id": endpoint_id},
            headers={
                "Idempotency-Key": "snap-key-abc",
                "X-Proxbox-Actor": "alice@netbox",
            },
            json={"snapname": "before-upgrade", "description": "pre-upgrade snap"},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 200
    body = resp.json()
    assert body["verb"] == "snapshot"
    assert body["vmid"] == 100
    assert body["vm_type"] == "qemu"
    assert body["endpoint_id"] == endpoint_id
    assert body["result"] == "ok"
    assert body["proxmox_task_upid"] == "UPID:pve-node-01:0001:snapshot"
    assert body["journal_entry_url"] == "/api/extras/journal-entries/791/"
    assert body["snapname"] == "before-upgrade"

    handles["snapshot"].assert_awaited_once()
    call_args = handles["snapshot"].call_args
    assert "before-upgrade" in call_args.args or call_args.kwargs.get(
        "snapname"
    ) == "before-upgrade"
    handles["journal"].assert_awaited_once()
    journal_kwargs = handles["journal"].call_args.kwargs
    assert journal_kwargs["kind"] == "info"
    assert "verb: snapshot" in journal_kwargs["comments"]
    assert "actor: alice@netbox" in journal_kwargs["comments"]


def test_snapshot_qemu_default_snapname_when_only_idempotency_key(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/qemu/100/snapshot",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "abcdef1234567890"},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "ok"
    # Per §13: proxbox-{idempotency_key[:8]}
    assert body["snapname"] == "proxbox-abcdef12"
    handles["snapshot"].assert_awaited_once()


def test_snapshot_qemu_default_snapname_with_no_key_uses_utc_stamp(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/qemu/100/snapshot", params={"endpoint_id": endpoint_id}
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "ok"
    # Default UTC-stamp fallback: proxbox-YYYYMMDDTHHMMSSZ (no colons/dots).
    assert body["snapname"].startswith("proxbox-")
    suffix = body["snapname"].removeprefix("proxbox-")
    assert "T" in suffix and suffix.endswith("Z")


def test_snapshot_qemu_idempotency_key_reuse_returns_cached_response(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        r1 = client.post(
            "/proxmox/qemu/100/snapshot",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "snap-reuse-1"},
        )
        r2 = client.post(
            "/proxmox/qemu/100/snapshot",
            params={"endpoint_id": endpoint_id},
            headers={"Idempotency-Key": "snap-reuse-1"},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    # Cached: dispatch + journal happen exactly once across both calls.
    assert handles["snapshot"].await_count == 1
    assert handles["journal"].await_count == 1


def test_snapshot_qemu_proxmox_dispatch_failure_writes_warning_journal(
    client: TestClient,
):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(
        snapshot_side_effect=ProxmoxAPIError(message="snapshot name in use")
    )
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/qemu/100/snapshot",
            params={"endpoint_id": endpoint_id},
            json={"snapname": "dup-name"},
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 502
    body = resp.json()
    assert body["result"] == "failed"
    assert body["reason"] == "proxmox_dispatch_failed"
    assert "snapshot name in use" in body["detail"]
    assert body["snapname"] == "dup-name"
    handles["journal"].assert_awaited_once()
    assert handles["journal"].call_args.kwargs["kind"] == "warning"


def test_snapshot_lxc_routes_through_same_dispatch(client: TestClient):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route()
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/lxc/101/snapshot", params={"endpoint_id": endpoint_id}
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


def test_snapshot_qemu_no_matching_netbox_vm_still_dispatches(client: TestClient):
    endpoint_id = _make_endpoint(client)
    handles = _patch_route(netbox_vm_id=None)
    for p in handles["patches"]:
        p.start()
    try:
        resp = client.post(
            "/proxmox/qemu/100/snapshot", params={"endpoint_id": endpoint_id}
        )
    finally:
        for p in handles["patches"]:
            p.stop()

    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "ok"
    assert "journal_entry_url" not in body
    handles["journal"].assert_not_awaited()
    handles["snapshot"].assert_awaited_once()
