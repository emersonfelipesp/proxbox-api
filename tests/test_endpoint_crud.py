"""Authenticated HTTP CRUD coverage for NetBox and Proxmox endpoint routes.

Uses the conftest-provided sync TestClient fixtures (test_client, auth_test_client)
to exercise the full request lifecycle including auth middleware, SSRF validation,
and DB persistence via the overridden get_session dependency.
"""

from __future__ import annotations

import asyncio

import pytest

from proxbox_api import credentials as credentials_module
from proxbox_api.database import NetBoxEndpoint
from proxbox_api.routes.netbox import (
    NetBoxEndpointCreate,
    NetBoxEndpointUpdate,
    create_netbox_endpoint,
    delete_netbox_endpoint,
    update_netbox_endpoint,
)


class TestAuthBoundary:
    """Verify that protected routes reject unauthenticated callers."""

    def test_protected_route_without_key_returns_401(self, test_client):
        resp = test_client.get("/netbox/endpoint")
        assert resp.status_code == 401

    def test_proxmox_endpoints_list_without_key_returns_401(self, test_client):
        resp = test_client.get("/proxmox/endpoints")
        assert resp.status_code == 401

    def test_root_is_auth_exempt(self, test_client):
        resp = test_client.get("/")
        assert resp.status_code == 200

    def test_health_is_auth_exempt(self, test_client):
        resp = test_client.get("/health")
        assert resp.status_code == 200


