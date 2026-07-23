"""Ceph HTTP fixtures that avoid the deprecated synchronous TestClient."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlmodel import Session

from proxbox_api.app.exceptions import register_exception_handlers
from proxbox_api.ceph.v2_routes import router as ceph_v2_router
from proxbox_api.credentials import reset_encryption_cache
from proxbox_api.database import get_async_session
from proxbox_api.session.proxmox_providers import proxmox_sessions_dep


class _AsyncSessionFacade:
    """Awaitable facade over the test transaction's synchronous SQLite session."""

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


@pytest.fixture(autouse=True)
def ceph_safety_environment(monkeypatch):
    """Enable write tests explicitly while production remains default-off."""

    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "ceph-test-binding-key")
    monkeypatch.setenv("PROXBOX_ENABLE_CEPH_V2_WRITES", "true")
    monkeypatch.setenv("PROXBOX_CEPH_TRUSTED_ACTOR_GATEWAY", "true")
    reset_encryption_cache()
    yield
    reset_encryption_cache()


@pytest.fixture
async def ceph_http_client(db_engine):
    """Minimal Ceph v2 ASGI client with deterministic in-process DB dependencies."""

    test_app = FastAPI()
    register_exception_handlers(test_app)
    test_app.include_router(ceph_v2_router, prefix="/ceph/v2")

    async def override_get_async_session():
        with Session(db_engine) as session:
            yield _AsyncSessionFacade(session)

    async def override_proxmox_sessions():
        return []

    test_app.dependency_overrides[get_async_session] = override_get_async_session
    test_app.dependency_overrides[proxmox_sessions_dep] = override_proxmox_sessions
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        yield client
