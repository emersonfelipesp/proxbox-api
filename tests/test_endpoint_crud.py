"""Authenticated HTTP CRUD coverage for NetBox and Proxmox endpoint routes.

Uses the conftest-provided sync TestClient fixtures (test_client, auth_test_client)
to exercise the full request lifecycle including auth middleware, SSRF validation,
and DB persistence via the overridden get_session dependency.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from proxbox_api.routes.proxmox.endpoints import ProxmoxEndpointUpdate


def test_proxmox_endpoint_update_rejects_explicit_null_ssh_port() -> None:
    with pytest.raises(ValidationError, match="ssh_port cannot be null"):
        ProxmoxEndpointUpdate.model_validate({"ssh_port": None})

    omitted = ProxmoxEndpointUpdate.model_validate({"enabled": True})
    assert "ssh_port" not in omitted.model_fields_set


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

    def test_create_proxmox_endpoint_persists_complete_cloud_image_ssh_binding(
        self,
        auth_test_client,
    ):
        payload = {
            "name": "pve-packer-bound",
            "ip_address": "192.168.1.120",
            "port": 8006,
            "username": "root@pam",
            "password": "secret",
            "verify_ssl": False,
            "enabled": True,
            "allow_writes": True,
            "access_methods": "api_ssh",
            "ssh_target_node": "pve01",
            "ssh_host": "192.168.1.120",
            "ssh_username": "root",
            "ssh_port": 22,
            "ssh_identity_file": "/etc/proxbox/ssh_keys/id_ed25519",
            "ssh_known_host_fingerprint": ("SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        }

        response = auth_test_client.post("/proxmox/endpoints", json=payload)

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["ssh_target_node"] == "pve01"
        assert data["ssh_host"] == "192.168.1.120"
        assert data["ssh_username"] == "root"
        assert data["ssh_identity_file"] == "/etc/proxbox/ssh_keys/id_ed25519"
        assert data["ssh_known_host_fingerprint"].startswith("SHA256:")

    def test_create_proxmox_endpoint_rejects_partial_cloud_image_ssh_binding(
        self,
        auth_test_client,
    ):
        response = auth_test_client.post(
            "/proxmox/endpoints",
            json={
                "name": "pve-packer-partial",
                "ip_address": "192.168.1.121",
                "port": 8006,
                "username": "root@pam",
                "password": "secret",
                "ssh_target_node": "pve01",
                "ssh_host": "192.168.1.121",
            },
        )

        assert response.status_code == 422
        assert "request_validation_error" in response.text
        assert "ssh_host" not in response.text

    def test_create_proxmox_endpoint_rejects_blank_cloud_image_node_binding(
        self,
        auth_test_client,
    ):
        response = auth_test_client.post(
            "/proxmox/endpoints",
            json={
                "name": "pve-packer-blank-node",
                "ip_address": "192.168.1.122",
                "port": 8006,
                "username": "root@pam",
                "password": "secret",
                "ssh_target_node": "   ",
                "ssh_host": "192.168.1.122",
                "ssh_username": "root",
                "ssh_port": 22,
                "ssh_identity_file": "/etc/proxbox/ssh_keys/id_ed25519",
                "ssh_known_host_fingerprint": (
                    "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
                ),
            },
        )

        assert response.status_code == 422
        assert "request_validation_error" in response.text
        assert "ssh_target_node" not in response.text

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