class TestNetBoxEndpointCRUD:
    """CRUD coverage for the singleton NetBox endpoint resource."""

    def test_list_endpoints_initially_empty(self, auth_test_client):
        resp = auth_test_client.get("/netbox/endpoint")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_nonexistent_endpoint_returns_404(self, auth_test_client):
        resp = auth_test_client.get("/netbox/endpoint/999")
        assert resp.status_code == 404

    def test_create_netbox_endpoint(self, auth_test_client):
        payload = {
            "name": "test-netbox",
            "ip_address": "192.168.1.10",
            "domain": "",
            "port": 8000,
            "token_version": "v1",
            "token": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "verify_ssl": False,
        }
        resp = auth_test_client.post("/netbox/endpoint", json=payload)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["name"] == "test-netbox"
        assert "token" not in data

    def test_create_second_endpoint_rejected(self, auth_test_client):
        """NetBox endpoint is a singleton — second create must fail."""
        payload = {
            "name": "first",
            "ip_address": "192.168.1.10",
            "domain": "",
            "port": 8000,
            "token_version": "v1",
            "token": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "verify_ssl": False,
        }
        first = auth_test_client.post("/netbox/endpoint", json=payload)
        assert first.status_code == 200, first.text

        payload["name"] = "second"
        second = auth_test_client.post("/netbox/endpoint", json=payload)
        assert second.status_code in (400, 409), second.text

    def test_get_created_endpoint_by_id(self, auth_test_client):
        payload = {
            "name": "by-id",
            "ip_address": "192.168.1.20",
            "domain": "",
            "port": 8000,
            "token_version": "v1",
            "token": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "verify_ssl": False,
        }
        created = auth_test_client.post("/netbox/endpoint", json=payload)
        assert created.status_code == 200, created.text
        endpoint_id = created.json()["id"]

        resp = auth_test_client.get(f"/netbox/endpoint/{endpoint_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "by-id"

    @pytest.mark.asyncio
    async def test_create_publishes_enabled_endpoint_to_runtime(self, db_session, monkeypatch):
        completed = {"invalidate": False, "refresh": False}

        async def invalidate(endpoint_id: int) -> int:
            assert endpoint_id > 0
            completed["invalidate"] = True
            return 17

        async def refresh(endpoint_arg, *, expected_revision: int) -> bool:
            assert endpoint_arg.enabled is True
            assert expected_revision == 17
            completed["refresh"] = True
            return True

        monkeypatch.setattr(
            "proxbox_api.routes.netbox.invalidate_netbox_api_cache",
            invalidate,
        )
        monkeypatch.setattr(
            "proxbox_api.routes.netbox.refresh_default_netbox_api",
            refresh,
        )

        created = await create_netbox_endpoint(
            NetBoxEndpointCreate(
                name="runtime-default",
                ip_address="192.168.1.21",
                port=8000,
                token="runtime-token",
                verify_ssl=False,
            ),
            db_session,
        )

        assert created.id is not None
        assert completed == {"invalidate": True, "refresh": True}

    @pytest.mark.asyncio
    async def test_partial_update_preserves_exact_encrypted_credentials(
        self,
        db_session,
        monkeypatch,
    ):
        monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "partial-update-regression-key")
        credentials_module.reset_encryption_cache()
        try:
            endpoint = NetBoxEndpoint(
                name="encrypted",
                ip_address="192.168.1.22",
                domain="",
                port=8000,
                token_version="v2",
                token_key="public-id",
                token="secret-value",
                verify_ssl=False,
                enabled=False,
            )
            endpoint.set_encrypted_token(endpoint.token)
            endpoint.set_encrypted_token_key(endpoint.token_key)
            db_session.add(endpoint)
            db_session.commit()
            db_session.refresh(endpoint)
            assert endpoint.id is not None
            stored_token = endpoint.token
            stored_token_key = endpoint.token_key

            await update_netbox_endpoint(
                endpoint.id,
                NetBoxEndpointUpdate(name="renamed"),
                db_session,
            )

            db_session.refresh(endpoint)
            assert endpoint.name == "renamed"
            assert endpoint.token == stored_token
            assert endpoint.token_key == stored_token_key
            assert endpoint.get_decrypted_token() == "secret-value"
            assert endpoint.get_decrypted_token_key() == "public-id"
        finally:
            credentials_module.reset_encryption_cache()

    @pytest.mark.asyncio
    async def test_update_awaits_runtime_client_invalidation(self, db_session, monkeypatch):
        endpoint = NetBoxEndpoint(
            name="to-rotate",
            ip_address="192.168.1.25",
            domain="",
            port=8000,
            token_version="v1",
            token="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            verify_ssl=False,
        )
        endpoint.set_encrypted_token(endpoint.token)
        db_session.add(endpoint)
        db_session.commit()
        db_session.refresh(endpoint)
        assert endpoint.id is not None
        completed = {"invalidate": False, "refresh": False}

        async def invalidate(endpoint_id_arg: int) -> int:
            await asyncio.sleep(0)
            assert endpoint_id_arg == endpoint.id
            completed["invalidate"] = True
            return 41

        async def refresh(endpoint_arg, *, expected_revision: int) -> bool:
            await asyncio.sleep(0)
            assert endpoint_arg is endpoint
            assert expected_revision == 41
            completed["refresh"] = True
            return True

        monkeypatch.setattr(
            "proxbox_api.routes.netbox.invalidate_netbox_api_cache",
            invalidate,
        )
        monkeypatch.setattr(
            "proxbox_api.routes.netbox.refresh_default_netbox_api",
            refresh,
        )

        updated = await update_netbox_endpoint(
            endpoint.id,
            NetBoxEndpointUpdate(token="cccccccccccccccccccccccccccccccccccccccc"),
            db_session,
        )

        assert updated.id == endpoint.id
        assert completed == {"invalidate": True, "refresh": True}

    @pytest.mark.asyncio
    async def test_disabling_endpoint_invalidates_without_republishing(
        self,
        db_session,
        monkeypatch,
    ):
        endpoint = NetBoxEndpoint(
            name="to-disable",
            ip_address="192.168.1.26",
            domain="",
            port=8000,
            token_version="v1",
            token="dddddddddddddddddddddddddddddddddddddddd",
            verify_ssl=False,
        )
        endpoint.set_encrypted_token(endpoint.token)
        db_session.add(endpoint)
        db_session.commit()
        db_session.refresh(endpoint)
        assert endpoint.id is not None
        completed = {"invalidate": False, "refresh": False, "clear": False}

        async def invalidate(endpoint_id_arg: int) -> int:
            assert endpoint_id_arg == endpoint.id
            completed["invalidate"] = True
            return 42

        async def refresh(endpoint_arg, *, expected_revision: int) -> bool:
            completed["refresh"] = True
            return True

        def clear_default() -> None:
            completed["clear"] = True

        monkeypatch.setattr(
            "proxbox_api.routes.netbox.invalidate_netbox_api_cache",
            invalidate,
        )
        monkeypatch.setattr(
            "proxbox_api.routes.netbox.refresh_default_netbox_api",
            refresh,
        )
        monkeypatch.setattr(
            "proxbox_api.routes.netbox.clear_default_netbox_api",
            clear_default,
        )

        updated = await update_netbox_endpoint(
            endpoint.id,
            NetBoxEndpointUpdate(enabled=False),
            db_session,
        )

        assert updated.enabled is False
        assert completed == {"invalidate": True, "refresh": False, "clear": True}

    def test_delete_endpoint(self, auth_test_client):
        payload = {
            "name": "to-delete",
            "ip_address": "192.168.1.30",
            "domain": "",
            "port": 8000,
            "token_version": "v1",
            "token": "cccccccccccccccccccccccccccccccccccccccc",
            "verify_ssl": False,
        }
        created = auth_test_client.post("/netbox/endpoint", json=payload)
        assert created.status_code == 200, created.text
        endpoint_id = created.json()["id"]

        del_resp = auth_test_client.delete(f"/netbox/endpoint/{endpoint_id}")
        assert del_resp.status_code in (200, 204), del_resp.text

        get_resp = auth_test_client.get(f"/netbox/endpoint/{endpoint_id}")
        assert get_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_awaits_runtime_client_invalidation(self, db_session, monkeypatch):
        endpoint = NetBoxEndpoint(
            name="to-delete-await",
            ip_address="192.168.1.31",
            domain="",
            port=8000,
            token_version="v1",
            token="dddddddddddddddddddddddddddddddddddddddd",
            verify_ssl=False,
        )
        endpoint.set_encrypted_token(endpoint.token)
        db_session.add(endpoint)
        db_session.commit()
        db_session.refresh(endpoint)
        assert endpoint.id is not None
        completed = {"value": False}

        async def invalidate(endpoint_id_arg: int) -> None:
            await asyncio.sleep(0)
            assert endpoint_id_arg == endpoint.id
            completed["value"] = True

        monkeypatch.setattr(
            "proxbox_api.routes.netbox.invalidate_netbox_api_cache",
            invalidate,
        )

        deleted = await delete_netbox_endpoint(endpoint.id, db_session)

        assert deleted == {"message": "NetBox Endpoint deleted."}
        assert completed["value"] is True


