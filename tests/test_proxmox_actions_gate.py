"""Tests for Issue #376 sub-PR B: operational verbs gate stub.

Pins the load-bearing trust boundary (``operational-verbs.md`` §2.3
layer 3): every operational-verb route returns 403 with a structured
``reason`` until ``ProxmoxEndpoint.allow_writes`` is True on the
target endpoint.

After sub-PR B lands, the only path that exits the 403 branch is
``_not_implemented`` returning 501. Sub-PRs C–F replace that with the
real dispatch. The 403 gate at the top of every route must remain.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.database import ApiKey, ProxmoxEndpoint, get_async_session, get_session
from proxbox_api.main import app


VERB_PATHS = [
    ("/proxmox/qemu/100/start"),
    ("/proxmox/lxc/100/start"),
    ("/proxmox/qemu/100/stop"),
    ("/proxmox/lxc/100/stop"),
    ("/proxmox/qemu/100/snapshot"),
    ("/proxmox/lxc/100/snapshot"),
    ("/proxmox/qemu/100/migrate"),
    ("/proxmox/lxc/100/migrate"),
]

# Verbs still on the sub-PR B 501 stub. As sub-PRs C–F land, their paths move
# off this list (their own test modules cover the wired dispatch path).
STUB_VERB_PATHS = [
    ("/proxmox/qemu/100/snapshot"),
    ("/proxmox/lxc/100/snapshot"),
    ("/proxmox/qemu/100/migrate"),
    ("/proxmox/lxc/100/migrate"),
]


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
        raw_key = "test-api-key-for-verb-gate-suite"
        ApiKey.store_key(session, raw_key, label="test-verb-gate")

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_async_session] = _override_get_async_session
    with TestClient(app, headers={"X-Proxbox-API-Key": raw_key}) as test_client:
        test_client.engine = engine  # type: ignore[attr-defined]
        yield test_client
    app.dependency_overrides.clear()
    asyncio.run(async_engine.dispose())


def _make_endpoint(client: TestClient, allow_writes: bool) -> int:
    with Session(client.engine) as session:  # type: ignore[attr-defined]
        endpoint = ProxmoxEndpoint(
            name="pve-test",
            ip_address="10.0.0.10",
            port=8006,
            username="root@pam",
            verify_ssl=False,
            allow_writes=allow_writes,
        )
        session.add(endpoint)
        session.commit()
        session.refresh(endpoint)
        assert endpoint.id is not None
        return endpoint.id


@pytest.mark.parametrize("path", VERB_PATHS)
def test_missing_endpoint_id_returns_403(client: TestClient, path: str):
    resp = client.post(path)
    assert resp.status_code == 403
    body = resp.json()
    assert body["reason"] == "endpoint_id_required"


@pytest.mark.parametrize("path", VERB_PATHS)
def test_unknown_endpoint_id_returns_403(client: TestClient, path: str):
    resp = client.post(path, params={"endpoint_id": 999})
    assert resp.status_code == 403
    body = resp.json()
    assert body["reason"] == "endpoint_not_found"


@pytest.mark.parametrize("path", VERB_PATHS)
def test_endpoint_with_writes_disabled_returns_403(client: TestClient, path: str):
    endpoint_id = _make_endpoint(client, allow_writes=False)
    resp = client.post(path, params={"endpoint_id": endpoint_id})
    assert resp.status_code == 403
    body = resp.json()
    assert body["reason"] == "endpoint_writes_disabled"
    assert body["endpoint_id"] == endpoint_id


@pytest.mark.parametrize("path", STUB_VERB_PATHS)
def test_endpoint_with_writes_enabled_falls_through_to_not_implemented(
    client: TestClient, path: str
):
    """Once the gate is open, sub-PR B's stub returns 501; sub-PRs C–F replace this."""
    endpoint_id = _make_endpoint(client, allow_writes=True)
    resp = client.post(path, params={"endpoint_id": endpoint_id})
    assert resp.status_code == 501
    body = resp.json()
    assert body["reason"] == "verb_not_yet_implemented"


def test_allow_writes_field_defaults_to_false():
    """The SQLModel default for allow_writes is False (gate closed by default)."""
    assert ProxmoxEndpoint.model_fields["allow_writes"].default is False
