"""Tests for NetBox and Proxmox endpoint CRUD APIs."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.database import ApiKey, get_async_session, get_session
from proxbox_api.main import app


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
        raw_key = "test-api-key-for-endpoint-crud-suite"
        ApiKey.store_key(session, raw_key, label="test-endpoint-crud")

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_async_session] = _override_get_async_session
    with TestClient(app, headers={"X-Proxbox-API-Key": raw_key}) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    asyncio.run(async_engine.dispose())


def test_proxmox_endpoint_crud_lifecycle(client: TestClient):
    create_payload = {
        "name": "pve-lab-1",
        "ip_address": "10.0.0.10",
        "domain": "pve-lab-1.local",
        "port": 8006,
        "username": "root@pam",
        "password": "supersecret",
        "verify_ssl": False,
        "site_id": 42,
        "site_slug": "dc1",
        "site_name": "DC 1",
        "tenant_id": 9,
        "tenant_slug": "customer-a",
        "tenant_name": "Customer A",
    }

    create_response = client.post("/proxmox/endpoints", json=create_payload)
    assert create_response.status_code == 200
    created = create_response.json()
    endpoint_id = created["id"]
    assert created["name"] == "pve-lab-1"
    assert created["site_id"] == 42
    assert created["tenant_slug"] == "customer-a"
    for secret_field in ("password", "token_name", "token_value"):
        assert secret_field not in created

    list_response = client.get("/proxmox/endpoints")
    assert list_response.status_code == 200
    listed = list_response.json()
    assert len(listed) == 1
    assert listed[0]["id"] == endpoint_id
    assert listed[0]["site_slug"] == "dc1"
    for secret_field in ("password", "token_name", "token_value"):
        assert secret_field not in listed[0]

    get_response = client.get(f"/proxmox/endpoints/{endpoint_id}")
    assert get_response.status_code == 200
    assert get_response.json()["username"] == "root@pam"
    for secret_field in ("password", "token_name", "token_value"):
        assert secret_field not in get_response.json()

    update_payload = {
        "name": "pve-lab-1-updated",
        "port": 8443,
        "verify_ssl": True,
    }
    update_response = client.put(f"/proxmox/endpoints/{endpoint_id}", json=update_payload)
    assert update_response.status_code == 200
    updated = update_response.json()
    assert updated["name"] == "pve-lab-1-updated"
    assert updated["port"] == 8443
    assert updated["verify_ssl"] is True
    for secret_field in ("password", "token_name", "token_value"):
        assert secret_field not in updated

    delete_response = client.delete(f"/proxmox/endpoints/{endpoint_id}")
    assert delete_response.status_code == 200
    assert delete_response.json() == {"message": "Proxmox endpoint deleted."}

    get_deleted_response = client.get(f"/proxmox/endpoints/{endpoint_id}")
    assert get_deleted_response.status_code == 404
    assert get_deleted_response.json()["detail"] == "Proxmox endpoint not found"


def test_proxmox_endpoint_create_requires_auth_fields(client: TestClient):
    invalid_payload = {
        "name": "pve-lab-2",
        "ip_address": "10.0.0.11",
        "domain": "pve-lab-2.local",
        "port": 8006,
        "username": "root@pam",
        "verify_ssl": True,
    }

    response = client.post("/proxmox/endpoints", json=invalid_payload)
    assert response.status_code == 400
    assert response.json()["detail"] == "Provide password or both token_name/token_value"


def test_netbox_endpoint_only_allows_single_instance(client: TestClient):
    first_payload = {
        "name": "netbox-primary",
        "ip_address": "10.0.0.20",
        "domain": "netbox.local",
        "port": 443,
        "token": "token-1",
        "verify_ssl": True,
    }
    second_payload = {
        "name": "netbox-secondary",
        "ip_address": "10.0.0.21",
        "domain": "netbox2.local",
        "port": 443,
        "token": "token-2",
        "verify_ssl": True,
    }

    first_response = client.post("/netbox/endpoint", json=first_payload)
    assert first_response.status_code == 200

    second_response = client.post("/netbox/endpoint", json=second_payload)
    assert second_response.status_code == 400
    assert second_response.json()["detail"] == "Only one NetBox endpoint is allowed"