class TestProxmoxEndpointCRUD:
    """CRUD coverage for Proxmox endpoint resources.

    SSRF defaults to allow_private_ips=True so private IPs (192.168.x.x,
    10.x.x.x) pass without additional configuration in the test environment.
    """

    def test_list_endpoints_initially_empty(self, auth_test_client):
        resp = auth_test_client.get("/proxmox/endpoints")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_nonexistent_endpoint_returns_404(self, auth_test_client):
        resp = auth_test_client.get("/proxmox/endpoints/999")
        assert resp.status_code == 404

    def test_create_proxmox_endpoint(self, auth_test_client):
        payload = {
            "name": "pve-test",
            "ip_address": "192.168.1.100",
            "port": 8006,
            "username": "root@pam",
            "password": "secret",
            "verify_ssl": False,
            "timeout": 30,
            "max_retries": 2,
            "retry_backoff": 1.5,
        }
        resp = auth_test_client.post("/proxmox/endpoints", json=payload)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["name"] == "pve-test"
        assert data["timeout"] == 30
        assert data["max_retries"] == 2
        assert data["retry_backoff"] == 1.5
        assert "password" not in data

    def test_get_created_endpoint_by_id(self, auth_test_client):
        payload = {
            "name": "pve-by-id",
            "ip_address": "192.168.1.101",
            "port": 8006,
            "username": "root@pam",
            "password": "secret",
            "verify_ssl": False,
        }
        created = auth_test_client.post("/proxmox/endpoints", json=payload)
        assert created.status_code == 200, created.text
        endpoint_id = created.json()["id"]

        resp = auth_test_client.get(f"/proxmox/endpoints/{endpoint_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "pve-by-id"

    def test_duplicate_name_rejected(self, auth_test_client):
        payload = {
            "name": "pve-dup",
            "ip_address": "192.168.1.102",
            "port": 8006,
            "username": "root@pam",
            "password": "secret",
            "verify_ssl": False,
        }
        first = auth_test_client.post("/proxmox/endpoints", json=payload)
        assert first.status_code == 200, first.text

        payload["ip_address"] = "192.168.1.103"
        second = auth_test_client.post("/proxmox/endpoints", json=payload)
        assert second.status_code in (400, 409), second.text

    def test_delete_proxmox_endpoint(self, auth_test_client):
        payload = {
            "name": "pve-to-delete",
            "ip_address": "192.168.1.110",
            "port": 8006,
            "username": "root@pam",
            "password": "secret",
            "verify_ssl": False,
        }
        created = auth_test_client.post("/proxmox/endpoints", json=payload)
        assert created.status_code == 200, created.text
        endpoint_id = created.json()["id"]

        del_resp = auth_test_client.delete(f"/proxmox/endpoints/{endpoint_id}")
        assert del_resp.status_code in (200, 204), del_resp.text

        get_resp = auth_test_client.get(f"/proxmox/endpoints/{endpoint_id}")
        assert get_resp.status_code == 404

    def test_create_defaults_access_methods_to_api(self, auth_test_client):
        """New endpoints created through the API default to API-only."""
        payload = {
            "name": "pve-access-default",
            "ip_address": "192.168.1.120",
            "port": 8006,
            "username": "root@pam",
            "password": "secret",
            "verify_ssl": False,
        }
        resp = auth_test_client.post("/proxmox/endpoints", json=payload)
        assert resp.status_code == 200, resp.text
        assert resp.json()["access_methods"] == "api"

    def test_create_accepts_api_ssh(self, auth_test_client):
        payload = {
            "name": "pve-access-ssh",
            "ip_address": "192.168.1.121",
            "port": 8006,
            "username": "root@pam",
            "password": "secret",
            "verify_ssl": False,
            "access_methods": "api_ssh",
        }
        resp = auth_test_client.post("/proxmox/endpoints", json=payload)
        assert resp.status_code == 200, resp.text
        assert resp.json()["access_methods"] == "api_ssh"

    def test_create_rejects_ssh_only(self, auth_test_client):
        """SSH-only is unrepresentable: 'ssh' must be a 422."""
        payload = {
            "name": "pve-access-sshonly",
            "ip_address": "192.168.1.122",
            "port": 8006,
            "username": "root@pam",
            "password": "secret",
            "verify_ssl": False,
            "access_methods": "ssh",
        }
        resp = auth_test_client.post("/proxmox/endpoints", json=payload)
        assert resp.status_code == 422, resp.text

    def test_update_access_methods(self, auth_test_client):
        payload = {
            "name": "pve-access-update",
            "ip_address": "192.168.1.123",
            "port": 8006,
            "username": "root@pam",
            "password": "secret",
            "verify_ssl": False,
        }
        created = auth_test_client.post("/proxmox/endpoints", json=payload)
        assert created.status_code == 200, created.text
        endpoint_id = created.json()["id"]
        assert created.json()["access_methods"] == "api"

        upd = auth_test_client.put(
            f"/proxmox/endpoints/{endpoint_id}",
            json={"access_methods": "api_ssh"},
        )
        assert upd.status_code == 200, upd.text
        assert upd.json()["access_methods"] == "api_ssh"

        bad = auth_test_client.put(
            f"/proxmox/endpoints/{endpoint_id}",
            json={"access_methods": "ssh"},
        )
        assert bad.status_code == 422, bad.text
