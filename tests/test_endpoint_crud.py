"""Authenticated HTTP CRUD coverage for NetBox and Proxmox endpoint routes.

Uses the conftest-provided sync TestClient fixtures (test_client, auth_test_client)
to exercise the full request lifecycle including auth middleware, SSRF validation,
and DB persistence via the overridden get_session dependency.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from proxbox_api import credentials
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.routes.proxmox import endpoints as proxmox_endpoints
from proxbox_api.routes.proxmox.endpoints import (
    ProxmoxEndpointUpdate,
    _endpoint_update_changes,
    update_proxmox_endpoint,
)


@pytest.fixture(autouse=True)
def isolated_endpoint_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep endpoint CRUD and bootstrap probes inside synthetic I/O boundaries."""
    from proxbox_api import settings_client
    from proxbox_api.netbox_probe import NetBoxProbeResult
    from proxbox_api.routes import netbox as netbox_routes

    monkeypatch.setattr(
        netbox_routes,
        "probe_netbox_endpoint",
        AsyncMock(
            return_value=NetBoxProbeResult(reachable=True, status="reachable", api_version="test")
        ),
    )
    monkeypatch.setattr(settings_client, "_request_settings_json", Mock(return_value=(None, 403)))

    attempted_io: list[str] = []

    def guard(method: str) -> Callable[..., Any]:
        original = getattr(socket.socket, method)

        def guarded(sock: socket.socket, *args: Any, **kwargs: Any) -> Any:
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                attempted_io.append(method)
                raise AssertionError(
                    "External network is forbidden in isolated endpoint CRUD tests."
                )
            return original(sock, *args, **kwargs)

        return guarded

    for method in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, method, guard(method))

    def block_name_resolution(*args: Any, **kwargs: Any) -> None:
        attempted_io.append("name_resolution")
        raise AssertionError("DNS is forbidden in isolated endpoint CRUD tests.")

    for method in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        monkeypatch.setattr(socket, method, block_name_resolution)
    yield
    assert not attempted_io, "An isolated endpoint test attempted external network I/O."


_PERSISTED_UPDATE_VALUES = {
    "name": "pve-complete",
    "node_device_name_template": "{node}.{cluster}",
    "ip_address": "192.0.2.10",
    "domain": "pve-complete.example.com",
    "port": 8006,
    "username": "root@pam",
    "password": "password-secret",
    "verify_ssl": True,
    "enabled": True,
    "allow_writes": False,
    "allow_packer_template_builds": False,
    "access_methods": "api_ssh",
    "ssh_target_node": "pve01",
    "ssh_host": "192.0.2.10",
    "ssh_username": "root",
    "ssh_port": 22,
    "ssh_identity_file": "/etc/proxbox/ssh_keys/id_ed25519",
    "ssh_known_host_fingerprint": "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "token_name": "sync",
    "token_value": "token-secret",
    "site_id": 11,
    "site_slug": "site-one",
    "site_name": "Site One",
    "tenant_id": 12,
    "tenant_slug": "tenant-one",
    "tenant_name": "Tenant One",
    "timeout": 30,
    "max_retries": 2,
    "retry_backoff": 1.5,
}


def _complete_endpoint() -> ProxmoxEndpoint:
    values = dict(_PERSISTED_UPDATE_VALUES)
    values["password"] = None
    values["token_value"] = None
    endpoint = ProxmoxEndpoint(**values)
    endpoint.set_encrypted_password(_PERSISTED_UPDATE_VALUES["password"])
    endpoint.set_encrypted_token_value(_PERSISTED_UPDATE_VALUES["token_value"])
    return endpoint


def _changed_value(field: str, value: object) -> object:
    if field in {"verify_ssl", "enabled", "allow_writes", "allow_packer_template_builds"}:
        return not value
    if field in {"port", "ssh_port", "site_id", "tenant_id", "timeout", "max_retries"}:
        return int(value) + 1
    if field == "retry_backoff":
        return float(value) + 0.5
    if field == "access_methods":
        return "api"
    return f"changed-{value}"


