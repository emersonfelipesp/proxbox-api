"""Shared pytest fixtures and environment setup for proxbox-api tests."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

# Disable rate limiting before importing the app so the middleware is created
# with a very high threshold.  This prevents 429 responses when hundreds of
# parametrized tests hit the shared app singleton in rapid succession.
os.environ.setdefault("PROXBOX_RATE_LIMIT", "999999")

# Tests use synthetic credentials that never leave the suite, so opt into the
# plaintext credential storage path. Production startup refuses this path
# unless PROXBOX_ENCRYPTION_KEY is set; tests don't exercise on-disk storage.
os.environ.setdefault("PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS", "1")

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.database import get_async_session, get_session
from proxbox_api.main import app
from proxbox_api.netbox_rest import _reset_netbox_globals
from proxbox_api.routes.proxmox.runtime_generated import (
    clear_generated_proxmox_route_cache,
    clear_generated_proxmox_routes,
)
from proxbox_api.session.netbox import get_netbox_async_session, get_netbox_session


class FakeNetBoxSession:
    def __init__(
        self,
        *,
        status_result: Any = None,
        openapi_result: Any = None,
        status_error: Exception | None = None,
        openapi_error: Exception | None = None,
    ) -> None:
        self._status_result = status_result if status_result is not None else {"ok": True}
        self._openapi_result = (
            openapi_result
            if openapi_result is not None
            else {
                "openapi": "3.1.0",
                "paths": {},
            }
        )
        self._status_error = status_error
        self._openapi_error = openapi_error
        # Set up fake virtualization endpoint for VM routes
        self.virtualization = self._FakeVirtualization()

    class _FakeVirtualization:
        def __init__(self):
            self.virtual_machines = FakeNetBoxSession._FakeVMEndpoint()

    class _FakeVMEndpoint:
        def all(self):
            return FakeNetBoxSession._FakeAsyncIter([])

        async def get(self, *args, **kwargs):
            return None

    class _FakeAsyncIter:
        def __init__(self, items):
            self._items = items

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._items:
                raise StopAsyncIteration
            return self._items.pop(0)

    async def status(self) -> Any:
        if self._status_error:
            raise self._status_error
        return self._status_result

    async def openapi(self) -> Any:
        if self._openapi_error:
            raise self._openapi_error
        return self._openapi_result


def _async_db_override(db_engine):
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    async_engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_async_session():
        async with session_factory() as session:
            yield session

    return async_engine, override_get_async_session


@pytest.fixture(autouse=True)
def reset_fastapi_state():
    app.dependency_overrides.clear()
    clear_generated_proxmox_route_cache()
    clear_generated_proxmox_routes(app)
    app.openapi_schema = None
    _reset_netbox_globals()
    yield
    app.dependency_overrides.clear()
    clear_generated_proxmox_route_cache()
    clear_generated_proxmox_routes(app)
    app.openapi_schema = None
    _reset_netbox_globals()


@pytest.fixture
def test_api_key(db_engine, db_session):
    """Create a test API key for authenticated tests.

    This fixture sets up the dependency override for get_session so that
    the authentication middleware uses the test database.

    Returns the raw key value for use in X-Proxbox-API-Key header.
    """
    from proxbox_api.database import ApiKey

    raw_key = "test-api-key-for-unit-tests-0000000000000000"
    ApiKey.store_key(db_session, raw_key, label="test-key")

    def override_get_session():
        with Session(db_engine) as session:
            yield session

    async_engine, override_get_async_session = _async_db_override(db_engine)
    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_async_session] = override_get_async_session
    yield raw_key
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_async_session, None)
    asyncio.run(async_engine.dispose())


@pytest.fixture
def auth_headers(test_api_key):
    """Return headers dict with test API key for authenticated requests.

    Usage:
        async with AsyncClient(...) as client:
            resp = await client.get("/protected", headers=auth_headers)
    """
    return {"X-Proxbox-API-Key": test_api_key}


@pytest.fixture
async def authenticated_client(test_api_key, client_with_fake_netbox):
    """AsyncClient with test API key pre-configured in headers.

    Usage:
        async with authenticated_client as client:
            resp = await client.get("/protected")
    """
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Proxbox-API-Key": test_api_key},
    ) as client:
        yield client


@pytest.fixture
def db_engine(tmp_path: Path):
    sqlite_file = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{sqlite_file}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture
def db_session(db_engine):
    with Session(db_engine) as session:
        yield session


@pytest.fixture
def client_with_fake_netbox(db_engine):
    fake_session = FakeNetBoxSession(
        status_result={"status": "ok", "netbox_version": "4.2"},
        openapi_result={
            "openapi": "3.1.0",
            "paths": {"/api/virtualization/virtual-machines/": {"get": {}}},
        },
    )

    def override_get_session():
        with Session(db_engine) as session:
            yield session

    async_engine, override_get_async_session = _async_db_override(db_engine)
    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_async_session] = override_get_async_session
    app.dependency_overrides[get_netbox_session] = lambda: fake_session
    app.dependency_overrides[get_netbox_async_session] = lambda: fake_session
    yield fake_session
    app.dependency_overrides.pop(get_async_session, None)
    app.dependency_overrides.pop(get_netbox_session, None)
    app.dependency_overrides.pop(get_netbox_async_session, None)
    asyncio.run(async_engine.dispose())


# Mock fixtures for proxmox and netbox sessions


@pytest.fixture
def mock_proxmox_cluster_status():
    """Mock Proxmox cluster status response."""
    from unittest.mock import MagicMock

    mock = MagicMock()
    mock.cluster_status = {
        "type": "cluster",
        "nodes": [{"name": "pve01", "status": "online"}],
    }
    app.dependency_overrides[
        __import__(
            "proxbox_api.session.proxmox_providers", fromlist=["ClusterStatusDep"]
        ).ClusterStatusDep
    ] = lambda: mock
    yield mock
    app.dependency_overrides.pop(
        __import__(
            "proxbox_api.session.proxmox_providers", fromlist=["ClusterStatusDep"]
        ).ClusterStatusDep,
        None,
    )


@pytest.fixture
def mock_netbox_devices():
    """Mock NetBox device creation responses."""
    return []


@pytest.fixture
def mock_netbox_vms():
    """Mock NetBox VM creation responses."""
    return []


@pytest.fixture
def mock_proxmox_vms():
    """Mock Proxmox VM resource responses."""
    return []


@pytest.fixture
def mock_proxmox_backups():
    """Mock Proxmox backup responses."""
    return []


@pytest.fixture
def mock_netbox_vm_list():
    """Mock NetBox VM list response."""
    return []


@pytest.fixture
def mock_netbox_vm_get():
    """Mock NetBox VM get response."""
    return {}


@pytest.fixture
def mock_netbox_status():
    """Mock NetBox status response."""
    return {"status": "ok", "netbox_version": "4.2"}


@pytest.fixture
def mock_proxmox_from_netbox_plugin():
    """Mock Proxmox endpoint fetched from NetBox plugin API."""
    return []


@pytest.fixture
def mock_proxmox_connection_error():
    """Mock Proxmox connection error for testing error handling."""
    from proxbox_api.exception import ProxboxException

    raise ProxboxException(
        message="Connection refused",
        detail="Could not connect to Proxmox. Check if the endpoint is reachable.",
    )


@pytest.fixture
async def proxmox_mock_backend():
    """In-process MockBackend for fast unit/integration tests.

    This fixture provides an in-memory MockBackend that generates
    responses from the OpenAPI schema without requiring HTTP.

    Usage:
        async def test_something(proxmox_mock_backend):
            vms = await proxmox_mock_backend.request("GET", "/api2/json/nodes/pve01/qemu")
    """
    _prior = os.environ.get("PROXMOX_API_MODE")
    os.environ["PROXMOX_API_MODE"] = "mock"

    from proxmox_sdk.sdk.backends.mock import MockBackend

    backend = MockBackend(schema_version="latest")
    yield backend

    if hasattr(backend, "_store") and backend._store:
        try:
            backend._store.reset()
        except Exception:
            pass
    if _prior is None:
        os.environ.pop("PROXMOX_API_MODE", None)
    else:
        os.environ["PROXMOX_API_MODE"] = _prior


@pytest.fixture
async def proxmox_mock_http_published():
    """HTTP mock using published Docker image (port 8006).

    This fixture connects to the proxmox-mock-published container
    running on localhost:8006.

    Usage:
        async def test_something(proxmox_mock_http_published):
            async with ProxmoxSDK.mock() as sdk:
                vms = await sdk.nodes.get()
    """
    _prior = os.environ.get("PROXMOX_API_MODE")
    os.environ["PROXMOX_API_MODE"] = "mock"

    from proxmox_sdk import ProxmoxSDK

    base_url = os.getenv("PROXMOX_MOCK_PUBLISHED_URL", "http://localhost:8006")
    sdk = ProxmoxSDK(host=base_url, backend="https", verify_ssl=False)
    yield sdk
    try:
        await sdk.close()
    except Exception:
        pass
    if _prior is None:
        os.environ.pop("PROXMOX_API_MODE", None)
    else:
        os.environ["PROXMOX_API_MODE"] = _prior


@pytest.fixture
async def proxmox_mock_http_local():
    """HTTP mock using locally built Docker image (port 8007).

    This fixture connects to the proxmox-mock-local container
    running on localhost:8007.

    Usage:
        async def test_something(proxmox_mock_http_local):
            async with ProxmoxSDK.mock() as sdk:
                vms = await sdk.nodes.get()
    """
    _prior = os.environ.get("PROXMOX_API_MODE")
    os.environ["PROXMOX_API_MODE"] = "mock"

    from proxmox_sdk import ProxmoxSDK

    base_url = os.getenv("PROXMOX_MOCK_LOCAL_URL", "http://localhost:8007")
    sdk = ProxmoxSDK(host=base_url, backend="https", verify_ssl=False)
    yield sdk
    try:
        await sdk.close()
    except Exception:
        pass
    if _prior is None:
        os.environ.pop("PROXMOX_API_MODE", None)
    else:
        os.environ["PROXMOX_API_MODE"] = _prior