@pytest.mark.parametrize("field", sorted(_PERSISTED_UPDATE_VALUES))
def test_endpoint_update_change_decision_covers_every_persisted_field(
    field: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY", "endpoint-field-coverage-key")
    credentials.reset_encryption_cache()
    endpoint = _complete_endpoint()

    assert not _endpoint_update_changes(endpoint, {field: _PERSISTED_UPDATE_VALUES[field]})
    assert _endpoint_update_changes(
        endpoint,
        {field: _changed_value(field, _PERSISTED_UPDATE_VALUES[field])},
    )
    assert set(ProxmoxEndpointUpdate.model_fields) == set(_PERSISTED_UPDATE_VALUES)


@pytest.mark.parametrize(
    ("field", "persisted"),
    [
        ("domain", "pve.example.com"),
        ("password", "secret"),
        ("token_name", "sync"),
        ("token_value", "secret-token"),
        ("site_id", 1),
        ("site_slug", "site"),
        ("site_name", "Site"),
        ("tenant_id", 2),
        ("tenant_slug", "tenant"),
        ("tenant_name", "Tenant"),
        ("timeout", 30),
        ("max_retries", 2),
        ("retry_backoff", 1.5),
        ("ssh_target_node", "pve01"),
        ("ssh_host", "192.0.2.10"),
        ("ssh_username", "root"),
        ("ssh_identity_file", "/etc/proxbox/ssh_keys/id_ed25519"),
        ("ssh_known_host_fingerprint", "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
    ],
)
def test_endpoint_update_distinguishes_omitted_and_explicit_null_optional_fields(
    field: str,
    persisted: object,
) -> None:
    endpoint = ProxmoxEndpoint(
        name="pve-null",
        ip_address="192.0.2.20",
        port=8006,
        username="root@pam",
    )
    if field == "password":
        endpoint.set_encrypted_password(persisted)
    elif field == "token_value":
        endpoint.set_encrypted_token_value(persisted)
    else:
        setattr(endpoint, field, persisted)

    omitted = ProxmoxEndpointUpdate.model_validate({}).model_dump(exclude_unset=True)
    explicit_null = ProxmoxEndpointUpdate.model_construct(**{field: None}).model_dump(
        exclude_unset=True
    )

    assert not _endpoint_update_changes(endpoint, omitted)
    assert _endpoint_update_changes(endpoint, explicit_null)


@pytest.mark.asyncio
async def test_credential_noop_resolves_cold_key_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    event_loop_thread = threading.get_ident()
    settings_threads: list[int] = []

    def get_settings() -> dict[str, object]:
        settings_threads.append(threading.get_ident())
        assert threading.get_ident() != event_loop_thread
        return {}

    endpoint = ProxmoxEndpoint(
        id=1,
        name="pve-cold-key",
        ip_address="192.0.2.30",
        port=8006,
        username="root@pam",
        password="same-secret",
    )
    session = SimpleNamespace(get=AsyncMock(return_value=endpoint))
    monkeypatch.delenv("PROXBOX_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("PROXBOX_ENCRYPTION_KEY_FILE", str(tmp_path / "missing-key"))
    monkeypatch.setattr("proxbox_api.settings_client.get_settings", get_settings)
    credentials.reset_encryption_cache()

    result = await update_proxmox_endpoint(
        1,
        ProxmoxEndpointUpdate(password="same-secret"),
        session,
    )

    assert result.id == 1
    # Identical credential payloads are a true no-op: no settings fetch or crypto work.
    assert settings_threads == []
    credentials.reset_encryption_cache()


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
            "node_device_name_template": "{node}.{cluster_slug}.example.com",
        }
        resp = auth_test_client.post("/proxmox/endpoints", json=payload)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["name"] == "pve-test"
        assert data["timeout"] == 30
        assert data["max_retries"] == 2
        assert data["retry_backoff"] == 1.5
        assert data["node_device_name_template"] == "{node}.{cluster_slug}.example.com"
        assert data["allow_packer_template_builds"] is False
        assert "password" not in data

        updated = auth_test_client.put(
            f"/proxmox/endpoints/{data['id']}",
            json={"node_device_name_template": "{node}.{endpoint}"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["node_device_name_template"] == "{node}.{endpoint}"

    def test_identical_update_skips_validation_settings_and_write(self, auth_test_client):
        payload = {
            "name": "pve-idempotent",
            "ip_address": "192.168.1.130",
            "domain": "",
            "port": 8006,
            "username": "root@pam",
            "password": "secret",
            "verify_ssl": False,
            "enabled": True,
            "allow_writes": False,
            "allow_packer_template_builds": False,
            "access_methods": "api",
            "ssh_port": 22,
            "timeout": 30,
            "max_retries": 2,
            "retry_backoff": 1.5,
        }
        created = auth_test_client.post("/proxmox/endpoints", json=payload)
        assert created.status_code == 200, created.text

        settings = Mock(side_effect=AssertionError("get_settings must not be called"))
        commit = AsyncMock(side_effect=AssertionError("commit must not be called"))
        with (
            patch.object(proxmox_endpoints, "get_settings", settings),
            patch("proxbox_api.ssrf.socket.getaddrinfo") as getaddrinfo,
            patch.object(AsyncSession, "commit", commit),
        ):
            updated = auth_test_client.put(
                f"/proxmox/endpoints/{created.json()['id']}",
                json=payload,
            )

        assert updated.status_code == 200, updated.text
        assert updated.json() == created.json()
        settings.assert_not_called()
        getaddrinfo.assert_not_called()
        commit.assert_not_awaited()

    def test_changed_update_still_rejects_blocked_host(self, auth_test_client, monkeypatch):
        created = auth_test_client.post(
            "/proxmox/endpoints",
            json={
                "name": "pve-blocked-update",
                "ip_address": "192.168.1.131",
                "port": 8006,
                "username": "root@pam",
                "password": "secret",
            },
        )
        assert created.status_code == 200, created.text
        monkeypatch.setattr(proxmox_endpoints, "pre_allow_endpoint_hosts", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            proxmox_endpoints,
            "get_settings",
            lambda: {
                "ssrf_protection_enabled": True,
                "allow_private_ips": False,
                "allowed_ip_ranges": [],
                "blocked_ip_ranges": [],
            },
        )

        updated = auth_test_client.put(
            f"/proxmox/endpoints/{created.json()['id']}",
            json={"ip_address": "127.0.0.1"},
        )

        assert updated.status_code == 400
        assert updated.json() == {
            "detail": (
                "Invalid IP address: Host '127.0.0.1' is a reserved/internal IP address. "
                "Either add it to ProxmoxEndpoint first, or adjust SSRF settings in "
                "ProxboxPluginSettings.. Adjust SSRF settings in ProxboxPluginSettings."
            )
        }

    def test_credentials_change_is_persisted(self, auth_test_client, db_session):
        created = auth_test_client.post(
            "/proxmox/endpoints",
            json={
                "name": "pve-credential-change",
                "ip_address": "192.168.1.132",
                "port": 8006,
                "username": "root@pam",
                "password": "old-secret",
            },
        )
        assert created.status_code == 200, created.text

        updated = auth_test_client.put(
            f"/proxmox/endpoints/{created.json()['id']}",
            json={"password": "new-secret"},
        )

        assert updated.status_code == 200, updated.text
        db_session.expire_all()
        stored = db_session.exec(
            select(ProxmoxEndpoint).where(ProxmoxEndpoint.id == created.json()["id"])
        ).one()
        assert stored.get_decrypted_password() == "new-secret"

    def test_node_device_name_template_rejects_unknown_placeholder(
        self,
        auth_test_client,
    ):
        response = auth_test_client.post(
            "/proxmox/endpoints",
            json={
                "name": "pve-invalid-template",
                "ip_address": "192.168.1.199",
                "port": 8006,
                "username": "root@pam",
                "password": "secret",
                "node_device_name_template": "{node}.{rack}",
            },
        )

        assert response.status_code == 422

    def test_create_rejects_template_invalid_for_actual_endpoint_name(
        self,
        auth_test_client,
    ):
        response = auth_test_client.post(
            "/proxmox/endpoints",
            json={
                "name": "endpoint with spaces",
                "ip_address": "192.168.1.198",
                "port": 8006,
                "username": "root@pam",
                "password": "secret",
                "node_device_name_template": "{node}.{endpoint}",
            },
        )

        assert response.status_code == 422

    def test_template_only_update_validates_existing_endpoint_name(
        self,
        auth_test_client,
    ):
        created = auth_test_client.post(
            "/proxmox/endpoints",
            json={
                "name": "endpoint with spaces",
                "ip_address": "192.168.1.197",
                "port": 8006,
                "username": "root@pam",
                "password": "secret",
            },
        )
        assert created.status_code == 200, created.text

        response = auth_test_client.put(
            f"/proxmox/endpoints/{created.json()['id']}",
            json={"node_device_name_template": "{node}.{endpoint}"},
        )

        assert response.status_code == 422

    def test_simultaneous_name_and_template_update_uses_new_endpoint_name(
        self,
        auth_test_client,
    ):
        created = auth_test_client.post(
            "/proxmox/endpoints",
            json={
                "name": "endpoint with spaces",
                "ip_address": "192.168.1.196",
                "port": 8006,
                "username": "root@pam",
                "password": "secret",
            },
        )
        assert created.status_code == 200, created.text

        response = auth_test_client.put(
            f"/proxmox/endpoints/{created.json()['id']}",
            json={
                "name": "endpoint-valid",
                "node_device_name_template": "{node}.{endpoint}",
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["name"] == "endpoint-valid"

    def test_packer_template_authorization_round_trips_and_can_be_revoked(
        self,
        auth_test_client,
    ):
        created = auth_test_client.post(
            "/proxmox/endpoints",
            json={
                "name": "pve-packer-authorized",
                "ip_address": "192.168.1.124",
                "port": 8006,
                "username": "root@pam",
                "password": "secret",
                "allow_writes": True,
                "allow_packer_template_builds": True,
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["allow_packer_template_builds"] is True

        endpoint_id = created.json()["id"]
        revoked = auth_test_client.put(
            f"/proxmox/endpoints/{endpoint_id}",
            json={"allow_packer_template_builds": False},
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["allow_packer_template_builds"] is False

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
